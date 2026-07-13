"""
Layered transactional-email detection — pure functions, no I/O, no
network, stdlib only. Mirrors this project's convention (src/parse.js,
src/balance.js) of keeping domain logic in a dependency-free module that
can be unit-tested directly, separate from poll_icloud.py's IMAP/HTTP
plumbing.

Scope: decide whether an email represents a COMPLETED purchase/payment
worth turning into a pocket-money expense, extract the merchant (the
company that sold the thing — never the payment method), the amount,
the payment method, an order/reference id, and a purchase date — then
score confidence so the caller can import, queue for review, or ignore.

Nothing here talks to a mailbox or a network. See poll_icloud.py for
the IMAP search + HTTP posting that calls into classify_email().
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Optional


# ============================================================
# text normalization
# ============================================================
def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def normalize(s: str) -> str:
    """Lowercase, accent-stripped, whitespace-collapsed — for MATCHING
    only. Extraction (amounts, merchant names) works on the original
    text so casing/accents in the actual value are preserved."""
    if not s:
        return ""
    s = strip_accents(s.lower())
    s = re.sub(r"[   ]", " ", s)  # exotic spaces
    s = re.sub(r"\s+", " ", s)
    return s.strip()


class _BlockHTMLParser(HTMLParser):
    """Converts HTML to text while treating block-level tags (tr/td/p/
    div/br/li/h1-6) as whitespace boundaries, so cells/lines don't run
    together (e.g. "Total TTC8,60 €" instead of "Total TTC 8,60 €").
    Skips script/style/head entirely. Deduplicates consecutive
    identical lines, which transactional HTML emails commonly produce
    (desktop + mobile-responsive sections repeating the same content)."""

    _BLOCK_TAGS = {
        "tr", "td", "th", "p", "div", "br", "li", "h1", "h2", "h3", "h4",
        "h5", "h6", "table", "hr",
    }
    _SKIP_TAGS = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")
        # alt text on images can carry a merchant logo's brand name
        if tag == "img" and self._skip_depth == 0:
            for k, v in attrs:
                if k == "alt" and v:
                    self._chunks.append(" " + v + " ")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(raw_html: str) -> str:
    """Semantic HTML -> normalized text. Strips scripts/styles/tracking
    pixels, preserves word boundaries at block tags, decodes entities,
    dedupes repeated lines, collapses whitespace."""
    if not raw_html:
        return ""
    parser = _BlockHTMLParser()
    try:
        parser.feed(raw_html)
    except Exception:
        # malformed markup — fall back to a crude tag strip rather than
        # losing the email entirely
        return normalize_whitespace(html.unescape(re.sub(r"<[^>]+>", " ", raw_html)))
    text = parser.text()
    text = html.unescape(text)
    text = re.sub(r"[   ]", " ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    lines = _dedupe_lines(lines)
    lines = _dedupe_repeated_block(lines)
    return normalize_whitespace(" \n".join(lines))


def _dedupe_lines(lines: list[str]) -> list[str]:
    """Drops consecutive identical single lines."""
    deduped = []
    for ln in lines:
        if not deduped or deduped[-1] != ln:
            deduped.append(ln)
    return deduped


def _dedupe_repeated_block(lines: list[str]) -> list[str]:
    """Responsive HTML emails commonly repeat a whole multi-line section
    verbatim (e.g. a desktop table and a mobile-stacked div with the
    same "Montant total TTC" / "8,60 EUR" pair) -- single-line dedup
    above doesn't catch that because the lines alternate rather than
    repeat back-to-back. Detects the largest block B such that lines
    starts with [B, B, ...] and collapses it to one copy."""
    n = len(lines)
    if n < 4:
        return lines
    for block_len in range(n // 2, 1, -1):
        if lines[:block_len] == lines[block_len:block_len * 2]:
            return lines[:block_len] + lines[block_len * 2:]
    return lines


def normalize_whitespace(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s).strip()


def body_to_text(body_html: Optional[str], body_plain: Optional[str]) -> str:
    """Plain-text fallback when HTML is unavailable, per spec."""
    if body_html:
        return html_to_text(body_html)
    return normalize_whitespace(body_plain or "")


# ============================================================
# signal group 1: subject classification
# ============================================================
class Classification:
    PURCHASE_CONFIRMATION = "purchase_confirmation"
    PAYMENT_RECEIPT = "payment_receipt"
    INVOICE_PAID = "invoice_paid"
    BOOKING = "booking"
    SUBSCRIPTION_RENEWAL = "subscription_renewal"
    REFUND = "refund"
    CANCELLATION = "cancellation"
    SHIPPING_UPDATE = "shipping_update"
    MARKETING = "marketing"
    PAYMENT_REQUEST = "payment_request"
    PAYMENT_FAILED = "payment_failed"
    UNKNOWN = "unknown"


# Each entry: normalized phrase fragment. Matched as a substring of the
# normalized (lowercased, accent-stripped) subject/body. FR + EN grouped
# and documented so more languages can be added without touching the
# scoring logic below.
SUBJECT_RULES: dict[str, list[str]] = {
    Classification.PURCHASE_CONFIRMATION: [
        # French
        "confirmation de commande", "commande confirmee", "votre commande",
        "recapitulatif de commande", "recu de paiement", "confirmation de paiement",
        "paiement confirme", "achat confirme", "expedition de votre commande",
        # English
        "order confirmation", "order confirmed", "your order",
        "purchase confirmation", "payment confirmation", "payment received",
        "thank you for your purchase", "thanks for your order",
    ],
    Classification.PAYMENT_RECEIPT: [
        "recu", "receipt", "facture", "votre facture", "invoice",
    ],
    Classification.BOOKING: [
        "billet", "reservation confirmee", "confirmation de reservation",
        "inscription confirmee", "booking confirmation", "reservation confirmed",
        "ticket confirmation",
    ],
    Classification.SUBSCRIPTION_RENEWAL: [
        "renouvellement", "prelevement", "subscription renewed", "renewal confirmation",
    ],
    Classification.REFUND: [
        "remboursement", "rembourse", "refund", "refunded", "avoir emis",
    ],
    Classification.CANCELLATION: [
        "commande annulee", "annulation", "order cancelled", "order canceled",
        "cancellation",
    ],
    Classification.SHIPPING_UPDATE: [
        "expedie", "en cours de livraison", "colis", "shipped", "on its way",
        "out for delivery", "delivery update", "tracking",
    ],
    Classification.MARKETING: [
        "panier", "votre panier vous attend", "finalisez votre commande",
        "profitez de", "offre speciale", "-50%", "soldes",
        "your basket is waiting", "complete your order", "don't miss",
        "special offer", "sale", "% off",
    ],
    Classification.PAYMENT_REQUEST: [
        "facture impayee", "reste a payer", "merci de regler",
        "unpaid invoice", "payment due", "please pay", "outstanding balance",
    ],
    Classification.PAYMENT_FAILED: [
        "paiement refuse", "paiement echoue", "echec du paiement",
        "payment failed", "payment declined", "declined",
    ],
}

_SUBJECT_NOISE_RE = re.compile(
    r"^(re|fw|fwd|tr)\s*:\s*", re.IGNORECASE
)
_ORDER_NUM_IN_SUBJECT_RE = re.compile(r"[#°]?\s*[\dA-Z][\dA-Z\-]{5,}")


def normalize_subject(subject: str) -> str:
    """Strips RE:/FW:/TR: prefixes and order/reference numbers so
    keyword matching isn't thrown off by "RE: Commande #123-4567890"."""
    s = subject or ""
    while True:
        new_s = _SUBJECT_NOISE_RE.sub("", s)
        if new_s == s:
            break
        s = new_s
    s = normalize(s)
    s = _ORDER_NUM_IN_SUBJECT_RE.sub(" ", s)
    return normalize_whitespace(s)


def classify_subject(subject: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (classification, matched_phrase) for the first matching
    rule group, prioritized in SUBJECT_RULES's own order (purchase/
    payment signals first, exclusions/negatives last) so e.g. a subject
    containing both "commande" and a marketing phrase still classifies
    as marketing (checked in a second pass by the caller — see
    classify_email, which checks MARKETING/negatives regardless of
    order to avoid this ambiguity)."""
    norm = normalize_subject(subject)
    for cls, phrases in SUBJECT_RULES.items():
        for phrase in phrases:
            if phrase in norm:
                return cls, phrase
    return None, None


# ============================================================
# signal group 2: body phrases
# ============================================================
BODY_PHRASES: dict[str, list[str]] = {
    "total_label": [
        "votre commande est confirmee", "votre commande a bien ete enregistree",
        "montant total", "total ttc", "total paye", "paiement realise",
        "recapitulatif de votre commande", "your order is confirmed",
        "order total", "total paid", "amount paid", "order summary",
    ],
    "payment_method_label": ["paye par", "mode de paiement", "paid with", "payment method"],
    "order_ref_label": [
        "numero de commande", "numero de reservation", "confirmation number",
        "booking reference", "invoice total",
    ],
    "marketing": [
        "panier", "votre panier vous attend", "finalisez votre commande",
        "your basket is waiting", "complete your order", "special offer",
    ],
    "cart_abandon": [
        "votre panier vous attend", "finalisez votre commande",
        "your basket is waiting", "complete your order", "you left something",
    ],
    "refund": ["rembourse", "remboursement", "refunded", "refund issued", "avoir"],
    "cancellation": ["commande annulee", "annulee", "cancelled", "canceled"],
    "shipping_only": [
        "a ete expedie", "en cours de livraison", "votre colis", "has shipped",
        "is on its way", "out for delivery", "suivi de livraison", "tracking number",
    ],
    "payment_failed": [
        "paiement refuse", "paiement echoue", "payment failed", "payment declined",
        "carte refusee", "card declined",
    ],
    "unpaid": [
        "reste a payer", "merci de regler", "payment due", "please pay",
        "outstanding balance", "en attente de paiement",
    ],
}


def find_body_signals(text: str) -> dict[str, list[str]]:
    norm = normalize(text)
    found: dict[str, list[str]] = {}
    for key, phrases in BODY_PHRASES.items():
        matches = [p for p in phrases if p in norm]
        if matches:
            found[key] = matches
    return found


# ============================================================
# signal group 3: monetary amount
# ============================================================
_MONEY_NUM = r"([0-9][0-9  ]*[.,][0-9]{2})"
_CURRENCY = r"(?:€|EUR|USD|\$|£|GBP)"
# A money token: number then currency, OR currency then number (€8.60 / EUR 8.60)
_MONEY_RE = re.compile(
    rf"(?:{_MONEY_NUM}\s*{_CURRENCY})|(?:{_CURRENCY}\s*{_MONEY_NUM})",
    re.IGNORECASE,
)

# Labels that indicate the TOTAL actually charged — checked in priority
# order. Amounts near an earlier label win over amounts near a later one.
AMOUNT_LABELS_POSITIVE = [
    ("montant total ttc", 100),
    ("total ttc", 95),
    ("total paye", 95),
    ("montant total", 90),
    ("total charged", 90),
    ("amount paid", 90),
    ("total paid", 90),
    ("grand total", 85),
    ("order total", 80),
    ("total :", 40),
    ("total", 35),
]
# Labels whose nearby amount must NOT be chosen as the purchase total.
AMOUNT_LABELS_NEGATIVE = [
    "sous-total", "sous total", "subtotal", "tva", "vat", "livraison",
    "frais de port", "shipping", "remise", "discount", "reduction",
    "solde precedent", "previous balance", "points fidelite", "loyalty points",
    "plafond", "credit limit", "limite de credit", "unit price", "prix unitaire",
]


def _extract_money(match_text: str) -> Optional[tuple[float, str]]:
    m = _MONEY_RE.search(match_text)
    if not m:
        return None
    num = m.group(1) or m.group(2)
    raw = match_text.lower()
    currency = "EUR"
    if "$" in raw or "usd" in raw:
        currency = "USD"
    elif "£" in raw or "gbp" in raw:
        currency = "GBP"
    num = num.replace(" ", "").replace(" ", "")
    # thousands/decimal: "1,234.56" (USD-style) vs "1 234,56" / "8,60" (FR-style)
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    else:
        num = num.replace(",", ".")
    try:
        return float(num), currency
    except ValueError:
        return None


def extract_amount(text: str) -> Optional[dict]:
    """Finds the labeled purchase total, preferring amounts near strong
    labels (Total TTC, Total paid, ...) over unlabeled or negatively-
    labeled ones (subtotal, VAT, shipping, discount, ...). Returns
    {"amount", "currency", "label"} or None."""
    norm_full = normalize(text)
    candidates = []  # (priority, position, amount, currency, label)

    for label, priority in AMOUNT_LABELS_POSITIVE:
        for lm in re.finditer(re.escape(label), norm_full):
            # look at the ORIGINAL text in a window right after the label
            window_start = lm.end()
            window = text[window_start: window_start + 40] if len(text) >= window_start else ""
            # window is sliced from `text`, but positions came from
            # `norm_full` — normalize() doesn't change length materially
            # for ASCII digits/currency symbols, so this window is a
            # reasonable approximation; re-search within a slightly
            # wider slice of the ORIGINAL text around the same offset
            # to stay robust against minor offset drift.
            lo = max(0, window_start - 5)
            hi = min(len(text), window_start + 60)
            window = text[lo:hi]
            found = _extract_money(window)
            if not found:
                continue
            amount, currency = found
            # reject if a negative label is closer to this amount than
            # our positive label was (e.g. "Sous-total ... Total TTC 8,60"
            # where the negative label also has a nearby number)
            neg_nearby = any(neg in norm_full[max(0, lm.start() - 20):lm.start()] for neg in AMOUNT_LABELS_NEGATIVE)
            if neg_nearby:
                continue
            candidates.append((priority, lm.start(), amount, currency, label))

    if not candidates:
        # fall back to any money token at all, but never one immediately
        # preceded by a negative label
        for m in _MONEY_RE.finditer(text):
            lo = max(0, m.start() - 25)
            preceding = normalize(text[lo:m.start()])
            if any(neg in preceding for neg in AMOUNT_LABELS_NEGATIVE):
                continue
            found = _extract_money(m.group(0))
            if found:
                amount, currency = found
                candidates.append((10, m.start(), amount, currency, "unlabeled"))

    if not candidates:
        return None

    candidates.sort(key=lambda c: (-c[0], c[1]))
    priority, _, amount, currency, label = candidates[0]
    return {"amount": amount, "currency": currency, "label": label}


# ============================================================
# signal group 4: order / reference identifier
# ============================================================
_ORDER_REF_PATTERNS = [
    re.compile(r"numero de commande\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{3,40})", re.IGNORECASE),
    re.compile(r"commande\s*n[°o]?\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{3,40})", re.IGNORECASE),
    re.compile(r"numero de reservation\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{3,40})", re.IGNORECASE),
    re.compile(r"order\s*(?:number|#|no\.?)\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{3,40})", re.IGNORECASE),
    re.compile(r"booking reference\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{3,40})", re.IGNORECASE),
    re.compile(r"confirmation number\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{3,40})", re.IGNORECASE),
    re.compile(r"invoice\s*(?:number|#)\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{3,40})", re.IGNORECASE),
    re.compile(r"receipt\s*(?:number|#)\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{3,40})", re.IGNORECASE),
    re.compile(r"transaction\s*id\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-\/]{3,40})", re.IGNORECASE),
]


def extract_order_ref(text: str) -> Optional[str]:
    for pat in _ORDER_REF_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return None


# ============================================================
# payment method
# ============================================================
_PAYMENT_METHOD_PATTERNS = [
    ("Apple Pay", re.compile(r"apple\s*pay", re.IGNORECASE)),
    ("Google Pay", re.compile(r"google\s*pay", re.IGNORECASE)),
    ("PayPal", re.compile(r"paypal", re.IGNORECASE)),
    ("Visa", re.compile(r"\bvisa\b", re.IGNORECASE)),
    ("Mastercard", re.compile(r"master\s*card", re.IGNORECASE)),
    ("American Express", re.compile(r"american express|\bamex\b", re.IGNORECASE)),
    ("Direct debit", re.compile(r"prelevement|direct debit", re.IGNORECASE)),
    ("Bank transfer", re.compile(r"virement|bank transfer|wire transfer", re.IGNORECASE)),
    ("Gift card", re.compile(r"carte cadeau|gift card", re.IGNORECASE)),
    ("Bank card", re.compile(r"carte bancaire|\bcb\b|bank card|credit card|debit card", re.IGNORECASE)),
]


def extract_payment_method(text: str) -> Optional[str]:
    for name, pat in _PAYMENT_METHOD_PATTERNS:
        if pat.search(text):
            return name
    return None


# ============================================================
# signal group 5 + merchant extraction
# ============================================================
# Known aliases normalize to a canonical display name. Matched against
# the normalized (lowercased, accent/punctuation-stripped) candidate.
MERCHANT_ALIASES: dict[str, str] = {
    "la poste": "La Poste",
    "laposte": "La Poste",
    "amazon": "Amazon",
    "sncf": "SNCF",
    "sncf connect": "SNCF Connect",
    "booking com": "Booking.com",
    "paypal": "PayPal",
    "stripe": "Stripe",
    "apple": "Apple",
}

_SENDER_NOISE_RE = re.compile(
    r"\b(no[\s\-]?reply|noreply|notifications?|notif|support|info|contact|hello|team|mail)\b",
    re.IGNORECASE,
)
_SELLER_LABEL_RE = re.compile(
    r"(?:vendu par|sold by|seller)\s*:?\s*([A-Za-z0-9][\w &\-\.]{1,60})", re.IGNORECASE
)


def clean_merchant_name(raw: str) -> str:
    """Strips noreply/notifications/support/etc noise and tidies
    whitespace/casing — does not alias-normalize (see normalize_merchant)."""
    s = _SENDER_NOISE_RE.sub(" ", raw)
    s = re.sub(r"[_\.]+", " ", s)
    s = normalize_whitespace(s)
    return s


def normalize_merchant(name: str) -> str:
    """Applies the alias table; falls back to title-casing an unknown
    name (with a small set of words kept lowercase, e.g. "de"/"of")."""
    key = normalize(clean_merchant_name(name))
    key = re.sub(r"[^\w ]", "", key).strip()
    if key in MERCHANT_ALIASES:
        return MERCHANT_ALIASES[key]
    # also try matching as a prefix (e.g. "la poste france" -> "la poste")
    for alias_key, canonical in MERCHANT_ALIASES.items():
        if key.startswith(alias_key):
            return canonical
    cleaned = clean_merchant_name(name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -.")
    if not cleaned:
        return "Unknown merchant"
    small_words = {"de", "du", "des", "la", "le", "of", "and", "et"}
    words = cleaned.split(" ")
    titled = [
        w if w.lower() in small_words and i > 0 else w[:1].upper() + w[1:]
        for i, w in enumerate(words)
    ]
    return " ".join(titled)[:80]


def domain_to_merchant(domain: str) -> str:
    """notif.laposte.fr -> La Poste (via alias) / unknownshop.com ->
    Unknownshop (title-cased root label, no alias)."""
    if not domain:
        return "Unknown merchant"
    parts = domain.lower().split(".")
    # drop common TLD/ccTLD and subdomain noise, keep the registrable label
    labels = [p for p in parts if p not in ("www", "notif", "notifications", "mail", "email", "shop", "fr", "com", "co", "uk", "net", "org", "io")]
    root = labels[-1] if labels else parts[0]
    return normalize_merchant(root)


def extract_merchant(
    body_text: str, subject: str, sender_name: Optional[str], sender_domain: str
) -> tuple[str, str]:
    """Priority: 1) "Vendu par"/"Sold by" seller label in body, 2)
    sender display name, 3) root domain, per the spec's priority order.
    (Order-heading/logo-alt-text and alias-mapping are folded into the
    body/domain steps: alt text is already inlined into body_text by
    html_to_text(), and alias mapping is applied at every step via
    normalize_merchant().) Returns (merchant, extraction_source)."""
    m = _SELLER_LABEL_RE.search(body_text)
    if m:
        return normalize_merchant(m.group(1)), "body_seller_label"
    if sender_name and clean_merchant_name(sender_name):
        return normalize_merchant(sender_name), "sender_display_name"
    return domain_to_merchant(sender_domain), "sender_domain"


# ============================================================
# date extraction
# ============================================================
_FR_MONTHS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
}
_DATE_LABEL_RE = re.compile(
    r"(?:date de commande|order date|date d'achat|purchase date|paye le|paid on)\s*:?\s*"
    r"(\d{1,2})[\/\.\-](\d{1,2})[\/\.\-](\d{2,4})",
    re.IGNORECASE,
)
_DATE_DMY_RE = re.compile(r"\b(\d{1,2})[\/\.\-](\d{1,2})[\/\.\-](\d{2,4})\b")
_DATE_FR_TEXT_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_FR_MONTHS.keys()) + r")\s+(\d{4})\b", re.IGNORECASE
)
# things that look like dates but usually aren't the purchase date
_DATE_EXCLUSION_WINDOW = [
    "livraison", "delivery", "arrivee", "arrival", "expire", "due date",
    "echeance", "fin d'abonnement", "subscription end",
]


def extract_order_date(text: str) -> Optional[str]:
    """Returns YYYY-MM-DD from an explicit order/payment date label,
    skipping matches that fall inside a delivery/due-date/expiry
    context. None if nothing trustworthy is found (caller falls back to
    the email header date, then received time)."""
    m = _DATE_LABEL_RE.search(text)
    if m:
        d, mo, y = m.groups()
        return _normalize_dmy(d, mo, y)

    for m in _DATE_DMY_RE.finditer(text):
        lo = max(0, m.start() - 30)
        context = normalize(text[lo:m.start()])
        if any(x in context for x in _DATE_EXCLUSION_WINDOW):
            continue
        d, mo, y = m.groups()
        result = _normalize_dmy(d, mo, y)
        if result:
            return result

    m = _DATE_FR_TEXT_RE.search(text)
    if m:
        d, month_name, y = m.groups()
        mo = _FR_MONTHS.get(strip_accents(month_name.lower()))
        if mo:
            return _normalize_dmy(d, str(mo), y)
    return None


def _normalize_dmy(d: str, mo: str, y: str) -> Optional[str]:
    try:
        day, month, year = int(d), int(mo), int(y)
        if year < 100:
            year += 2000
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"
    except ValueError:
        return None


# ============================================================
# confidence scoring
# ============================================================
class Weight:
    """Named constants — see module docstring; adapted from the spec's
    suggested weights after reviewing what signals this codebase can
    actually extract reliably."""
    STRONG_SUBJECT = 30
    TOTAL_BODY_PHRASE = 25
    LABELED_AMOUNT = 25
    ORDER_REF_FOUND = 10
    PAYMENT_METHOD_FOUND = 5
    TRANSACTIONAL_SENDER = 10
    MARKETING_LANGUAGE = -25
    CART_ABANDON = -40
    SHIPPING_NO_TOTAL = -20


HIGH_CONFIDENCE_THRESHOLD = 60
MEDIUM_CONFIDENCE_THRESHOLD = 35


class Outcome:
    IMPORT = "import"
    REVIEW = "review"
    IGNORE = "ignore"


@dataclass
class DetectionResult:
    outcome: str
    classification: str
    confidence: int
    amount: Optional[float] = None
    currency: Optional[str] = None
    amount_label: Optional[str] = None
    merchant: Optional[str] = None
    merchant_source: Optional[str] = None
    payment_method: Optional[str] = None
    order_ref: Optional[str] = None
    order_date: Optional[str] = None
    date_source: Optional[str] = None
    matched_subject_rule: Optional[str] = None
    matched_body_indicators: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


# senders whose domain alone is a mild positive signal (transactional
# infrastructure / marketplaces / travel / ticketing / payment
# processors) — NOT sufficient alone per spec, just one input to scoring
_TRANSACTIONAL_SENDER_HINTS = [
    "notif.", "notification", "amazon.", "sncf-connect.com", "booking.com",
    "paypal.com", "stripe.com", "apple.com", "laposte.fr",
]


def classify_email(
    subject: str,
    sender_email: str,
    sender_name: Optional[str] = None,
    body_html: Optional[str] = None,
    body_plain: Optional[str] = None,
    header_date: Optional[str] = None,
) -> DetectionResult:
    """The single entry point: classify + extract + score. Never raises
    on malformed input — worst case returns Outcome.IGNORE with an
    explanatory reason, since a poller that crashes on one weird email
    would stop processing everything after it."""
    reasons: list[str] = []
    text = body_to_text(body_html, body_plain)
    sender_domain = sender_email.split("@")[-1].lower() if "@" in (sender_email or "") else ""

    subj_cls, subj_rule = classify_subject(subject or "")
    body_signals = find_body_signals(text)

    # Hard routes: these never become a new positive expense regardless
    # of score, per spec ("route to a different classification").
    if subj_cls == Classification.REFUND or "refund" in body_signals:
        return DetectionResult(Outcome.IGNORE, Classification.REFUND, 0,
                                reasons=["refund/credit — not a new expense"])
    if subj_cls == Classification.CANCELLATION or "cancellation" in body_signals:
        return DetectionResult(Outcome.IGNORE, Classification.CANCELLATION, 0,
                                reasons=["order cancelled"])
    if subj_cls == Classification.PAYMENT_FAILED or "payment_failed" in body_signals:
        return DetectionResult(Outcome.IGNORE, Classification.PAYMENT_FAILED, 0,
                                reasons=["payment failed/declined — nothing was actually charged"])
    if subj_cls == Classification.PAYMENT_REQUEST or "unpaid" in body_signals:
        return DetectionResult(Outcome.IGNORE, Classification.PAYMENT_REQUEST, 0,
                                reasons=["unpaid invoice / payment request, not a completed payment"])

    score = 0
    classification = subj_cls or Classification.UNKNOWN

    if subj_cls in (
        Classification.PURCHASE_CONFIRMATION, Classification.PAYMENT_RECEIPT,
        Classification.BOOKING, Classification.SUBSCRIPTION_RENEWAL,
    ):
        score += Weight.STRONG_SUBJECT
        reasons.append(f"subject matched '{subj_rule}' ({classification})")

    if "total_label" in body_signals:
        score += Weight.TOTAL_BODY_PHRASE
        reasons.append("body contains a total/payment-confirmed phrase")

    amount_info = extract_amount(text)
    if amount_info:
        score += Weight.LABELED_AMOUNT
        reasons.append(f"amount {amount_info['amount']} {amount_info['currency']} (label: {amount_info['label']})")

    order_ref = extract_order_ref(text)
    if order_ref:
        score += Weight.ORDER_REF_FOUND
        reasons.append(f"order/reference id found: {order_ref}")

    payment_method = extract_payment_method(text)
    if payment_method:
        score += Weight.PAYMENT_METHOD_FOUND
        reasons.append(f"payment method: {payment_method}")

    if any(hint in sender_domain for hint in _TRANSACTIONAL_SENDER_HINTS):
        score += Weight.TRANSACTIONAL_SENDER
        reasons.append(f"transactional sender domain: {sender_domain}")

    is_marketing = subj_cls == Classification.MARKETING or "marketing" in body_signals
    is_cart_abandon = "cart_abandon" in body_signals
    is_shipping_only = (
        (subj_cls == Classification.SHIPPING_UPDATE or "shipping_only" in body_signals)
        and not amount_info
    )

    if is_cart_abandon:
        score += Weight.CART_ABANDON
        classification = Classification.MARKETING
        reasons.append("cart-abandonment language")
    elif is_marketing:
        score += Weight.MARKETING_LANGUAGE
        classification = Classification.MARKETING
        reasons.append("marketing language dominates")

    if is_shipping_only:
        score += Weight.SHIPPING_NO_TOTAL
        classification = Classification.SHIPPING_UPDATE
        reasons.append("shipping/delivery update with no payment total")

    if not amount_info:
        # Without a plausible amount, we can't record an expense at all,
        # regardless of how "purchase-y" the subject sounds — per spec,
        # require a plausible monetary amount in most cases.
        score = min(score, MEDIUM_CONFIDENCE_THRESHOLD - 1)
        reasons.append("no plausible total amount found — capped below auto-import")

    if score >= HIGH_CONFIDENCE_THRESHOLD and amount_info:
        outcome = Outcome.IMPORT
    elif score >= MEDIUM_CONFIDENCE_THRESHOLD:
        outcome = Outcome.REVIEW
    else:
        outcome = Outcome.IGNORE

    merchant, merchant_source = extract_merchant(text, subject or "", sender_name, sender_domain)
    order_date, date_source = None, None
    explicit_date = extract_order_date(text)
    if explicit_date:
        order_date, date_source = explicit_date, "body_explicit_date"
    elif header_date:
        order_date, date_source = header_date, "email_header_date"

    return DetectionResult(
        outcome=outcome,
        classification=classification,
        confidence=score,
        amount=amount_info["amount"] if amount_info else None,
        currency=amount_info["currency"] if amount_info else None,
        amount_label=amount_info["label"] if amount_info else None,
        merchant=merchant if outcome != Outcome.IGNORE else None,
        merchant_source=merchant_source if outcome != Outcome.IGNORE else None,
        payment_method=payment_method,
        order_ref=order_ref,
        order_date=order_date,
        date_source=date_source,
        matched_subject_rule=subj_rule,
        matched_body_indicators=list(body_signals.keys()),
        reasons=reasons,
    )
