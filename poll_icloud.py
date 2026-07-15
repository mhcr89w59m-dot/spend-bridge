#!/usr/bin/env python3
"""
iCloud → Worker bridge for the spend tracker.

Connects to iCloud over IMAP (read-only), finds recent Fortuneo card
alerts, parses amount + merchant, and posts each one to the Worker's
ingest endpoint. The Worker dedupes on Message-ID, so this script is
stateless: it re-scans the last few days on every run and duplicates
are silently ignored server-side.

Required environment variables (set as GitHub Actions secrets):
  ICLOUD_EMAIL         your Apple ID email
  ICLOUD_APP_PASSWORD  app-specific password from appleid.apple.com
  WORKER_URL           e.g. https://spend-tracker.yourname.workers.dev
  API_TOKEN            same value as the Worker's API_TOKEN secret

The mailbox is opened READ-ONLY: nothing is marked read, moved, or
altered in any way.
"""

import email
import email.policy
import email.utils
import html
import imaplib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import detect

IMAP_HOST = "imap.mail.me.com"
LOOKBACK_DAYS = 4
try:
    PARIS_TZ = ZoneInfo("Europe/Paris")
except Exception:
    # No IANA tzdata on this system (e.g. some Windows Python installs) —
    # email_spent_at() degrades to None below.
    PARIS_TZ = None


def email_spent_at(msg) -> str | None:
    """Paris-local 'YYYY-MM-DD HH:MM:SS' from the email's Date header, so
    expenses keep their real transaction date even if the poller runs
    (or catches up on a backlog) hours or days later."""
    if PARIS_TZ is None:
        return None
    raw = msg["Date"]
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            return None
        return dt.astimezone(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # Malformed header etc. — fall back to the Worker's default
        # (ingest time) rather than failing the poll.
        return None

# ---------- Fortuneo card alerts ----------
AMOUNT_RE = re.compile(
    r"autorisation de paiement de\s*([0-9][0-9 ]*[.,][0-9]{2})\s*(?:€|EUR)",
    re.IGNORECASE,
)
MERCHANT_RE = re.compile(
    r"chez\s+(.{1,80}?)\s+a bien été acceptée", re.IGNORECASE
)

# ---------- Amazon order confirmations ----------
# "Total 24,99€" / "Total : EUR 24,99" — but never "Sous-total"
AMZ_TOTAL_RE = re.compile(
    r"(?<![Ss]ous-)Total(?:\s+de la commande)?\s*:?\s*(?:EUR\s*)?"
    r"([0-9][0-9 ]*[.,][0-9]{2})\s*(?:€|EUR)?",
)
AMZ_ORDER_RE = re.compile(r"commande\s*:?\s*(\d{3}-\d{7}-\d{7})", re.IGNORECASE)
AMZ_SUBJECT_RE = re.compile(r"«\s*(.+?)\s*»")

# ---------- PayPal payment receipts ----------
# "Vous avez payé 15,99 € EUR à Deezer."
PP_PAID_RE = re.compile(
    r"Vous avez payé\s*([0-9][0-9 ]*[.,][0-9]{2})\s*€(?:\s*EUR)?\s*à\s+(.{1,60}?)\s*\.",
    re.IGNORECASE,
)
PP_ORDER_RE = re.compile(r"commande\s*:?\s*([A-Za-z0-9_\-]{8,40})", re.IGNORECASE)


def flatten(msg) -> str:
    """Extract decoded text from all text/* parts, strip HTML, normalize."""
    chunks = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)  # handles QP + base64
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            chunks.append(payload.decode(charset, errors="replace"))
        except LookupError:
            chunks.append(payload.decode("utf-8", errors="replace"))
    text = "\n".join(chunks)
    text = re.sub(r"<[^>]+>", " ", text)          # strip tags
    text = html.unescape(text)                      # entities
    text = re.sub(r"[\u00A0\u202F\u2009]", " ", text)  # exotic spaces
    return re.sub(r"\s+", " ", text)


def parse_fortuneo(msg):
    """Return an expense dict for a Fortuneo payment alert, else None."""
    flat = flatten(msg)
    am = AMOUNT_RE.search(flat)
    if not am:
        return None
    amount = float(am.group(1).replace(" ", "").replace(",", "."))
    mm = MERCHANT_RE.search(flat)
    return {
        "amount": amount,
        "merchant": mm.group(1).strip() if mm else None,
        "source": "email",
    }


def parse_amazon(msg):
    """Return an expense dict for an Amazon order confirmation, else None."""
    flat = flatten(msg)
    am = AMZ_TOTAL_RE.search(flat)
    if not am:
        return None
    amount = float(am.group(1).replace(" ", "").replace(",", "."))

    subject = str(msg["Subject"] or "")
    pm = AMZ_SUBJECT_RE.search(subject)
    merchant = ("Amazon · " + pm.group(1))[:80] if pm else "Amazon"

    om = AMZ_ORDER_RE.search(flat)
    return {
        "amount": amount,
        "merchant": merchant,
        "source": "amazon",
        "category": "shopping",
        # order number is the perfect dedup key (confirmation may be resent)
        "order": om.group(1) if om else None,
    }


def parse_paypal(msg):
    """Return an expense dict for a PayPal 'Vous avez payé' receipt, else None."""
    flat = flatten(msg)
    m = PP_PAID_RE.search(flat)
    if not m:
        return None  # login alerts, money received, refunds, marketing…
    amount = float(m.group(1).replace(" ", "").replace(",", "."))
    merchant = ("PayPal · " + m.group(2).strip())[:80]

    om = PP_ORDER_RE.search(flat)
    return {
        "amount": amount,
        "merchant": merchant,
        "source": "paypal",
        "order": om.group(1) if om else None,
    }


# sender filter → parser. The poller searches each sender separately.
SOURCES = [
    ("contact.fortuneo.com", parse_fortuneo),
    ("confirmation-commande@amazon.fr", parse_amazon),
    ("service@paypal.fr", parse_paypal),
]
KNOWN_SENDERS = [s for s, _ in SOURCES]

# ---------- generalized merchant-email detection (any sender) ----------
# Bounded IMAP subject search: only the positive-signal phrase groups
# (never marketing/refund/cancellation/etc — those exist to let
# detect.classify_email() route emails AWAY from import, not to widen
# the search). Single source of truth is detect.SUBJECT_RULES so the
# search terms and the classifier's own phrases never drift apart.
GENERIC_SEARCH_SUBJECTS = sorted({
    phrase
    for cls in (
        detect.Classification.PURCHASE_CONFIRMATION,
        detect.Classification.PAYMENT_RECEIPT,
        detect.Classification.BOOKING,
        detect.Classification.SUBSCRIPTION_RENEWAL,
    )
    for phrase in detect.SUBJECT_RULES[cls]
})


def get_bodies(msg) -> tuple[str, str]:
    """Separate HTML and plain-text bodies (unlike flatten(), which is
    the crude regex-strip used by the three dedicated parsers above) —
    detect.py's html_to_text() needs real markup to find block-tag
    boundaries and image alt text."""
    html_parts, plain_parts = [], []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if ctype == "text/html":
            html_parts.append(text)
        elif ctype == "text/plain":
            plain_parts.append(text)
    return "\n".join(html_parts), "\n".join(plain_parts)


def process_generic(msg, uid, worker_url, token):
    """Second, additive detection pass for senders NOT covered by
    SOURCES above (e.g. La Poste). Only ever posts source='merchant' —
    never touches the amazon/paypal/email sources the dedicated parsers
    use, so their proven dedup/TWINS behavior can't regress."""
    subject = str(msg["Subject"] or "")
    from_name, from_email = email.utils.parseaddr(msg["From"] or "")
    body_html, body_plain = get_bodies(msg)

    result = detect.classify_email(
        subject=subject,
        sender_email=from_email,
        sender_name=from_name,
        body_html=body_html,
        body_plain=body_plain,
        header_date=msg["Date"],
    )

    if result.outcome == detect.Outcome.IGNORE:
        print(f"uid {uid.decode()}: ignored ({result.classification}, score={result.confidence})")
        return
    if not result.amount or not result.merchant:
        print(f"uid {uid.decode()}: {result.outcome} outcome but no amount/merchant extracted, skipping")
        return

    if result.date_source == "body_explicit_date" and result.order_date:
        spent_at = result.order_date + " 12:00:00"
    else:
        spent_at = email_spent_at(msg)

    review_status = "confirmed" if result.outcome == detect.Outcome.IMPORT else "pending_review"
    order_hash = (
        f"merchant-{result.order_ref}" if result.order_ref
        else (msg["Message-ID"] or f"icloud-uid-{uid.decode()}").strip()
    )

    payload = {
        "amount": result.amount,
        "merchant": result.merchant,
        "source": "merchant",
        "hash": order_hash,
        "paymentMethod": result.payment_method,
        "orderRef": result.order_ref,
        "reviewStatus": review_status,
        "confidence": result.confidence,
        "reason": "; ".join(result.reasons)[:500],
        **({"spentAt": spent_at} if spent_at else {}),
    }
    post_result = post_expense(worker_url, token, payload)
    state = (
        "duplicate/reconciled" if post_result.get("duplicate")
        else "queued for review" if post_result.get("pendingReview")
        else "NEW → pushed"
    )
    print(f"uid {uid.decode()}: {result.amount} € · {result.merchant} "
          f"[{review_status}, score={result.confidence}] — {state}")


def post_expense(worker_url, token, payload) -> dict:
    req = urllib.request.Request(
        worker_url.rstrip("/") + "/api?action=ingest",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Api-Token": token,
            # Cloudflare's Browser Integrity Check rejects Python's
            # default User-Agent with error 1010 — identify normally.
            "User-Agent": "Mozilla/5.0 (compatible; spend-tracker-poller/1.0)",
        },
        method="POST",
    )
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            print(f"Worker answered HTTP {e.code}", file=sys.stderr)
            print(f"Response body: {body}", file=sys.stderr)
            raise SystemExit(1)
        except (TimeoutError, urllib.error.URLError, OSError) as e:
            print(f"attempt {attempt}: no response ({e}), retrying...", file=sys.stderr)
            time.sleep(5 * attempt)
    print("Worker unreachable after 3 attempts", file=sys.stderr)
    raise SystemExit(1)


def main():
    user = os.environ["ICLOUD_EMAIL"]
    password = os.environ["ICLOUD_APP_PASSWORD"]
    worker_url = os.environ["WORKER_URL"]
    token = os.environ["API_TOKEN"]

    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime(
        "%d-%b-%Y"
    )

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)  # never alters the mailbox

        handled_uids: set[bytes] = set()

        for sender, parser in SOURCES:
            status, data = imap.uid("SEARCH", None, "FROM", sender, "SINCE", since)
            if status != "OK":
                print(f"SEARCH failed for {sender}: {status}", file=sys.stderr)
                continue

            uids = data[0].split() if data and data[0] else []
            print(f"[{sender}] {len(uids)} email(s) in the last {LOOKBACK_DAYS} days")

            for uid in uids:
                handled_uids.add(uid)
                status, fetched = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK" or not fetched or fetched[0] is None:
                    print(f"uid {uid.decode()}: fetch failed, skipping", file=sys.stderr)
                    continue

                msg = email.message_from_bytes(
                    fetched[0][1], policy=email.policy.default
                )
                parsed = parser(msg)
                if not parsed:
                    print(f"uid {uid.decode()}: not a relevant email, ignored")
                    continue

                order = parsed.pop("order", None)
                spent_at = email_spent_at(msg)
                payload = {
                    **parsed,
                    "hash": (
                        f"{parsed['source']}-{order}" if order
                        else (msg["Message-ID"] or f"icloud-uid-{uid.decode()}").strip()
                    ),
                    **({"spentAt": spent_at} if spent_at else {}),
                }
                result = post_expense(worker_url, token, payload)
                state = (
                    "duplicate (already recorded)" if result.get("duplicate")
                    else "NEW → pushed"
                )
                print(f"uid {uid.decode()}: {parsed['amount']} € · {parsed['merchant']} — {state}")

        # Second pass: any sender, bounded to positive-signal subject
        # keywords, for merchants with no dedicated parser (e.g. La
        # Poste). Split into one SEARCH per keyword — a single query
        # ORing dozens of terms risks exceeding IMAP command-length
        # limits on some servers.
        generic_uids: set[bytes] = set()
        for keyword in GENERIC_SEARCH_SUBJECTS:
            status, data = imap.uid("SEARCH", None, "SINCE", since, "SUBJECT", f'"{keyword}"')
            # iCloud's IMAP server sometimes answers OK with data[0]=None
            # instead of b'' when a particular SUBJECT query matches
            # nothing — observed live, not just a theoretical case.
            if status != "OK" or not data or not data[0]:
                continue
            generic_uids.update(data[0].split())

        generic_uids -= handled_uids
        print(f"[generic] {len(generic_uids)} candidate email(s) from unrecognized senders")

        for uid in generic_uids:
            status, fetched = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not fetched or fetched[0] is None:
                print(f"uid {uid.decode()}: fetch failed, skipping", file=sys.stderr)
                continue
            msg = email.message_from_bytes(fetched[0][1], policy=email.policy.default)
            from_email = email.utils.parseaddr(msg["From"] or "")[1].lower()
            if any(known in from_email for known in KNOWN_SENDERS):
                continue  # already covered by a dedicated parser above
            process_generic(msg, uid, worker_url, token)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
