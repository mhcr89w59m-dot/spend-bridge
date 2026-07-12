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
import html
import imaplib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

IMAP_HOST = "imap.mail.me.com"
LOOKBACK_DAYS = 4

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


# sender filter → parser. The poller searches each sender separately.
SOURCES = [
    ("contact.fortuneo.com", parse_fortuneo),
    ("confirmation-commande@amazon.fr", parse_amazon),
]


def post_expense(worker_url, token, payload) -> dict:
    req = urllib.request.Request(
        worker_url.rstrip("/") + "/api?action=ingest",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Api-Token": token,
            "User-Agent": "Mozilla/5.0 (compatible; spend-tracker-poller/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        print(f"Worker answered HTTP {e.code} at {req.full_url}", file=sys.stderr)
        print(f"Response body: {body}", file=sys.stderr)
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

        for sender, parser in SOURCES:
            status, data = imap.uid("SEARCH", None, "FROM", sender, "SINCE", since)
            if status != "OK":
                print(f"SEARCH failed for {sender}: {status}", file=sys.stderr)
                continue

            uids = data[0].split()
            print(f"[{sender}] {len(uids)} email(s) in the last {LOOKBACK_DAYS} days")

            for uid in uids:
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
                payload = {
                    **parsed,
                    "hash": (
                        f"amazon-{order}" if order
                        else (msg["Message-ID"] or f"icloud-uid-{uid.decode()}").strip()
                    ),
                }
                result = post_expense(worker_url, token, payload)
                state = (
                    "duplicate (already recorded)" if result.get("duplicate")
                    else "NEW → pushed"
                )
                print(f"uid {uid.decode()}: {parsed['amount']} € · {parsed['merchant']} — {state}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
