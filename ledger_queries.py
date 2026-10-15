"""ledger_queries.py - answer questions about logged/scanned payments (keyword routing, no ML, no retraining).
answer(q) returns a reply string, or None so the normal chatbot flow continues."""
import re
from datetime import datetime
import ledger

CATEGORY_WORDS = {"food", "groceries", "travel", "entertainment", "bills", "shopping", "health", "misc",
                  "leisure", "rent", "transport", "fuel", "petrol", "medicine", "snacks"}
PERIODS = [("last month", r"last\s+month"), ("today", r"\btoday\b"), ("this week", r"this\s+week|past\s+week"),
           ("all time", r"all[\s-]*time|\bever\b|overall|in\s+total|so\s+far"), ("this month", r"this\s+month")]

def _period(q):
    for label, rx in PERIODS:
        if re.search(rx, q):
            return label, True
    return "this month", False

def _money(paise):
    return f"₹{paise // 100:,}" if paise % 100 == 0 else f"₹{paise / 100:,.2f}"

def _when(dt):
    try:
        d = datetime.fromisoformat(dt)
        return f"{d.day} {d:%b %Y}, {d.hour % 12 or 12}:{d:%M} {d:%p}"
    except Exception:
        return dt

def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())

def _target(q):
    m = re.search(r"\b(?:at|on|to|for|with)\s+(.+?)(?:\s+(?:this|last|today|so|in|during|overall|ever|all)\b.*)?$", q)
    return m.group(1).strip() if m else None

def _match(target):
    t = _norm(" ".join(w for w in target.split() if w not in {"my", "the", "a", "an"}))
    if len(t) < 3 or t in CATEGORY_WORDS:
        return []
    out = []
    for m in ledger.merchants():
        n = _norm(m)
        if t in n or (len(n) >= 4 and n in t):
            out.append(m)
    return out

def _line(r):
    who = r["merchant"] if r["source"] != "chat" else f"{r['merchant']} (chat)"
    sign = "+" if r["kind"] == "received" else ""
    return f"• {_when(r['dt'])} — **{who}** — {sign}{_money(r['amount_paise'])}"

def answer(q):
    q = q.lower().strip().rstrip("?!. ")
    if not q or not ledger.has_rows():
        return None
    label, explicit = _period(q)
    start, end = ledger.bounds(label)
    plural = bool(re.search(r"payments|transactions|receipts|scans|purchases|expenses|history|ledger", q))

    # recent payments
    if (re.search(r"\b(last|latest|recent|previous)\b.*\b(payments?|transactions?|receipts?|scans?|purchases?|expenses?)\b", q)
            and not re.search(r"last\s+(month|week|year)", q)) or re.search(r"\bshow\b.*\b(payments|transactions|ledger|history)\b", q):
        rows = ledger.recent(5 if plural else 1)
        head = "🧾 Your recent payments:" if plural else "🧾 Your last payment:"
        return head + "\n\n" + "\n".join(_line(r) for r in rows)

    # biggest
    if re.search(r"\b(biggest|largest|highest|most expensive|top)\b.*\b(payment|transaction|expense|purchase|spend)", q):
        rows = ledger.biggest(3, start, end)
        if not rows and not explicit:
            label, rows = "all time", ledger.biggest(3, None, None)
        if not rows:
            return f"I don't have any payments {label} yet."
        return f"💸 Biggest payments ({label}):\n\n" + "\n".join(_line(r) for r in rows)

    # top merchants
    if (re.search(r"\b(top|most|favou?rite)\b.*\b(merchants?|shops?|stores?|places?)\b", q)
            or re.search(r"where\b.*\b(spend|pay|paid|spent)\b.*\bmost\b", q)):
        rows = ledger.top_merchants(5, start, end)
        if not rows:
            return None
        return f"🏪 Where your scanned payments went most ({label}):\n\n" + "\n".join(
            f"{i}. **{r['merchant']}** — {_money(r['paise'])} ({r['n']} payment{'s' if r['n'] != 1 else ''})"
            for i, r in enumerate(rows, 1))

    # counts
    if re.search(r"how many\b.*\b(payments?|transactions?|times|purchases?)", q):
        tgt = _target(q)
        ms = _match(tgt) if tgt else []
        if ms:
            paise, n = ledger.merchant_total(ms, start, end)
            return f"You made **{n}** payment{'s' if n != 1 else ''} to {', '.join(ms)} {label} ({_money(paise)} in total)."
        paise, n = ledger.totals(start, end)
        return f"You made **{n}** payment{'s' if n != 1 else ''} {label}, {_money(paise)} in total."

    # how much at <merchant>
    if re.search(r"\b(how much|total|sum)\b", q):
        tgt = _target(q)
        ms = _match(tgt) if tgt else []
        if ms:
            paise, n = ledger.merchant_total(ms, start, end)
            if n == 0:
                return f"No payments to {', '.join(ms)} found {label}."
            return f"You spent **{_money(paise)}** at **{', '.join(ms)}** {label} — {n} payment{'s' if n != 1 else ''}."
    return None
