"""upi_extractor.py - structured fields from a UPI payment-confirmation screenshot (Google Pay layout).

Input : OCR detections [{text, conf, x0, y0, x1, y1, h}]  (same format as ocr_cache.json)
Output: record dict (see extract_upi). Pure Python, engine-agnostic.
Design rules: never trust the rupee symbol (OCR drops/garbles it), use redundancy
(big amount + "Payment of X completed"), take the full merchant name from the detail
block when the header is truncated, and refuse to record failed payments.
"""
import re
from decimal import Decimal, InvalidOperation

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

AMT_ONLY  = re.compile(r"^\s*[^\d\s]{0,3}\s*(\d[\d,]*(?:\.\d{1,2})?)\s*$")        # "₹1,660" / "?160" / "226.18"
SENT_PAID = re.compile(r"paymentof\W{0,3}(\d[\d,]*(?:\.\d{1,2})?)completed")      # on whitespace-free text
SENT_FAIL = re.compile(r"\W{0,3}(\d[\d,]*(?:\.\d{1,2})?)notdebited")
DATE_RE   = re.compile(r"(\d{1,2})\s*([A-Za-z0]{3,5})\.?\s*(\d{4})\s*,?\s*(\d{1,2})\s*[:.]\s*(\d{2})\s*([ap])\.?\s*m", re.I)
HEADER_RE  = re.compile(r"^\s*(to|from)\s+(\S.*)$", re.I)
HEADER_RE2 = re.compile(r"^\s*(To|From)([A-Z0-9][A-Za-z0-9 &.'\-]*)$")
DETAIL_TO   = re.compile(r"^\s*to\s*:\s*(.*)$", re.I)
DETAIL_FROM = re.compile(r"^\s*from\s*:\s*(.*)$", re.I)
TXN_LABEL = re.compile(r"up[il1]transaction")
ID_FIX = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"})


def _c(s):            return re.sub(r"\s+", "", s.lower())
def _norm(s):         return re.sub(r"[^a-z0-9]", "", s.lower())

def _dec(s):
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None

def _fmt(d):
    return str(int(d)) if d == d.to_integral_value() else format(d.quantize(Decimal("0.01")), "f")


def _mon(tok):
    return MONTHS.get(tok.lower().replace("0", "o")[:3])


# ------------------------------------------------------------ merchant helpers
_CONF = {"$": "s", "0": "o", "1": "i", "l": "i", "|": "i", "5": "s"}      # OCR look-alikes, for comparing only

def _keep(ch):
    return ch.isalnum() or ch in "$|"

def _cnorm(s):
    return "".join(_CONF.get(ch, ch) for ch in s.lower() if _keep(ch))

def _tail_after(s, k):
    """Original text after the first k comparable characters."""
    cnt = 0
    for i, ch in enumerate(s):
        if _keep(ch):
            cnt += 1
            if cnt == k:
                return s[i + 1:].strip()
    return ""

def _head_before(s, k):
    """Original text without its last k comparable characters."""
    target = sum(_keep(ch) for ch in s) - k
    if target <= 0:
        return ""
    cnt = 0
    for i, ch in enumerate(s):
        if _keep(ch):
            cnt += 1
            if cnt == target:
                return s[:i + 1].strip()
    return ""

def fix_confusables(name):
    """OCR confuses l/I and 0/O. Inside mostly-uppercase words, force the uppercase reading."""
    out = []
    for tok in name.split(" "):
        letters = [c for c in tok if c.isalpha()]
        if len(letters) >= 2 and sum(c.isupper() for c in letters) / len(letters) >= 0.5:
            tok = tok.replace("l", "I")
            tok = re.sub(r"(?<=[A-Z])0|0(?=[A-Z])", "O", tok)
        out.append(tok)
    return " ".join(out)

def merge_names(header, detail):
    """The app truncates long names in the header ("TATA 1MG ... SOLUTIONS ..").
    Rebuild the full name from the detail block. Comparison ignores spaces and OCR look-alikes
    ($/S, l/I/1, 0/O); the detail block's own characters win inside the overlap."""
    hp = re.sub(r"[\s.:\u2026_]+$", "", header.strip())        # OCR often reads the ellipsis as ':'
    if not detail:
        return hp
    trunc = bool(re.search(r"(\.{2,}|\u2026|:)\s*$", header))
    nh, nd = _cnorm(hp), _cnorm(detail)
    if nh == nd:
        return hp
    if nd.startswith(nh) and len(nd) > len(nh):
        return detail.strip()
    for k in range(min(len(nh), len(nd)) - 1, 5, -1):           # header ends with the start of the detail
        if nh.endswith(nd[:k]):
            return (_head_before(hp, k) + " " + detail.strip()).strip()
    return detail.strip() if trunc else hp


CATEGORY_KEYWORDS = [
    ("entertainment", ["riot", "valve", "steam", "games", "gaming", "googleplay", "netflix", "spotify", "hotstar",
                       "bookmyshow", "pvr", "inox", "primevideo", "playstation", "xbox", "epicgames"]),
    ("health",        ["pharma", "medical", "medic", "netmeds", "apollo", "hospital", "clinic", "diagnostic",
                       "chemist", "healthcare", "1mg"]),
    ("travel",        ["fillingstation", "servicestation", "fuel", "petrol", "bpcl", "hpcl", "iocl", "indianoil",
                       "uber", "rapido", "irctc", "redbus", "metro", "fastag", "parking", "toll"]),
    ("bills",         ["recharge", "prepaid", "postpaid", "airtel", "jio", "vodafone", "bsnl", "electricity", "bescom",
                       "broadband", "billdesk", "dth", "tatasky", "insurance", "tuition", "university", "college"]),
    ("food",          ["zomato", "swiggy", "bikanervala", "bakers", "bakery", "namkeen", "sweets", "restaurant", "cafe",
                       "dhaba", "pizza", "burger", "kitchen", "haldiram", "biryani", "chai", "coffee", "food"]),
    ("groceries",     ["blinkit", "zepto", "bigbasket", "instamart", "departmental", "store", "mart", "kirana",
                       "supermarket", "grocery", "provision", "dmart"]),
    ("shopping",      ["amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa", "decathlon", "lifestyle", "mall"]),
]
_SHORT = {"jio", "dth", "1mg", "ola", "hp", "vi"}

def categorise_merchant(name):
    comp = _norm(name)
    tokens = {_norm(t) for t in name.split()}
    for cat, kws in CATEGORY_KEYWORDS:
        for kw in kws:
            if (kw in tokens) if kw in _SHORT else (kw in comp):
                return cat
    return "misc"


# ------------------------------------------------------------ date / time
DATE2 = re.compile(r"(\d{1,2})\s*([A-Za-z0]{3,5})\.?\s*(20\d\d)")
TIME2 = re.compile(r"([0-9OolI|][0-9OolI|:;.*'\s]{1,6}?)\s*([ap])\.?\s*m", re.I)
TIME_FIX = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1"})

def parse_datetime(text):
    """'25 Jul 2026, 12.58pm' / '28 Sept 2026, 11:1Oam' / '6 0ct 2026, 111pm' -> '2026-07-25T12:58'.
    The colon is unreliable in OCR, so the time is read as 3-4 digits (H MM / HH MM)."""
    m = DATE2.search(text)
    if not m or not _mon(m.group(2)):
        return None
    day, mon, year = int(m.group(1)), _mon(m.group(2)), int(m.group(3))
    t = TIME2.search(text, m.end())
    if not t:
        return None
    digits = re.sub(r"\D", "", t.group(1).translate(TIME_FIX))
    if len(digits) == 4:
        hh, mm = int(digits[:2]), int(digits[2:])
    elif len(digits) == 3:
        hh, mm = int(digits[0]), int(digits[1:])
    else:
        return None
    if not (1 <= day <= 31 and 1 <= hh <= 12 and 0 <= mm <= 59):
        return None
    hh = hh % 12 + (12 if t.group(2).lower() == "p" else 0)
    return f"{year:04d}-{mon:02d}-{day:02d}T{hh:02d}:{mm:02d}"

def _row(ds, d):
    yc = (d["y0"] + d["y1"]) / 2
    band = [e for e in ds if abs((e["y0"] + e["y1"]) / 2 - yc) <= 0.6 * max(d["h"], e["h"])]
    return sorted(band, key=lambda e: e["x0"])


# ------------------------------------------------------------ main
def extract_upi(dets):
    flags, notes = [], []
    if not dets:
        return {"kind": "rejected", "reason": "no_text", "flags": ["no_text"], "recordable": False}
    ds = sorted(dets, key=lambda d: d["y0"])
    maxy = max(d["y1"] for d in ds)
    full = _c(" ".join(d["text"] for d in ds))
    if looks_like_paytm(full):
        return extract_paytm(ds, full)
    template = "gpay" if ("googletransactionid" in full or "googlepay" in full) else "unknown"
    if template == "unknown":
        flags.append("unknown_template")

    # status
    if re.search(r"fail|notdebited|declin|unsuccess", full):
        status = "failed"
    elif re.search(r"completed|successful", full):
        status = "success"
    elif re.search(r"pending|processing|inprogress", full):
        status = "pending"
    else:
        status = "unknown"
        flags.append("status_unknown")

    # amount: the rupee symbol is unreliable (read as 8,7,2,r,=,*,? or dropped and often glued to the digits),
    # so cross-check the big number with the sentence ("Payment of X completed" / "X not debited"),
    # which carries the symbol as a separate, differently-misread glyph.
    big = big_det = None
    big_prefix = ""
    cands = [(d["h"], d, AMT_ONLY.match(d["text"])) for d in ds]
    cands = [c for c in cands if c[2]]
    if cands:
        _, big_det, m = max(cands, key=lambda t: t[0])
        big = _dec(m.group(1))
        big_prefix = big_det["text"][:m.start(1)].strip()
    sent = None
    for rx in (SENT_PAID, SENT_FAIL):
        m = rx.search(full)
        if m:
            sent = _dec(m.group(1))
            break
    amount, amount_weak = None, False
    if big is not None and sent is not None:
        sb, ss = _fmt(big), _fmt(sent)
        if sb == ss:
            amount = big
        else:
            vb = {sb} | ({sb[1:]} if len(sb) > 1 and sb[1].isdigit() else set())
            vs = {ss} | ({ss[1:]} if len(ss) > 1 and ss[1].isdigit() else set())
            common = vb & vs
            if common:
                amount = Decimal(max(common, key=len))
                notes.append("symbol_glued_digit")
            else:
                amount, amount_weak = big, True
                flags.append("amount_mismatch")
    elif big is not None or sent is not None:
        amount = big if big is not None else sent
        if big is None or big_prefix == "":                     # no visible symbol -> a glued digit is possible
            flags.append("amount_symbol_unverified")
            amount_weak = True
    else:
        flags.append("no_amount")

    # date/time: EasyOCR splits the line into several boxes, so parse the joined row
    dt, date_boxes = None, []
    for d in ds:
        if d["h"] < 150 and re.search(r"20\d\d|[ap]\.?m\b", d["text"], re.I):
            band = _row(ds, d)
            dt = parse_datetime(" ".join(e["text"] for e in band))
            if dt:
                date_boxes = band
                break
    if dt is None:
        flags.append("no_date")

    # UPI transaction id (12 digits) = dedupe key
    txn, txn_det = None, None
    for i, d in enumerate(ds):
        if TXN_LABEL.search(_c(d["text"])):
            for e in ds[i + 1:i + 4]:
                t2 = re.sub(r"\s", "", e["text"]).translate(ID_FIX)
                if re.fullmatch(r"\d{12}", t2):
                    txn, txn_det = t2, e
                    break
            break
    if txn is None:
        for d in ds:
            t2 = re.sub(r"\s", "", d["text"]).translate(ID_FIX)
            if re.fullmatch(r"\d{12}", t2):
                txn, txn_det = t2, d
                break
    if txn is None:
        flags.append("no_txn_id")

    # direction + merchant
    direction, header, head_det = None, None, None
    for d in ds:
        if d["y0"] > 0.35 * maxy:
            break
        m = HEADER_RE.match(d["text"]) or HEADER_RE2.match(d["text"])
        if m:
            direction = "debit" if m.group(1).lower() == "to" else "credit"
            header, head_det = m.group(2).strip(), d
            break
    detail = None
    rx = DETAIL_TO if direction != "credit" else DETAIL_FROM
    for i, d in enumerate(ds):
        m = rx.match(d["text"])
        if m and d is not head_det:
            detail = re.sub(r"\s*\(.*$", "", m.group(1)).strip()
            nxt = ds[i + 1] if i + 1 < len(ds) else None
            nt = nxt["text"].strip() if nxt is not None else ""
            wrapped = (nxt is not None and direction != "credit"
                       and _cnorm(detail) != _cnorm(header or "")          # header already complete -> no wrap
                       and "@" not in nt and not re.match(r"^(from|google)", nt, re.I)
                       and nxt["y0"] - d["y1"] <= 1.2 * d["h"]
                       and re.fullmatch(r"[A-Z0-9][A-Za-z0-9 &.'\-]*", nt)   # a name continuation, not a handle ("b4swaptys")
                       and not (re.search(r"[a-z]", nt) and re.search(r"\d", nt)))
            if wrapped:
                detail = (detail + " " + nt).strip()
            break
    if direction is None:
        flags.append("no_direction")
    header = fix_confusables(header) if header else header
    detail = fix_confusables(detail) if detail else detail
    merchant = merge_names(header, detail) if header else (detail or None)
    if not merchant:
        flags.append("no_merchant")

    # classify the outcome
    if status != "success":
        kind, reason = "rejected", f"payment_{status}"
    elif direction == "credit":
        kind, reason = "received", None
    else:
        kind, reason = "expense", None
    category = categorise_merchant(merchant) if (merchant and kind == "expense") else ("received" if kind == "received" else None)

    # confidence = weakest field the user must trust. The amount box is excluded when two readings agree
    # (its confidence is low only because of the rupee glyph).
    used = [x["conf"] for x in (head_det, txn_det) if x] + ([min(e["conf"] for e in date_boxes)] if date_boxes else [])
    if amount_weak and big_det:
        used.append(big_det["conf"])
    rec = {"kind": kind, "reason": reason, "template": template, "direction": direction, "status": status,
           "amount": _fmt(amount) if amount is not None else None, "merchant": merchant, "datetime": dt,
           "txn_id": txn, "category": category, "flags": flags, "notes": notes,
           "confidence": round(min(used), 3) if used else 0.0}
    rec["recordable"] = bool(kind in ("expense", "received") and amount is not None and amount > 0 and txn)
    rec["needs_review"] = bool(flags) or rec["confidence"] < 0.5
    return rec


# ------------------------------------------------------------ amount in words ("Rupees Four Hundred Fifteen Only")
_W_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
            "seventeen": 17, "eighteen": 18, "nineteen": 19}
_W_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_W_RX = re.compile("|".join(sorted(list(_W_UNITS) + list(_W_TENS) + ["hundred", "thousand", "lakhs", "lakh", "lac",
                                   "crores", "crore", "and", "paise", "paisa"], key=len, reverse=True)))

def _parse_words(tokens):
    total = current = 0
    for w in tokens:
        if w in _W_UNITS:
            current += _W_UNITS[w]
        elif w in _W_TENS:
            current += _W_TENS[w]
        elif w == "hundred":
            current = (current or 1) * 100
        elif w == "thousand":
            total += (current or 1) * 1000; current = 0
        elif w in ("lakh", "lakhs", "lac"):
            total += (current or 1) * 100000; current = 0
        elif w in ("crore", "crores"):
            total += (current or 1) * 10000000; current = 0
        elif w == "and":
            continue
        else:
            return None
    return total + current

def words_to_amount(text):
    """'Rupees Four Hundred Fifteen Only' (spaces may be missing) -> Decimal('415'); None if it can't be read cleanly."""
    c = re.sub(r"[^a-z]", "", text.lower())
    m = re.search(r"rupees?(.*?)only", c)
    if not m:
        return None
    body = m.group(1)
    toks = _W_RX.findall(body)
    if not toks or "".join(toks) != body:                       # leftover letters = OCR noise
        return None
    for key in ("paise", "paisa"):
        if key in toks:
            i = toks.index(key)
            before = toks[:i]
            if "and" not in before or toks[i + 1:]:
                return None
            a = len(before) - 1 - before[::-1].index("and")
            r, p = _parse_words(before[:a]), _parse_words(before[a + 1:])
            return None if r is None or p is None else Decimal(r) + Decimal(p) / 100
    v = _parse_words(toks)
    return None if v is None else Decimal(v)


# ------------------------------------------------------------ Paytm layout
PAYTM_DT = re.compile(r"(\d{1,2})\s*[:.]\s*(\d{2})\s*([ap])\.?\s*m\.?\s*,?\s*(\d{1,2})\s*([A-Za-z0]{3,5})\.?\s*,?\s*(20\d\d)", re.I)
REF_RX = re.compile(r"up[il1]\s*ref\s*no\.?\s*:?\s*([0-9OolI]{12})", re.I)

def looks_like_paytm(full):
    return bool(re.search(r"up[il1]refno", full))

def looks_like_upi_screen(dets):
    full = _c(" ".join(d["text"] for d in dets))
    return bool(re.search(r"up[il1]transaction|up[il1]ref|up[il1]id|googletransaction|paidsuccessfully|paymentsuccessful|"
                          r"transactionsuccessful|poweredbyup[il1]", full))

def parse_paytm_datetime(text):
    """'08:56 AM, 04 Jun 2025' -> '2025-06-04T08:56'"""
    m = PAYTM_DT.search(text)
    if not m or not _mon(m.group(5)):
        return None
    hh, mm, day, year = int(m.group(1)), int(m.group(2)), int(m.group(4)), int(m.group(6))
    if not (1 <= hh <= 12 and 0 <= mm <= 59 and 1 <= day <= 31):
        return None
    hh = hh % 12 + (12 if m.group(3).lower() == "p" else 0)
    return f"{year:04d}-{_mon(m.group(5)):02d}-{day:02d}T{hh:02d}:{mm:02d}"

def extract_paytm(ds, full):
    flags, notes = [], []
    if re.search(r"fail|declin|unsuccess|cancel", full):
        status = "failed"
    elif re.search(r"paidsuccessfully|paymentsuccess|transactionsuccess|successful", full):
        status = "success"
    elif re.search(r"pending|processing|inprogress", full):
        status = "pending"
    else:
        status = "unknown"
        flags.append("status_unknown")

    # amount: big number, verified by the amount written in words (immune to the rupee-glyph problem)
    big = big_det = None
    prefix = ""
    cands = [(d["h"], d, AMT_ONLY.match(d["text"])) for d in ds]
    cands = [c for c in cands if c[2]]
    if cands:
        _, big_det, m = max(cands, key=lambda t: t[0])
        big, prefix = _dec(m.group(1)), big_det["text"][:m.start(1)].strip()
    words = None
    for d in ds:
        cd = _c(d["text"])
        if "rupee" in cd and "only" in cd:
            words = words_to_amount(d["text"])
            break
    if words is None:
        words = words_to_amount(" ".join(d["text"] for d in ds))
    amount, weak = None, False
    if big is not None:
        sb = _fmt(big)
        variants = {sb} | ({sb[1:]} if len(sb) > 1 and sb[1].isdigit() else set())
        if words is not None:
            if any(_dec(v) == words for v in variants):
                amount = words
                if _fmt(words) != sb:
                    notes.append("symbol_glued_digit")
            else:
                amount, weak = big, True
                flags.append("amount_mismatch")
        else:
            amount = big
            if prefix == "":
                flags.append("amount_symbol_unverified")
                weak = True
    elif words is not None:
        amount = words
        notes.append("amount_from_words")
    else:
        flags.append("no_amount")

    # UPI reference number (12 digits) = dedupe key
    ref = ref_det = None
    for d in ds:
        m = REF_RX.search(d["text"])
        if m:
            t = m.group(1).translate(ID_FIX)
            if re.fullmatch(r"\d{12}", t):
                ref, ref_det = t, d
                break
    if ref is None:
        for i, d in enumerate(ds):
            if re.search(r"up[il1]refno", _c(d["text"])):
                for e in ds[i + 1:i + 3]:
                    t = re.sub(r"\s", "", e["text"]).translate(ID_FIX)
                    if re.fullmatch(r"\d{12}", t):
                        ref, ref_det = t, e
                        break
                break
    if ref is None:
        flags.append("no_txn_id")

    # counterparty ("To: name" wraps onto the next line)
    credit = bool(re.search(r"received|creditedto", full))
    rx = DETAIL_FROM if credit else DETAIL_TO
    merchant = m_det = None
    for i, d in enumerate(ds):
        m = rx.match(d["text"])
        if m and m.group(1).strip():
            name, m_det = m.group(1).strip(), d
            nxt = ds[i + 1] if i + 1 < len(ds) else None
            if nxt is not None:
                nt = nxt["text"].strip()
                if (re.fullmatch(r"[A-Za-z][A-Za-z .'\-&]*", nt) and not re.match(r"^(upi|from|to\b)", nt, re.I)
                        and "@" not in nt and nxt["y0"] - d["y1"] <= 1.5 * d["h"]):
                    name = (name + " " + nt).strip()
            merchant = fix_confusables(name)
            break
    if not merchant:
        flags.append("no_merchant")
    if credit:
        flags.append("paytm_received_layout_unverified")

    dt, dboxes = None, []
    for d in ds:
        if d["h"] < 150 and re.search(r"20\d\d", d["text"]):
            band = _row(ds, d)
            txt = " ".join(e["text"] for e in band)
            dt = parse_paytm_datetime(txt) or parse_datetime(txt)
            if dt:
                dboxes = band
                break
    if dt is None:
        flags.append("no_date")

    direction = "credit" if credit else ("debit" if merchant else None)
    if direction is None:
        flags.append("no_direction")
    if status != "success":
        kind, reason = "rejected", f"payment_{status}"
    elif credit:
        kind, reason = "received", None
    else:
        kind, reason = "expense", None
    category = categorise_merchant(merchant) if (merchant and kind == "expense") else ("received" if kind == "received" else None)
    used = [x["conf"] for x in (m_det, ref_det) if x] + ([min(e["conf"] for e in dboxes)] if dboxes else [])
    if weak and big_det:
        used.append(big_det["conf"])
    rec = {"kind": kind, "reason": reason, "template": "paytm", "direction": direction, "status": status,
           "amount": _fmt(amount) if amount is not None else None, "merchant": merchant, "datetime": dt,
           "txn_id": ref, "category": category, "flags": flags, "notes": notes,
           "confidence": round(min(used), 3) if used else 0.0}
    rec["recordable"] = bool(kind in ("expense", "received") and amount is not None and amount > 0 and ref)
    rec["needs_review"] = bool(flags) or rec["confidence"] < 0.5
    return rec
