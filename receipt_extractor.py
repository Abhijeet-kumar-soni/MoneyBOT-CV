"""receipt_extractor.py - total-amount extraction from OCR boxes.

Pure Python (re + collections). Input: list of OCR detections, each a dict with
keys text, conf, x0, y0, x1, y1, h  (same format as ocr_cache.json).
Output: (amount_as_digit_string | None, method_name)
"""
import re
from collections import Counter
from decimal import Decimal
import numpy as np

NUM_TOKEN = re.compile(r"(?<![A-Za-z])[\dOoIl|SB][\dOoIl|SB.,]*")
FIX = str.maketrans({"o": "0", "O": "0", "I": "1", "l": "1", "|": "1", "S": "5", "B": "8"})

TOTAL_RE  = re.compile(r"(?<![a-z])(?:t[o0][t7][a4][l1i|!]?|grand\s*t|amount\s*(?:due|paid)|net\s*amount|jumlah|bayar)", re.I)
SKIP_SUB  = re.compile(r"(?<![a-z])s[uv][bgd6]\s*-?\s*t", re.I)
SKIP_QTY  = re.compile(r"(?<![a-z])(?:qty|items?|pcs)", re.I)
SKIP_PAY  = re.compile(r"(?<![a-z])(?:cas[h]?|tunai|change|kemb|tax|pajak|service|disc\w*|diskon|tip)", re.I)
CASH_RE   = re.compile(r"(?<![a-z])(?:cash|cas|tunai)", re.I)
CHANGE_RE = re.compile(r"(?<![a-z])(?:change|kemb\w*)", re.I)
GRAND_RE  = re.compile(r"grand", re.I)


# ---------------------------------------------------------------- numbers
def prep_text(text, merge=False):
    text = re.sub(r"\b(rp|rs|inr)\.?\s*(?=[\dOoIl|SB])", r"\1 ", text, flags=re.I)  # Rp16,500 -> Rp 16,500
    text = re.sub(r"(?<=\d)\s*([.,])\s*(?=[\dOo])", r"\1", text)                      # "50 , Ooo" -> "50,Ooo"
    if merge:                                                                            # "88 000" -> "88000"
        prev = None
        while prev != text:
            prev = text
            text = re.sub(r"(?<![\d.,])(\d{1,3}) (\d{3})(?!\d)", r"\1\2", text)
    return text

def numeric_tokens(text, guard=False, merge=False):
    text = prep_text(text, merge)
    out = []
    for m in NUM_TOKEN.finditer(text):
        e = m.end()
        if e < len(text) and text[e].isalpha():
            continue
        tok = m.group()
        if not re.search(r"\d", tok):
            continue
        if tok[0] == "S" and len(tok) > 1 and tok[1].isdigit():      # '$' is often read as 'S' ("S10.64" is $10.64, not 510.64)
            tok = tok[1:]
        if guard:                                   # reject decoration lines like SSBB5B5...
            core = re.sub(r"[.,]", "", tok)
            risky = sum(ch in "SBIl|" for ch in core)
            if risky > max(1, 0.3 * len(core)):
                continue
        out.append(tok.translate(FIX))
    return out

def to_amount(tok, keep_decimals=False):
    t = tok.strip(".,")
    m = re.search(r"[.,](\d{2})$", t)               # 1,250.00 / 10.64 : two digits after the last separator = decimals
    dec = ""
    if m and re.search(r"\d", t[:m.start()]):
        dec, t = m.group(1), t[:m.start()]
    base = re.sub(r"\D", "", t).lstrip("0")
    if keep_decimals and dec and dec != "00" and base:
        return f"{base}.{dec}"
    return base

def amounts_in(text, guard=False, merge=False, keep_decimals=False):
    out = []
    for t in numeric_tokens(text, guard=guard, merge=merge):
        a = to_amount(t, keep_decimals)
        digits = a.replace(".", "")
        if len(digits) >= 2 and digits.strip("0") and (not guard or len(digits) <= 9):
            out.append(a)
    return out

def parse_amount_string(s):
    """Ground-truth strings like 'Rp. 91,000', '365000.00', '9.500,00' -> digit string."""
    return to_amount(re.sub(r"[^\d.,]", "", str(s)))


# ---------------------------------------------------------------- lines (v1)
def group_lines(dets):
    if not dets:
        return []
    med_h = float(np.median([d["h"] for d in dets]))
    ds = sorted(dets, key=lambda d: (d["y0"] + d["y1"]) / 2)
    lines, cur, cur_y = [], [], 0.0
    for d in ds:
        yc = (d["y0"] + d["y1"]) / 2
        if cur and abs(yc - cur_y) > 0.6 * med_h:
            lines.append(cur)
            cur = []
        cur.append(d)
        cur_y = float(np.mean([(c["y0"] + c["y1"]) / 2 for c in cur]))
    if cur:
        lines.append(cur)
    return [{"text": " ".join(c["text"] for c in sorted(l, key=lambda d: d["x0"])),
             "conf": float(np.mean([c["conf"] for c in l]))} for l in lines]

def _skipped(text):
    return bool(SKIP_SUB.search(text) or SKIP_QTY.search(text) or SKIP_PAY.search(text)) and not GRAND_RE.search(text)

def pick_total_v1(lines):
    """Step-2 extractor (line based), kept as the baseline."""
    cands = []
    for i, ln in enumerate(lines):
        t = ln["text"]
        if not TOTAL_RE.search(t) or _skipped(t):
            continue
        nums = amounts_in(t)
        if not nums and i + 1 < len(lines):
            nxt = lines[i + 1]["text"]
            if not (SKIP_PAY.search(nxt) or SKIP_SUB.search(nxt)):
                nums = amounts_in(nxt)
        if nums:
            cands.append(nums[-1])
    if cands:
        return max(cands, key=Decimal), "keyword"
    pool = []
    for ln in lines[int(len(lines) * 0.4):]:
        if SKIP_PAY.search(ln["text"]) or SKIP_SUB.search(ln["text"]):
            continue
        pool += amounts_in(ln["text"])
    if pool:
        return max(pool, key=Decimal), "fallback"
    return None, "none"


# ---------------------------------------------------------------- v2 (geometry based)
def _yc(d):
    return (d["y0"] + d["y1"]) / 2

def _band(dets, ref, tol=0.8):
    yc = _yc(ref)
    return [d for d in dets if abs(_yc(d) - yc) <= tol * max(ref["h"], d["h"])]

def _text(boxes):
    return " ".join(d["text"] for d in sorted(boxes, key=lambda d: d["x0"]))

def _right_amounts(dets, k, merge, guard):
    band = _band(dets, k)
    right = [d for d in band if d["x1"] >= k["x0"]]
    return amounts_in(_text(right), guard=guard, merge=merge), _text(band)

def _below_amounts(dets, k, merge, guard):
    yc, h = _yc(k), k["h"]
    below = [d for d in dets if 0.8 * max(h, d["h"]) < _yc(d) - yc <= 2.5 * max(h, d["h"]) and d["x1"] >= k["x0"]]
    if not below:
        return []
    below.sort(key=_yc)
    first = below[0]
    row = [d for d in below if abs(_yc(d) - _yc(first)) <= 0.8 * max(d["h"], first["h"])]
    full = _text(row)
    if _skipped(full):
        return []
    return amounts_in(full, guard=guard, merge=merge)

def _row_last_amount(dets, k, merge, guard):
    nums, _ = _right_amounts(dets, k, merge, guard)
    return int(nums[-1]) if nums else None

def extract_total(dets, geom=False, merge=True, guard=True, cashchg=False, fallback="max", keep_decimals=False):
    """Defaults = final config chosen on the dev split (line-based + merged thousands + digit guard).
       geom: use box geometry instead of line text (no significant gain on dev).
       cashchg: False | 'confirm' (use cash-change only if it matches a candidate) | 'override'.
       fallback: 'max' | 'freq' (most repeated amount in the lower part of the receipt)."""
    if not dets:
        return None, "none"
    lines = group_lines(dets)
    cands = []
    if geom:
        for k in dets:
            if not TOTAL_RE.search(k["text"]):
                continue
            nums, full = _right_amounts(dets, k, merge, guard)
            if _skipped(full):
                continue
            if not nums:
                nums = _below_amounts(dets, k, merge, guard)
            if nums:
                cands.append(nums[-1])
    else:
        for i, ln in enumerate(lines):
            t = ln["text"]
            if not TOTAL_RE.search(t) or _skipped(t):
                continue
            nums = amounts_in(t, guard=guard, merge=merge, keep_decimals=keep_decimals)
            if not nums and i + 1 < len(lines):
                nxt = lines[i + 1]["text"]
                if not (SKIP_PAY.search(nxt) or SKIP_SUB.search(nxt)):
                    nums = amounts_in(nxt, guard=guard, merge=merge, keep_decimals=keep_decimals)
            if nums:
                cands.append(nums[-1])

    T = None
    if cashchg:
        cash = change = None
        for k in dets:
            if cash is None and CASH_RE.search(k["text"]) and not CHANGE_RE.search(k["text"]):
                cash = _row_last_amount(dets, k, merge, guard)
            if change is None and CHANGE_RE.search(k["text"]):
                change = _row_last_amount(dets, k, merge, guard)
        if cash and change is not None and cash - change >= 10:
            T = str(cash - change)

    if cands:
        if T and (cashchg == "override" or T in cands):
            return T, "cashchg"
        return max(cands, key=Decimal), "keyword"
    if T:
        return T, "cashchg_only"

    pool = []
    for ln in lines[int(len(lines) * 0.4):]:
        if SKIP_PAY.search(ln["text"]) or SKIP_SUB.search(ln["text"]):
            continue
        pool += amounts_in(ln["text"], guard=guard, keep_decimals=keep_decimals)
    if pool:
        if fallback == "freq":
            c = Counter(pool)
            return max(c.items(), key=lambda kv: (kv[1], Decimal(kv[0])))[0], "fallback_freq"
        return max(pool, key=Decimal), "fallback_max"
    return None, "none"
