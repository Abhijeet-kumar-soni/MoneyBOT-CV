"""ledger.py - persistent transaction ledger (SQLite, stdlib only).

One row per payment. UPI payments are deduplicated on the 12-digit UPI transaction ID, receipts on a
merchant+minute+amount fingerprint; chat-logged expenses are never deduplicated.
The ledger is history/analytics. Profile totals stay session-based (as before) - see scan_page re-apply button.
"""
import csv, hashlib, io, os, re, sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

DB_PATH = os.environ.get("MONEYBOT_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "moneybot.db")

SCHEMA = """CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id TEXT UNIQUE,
    fp TEXT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    merchant TEXT,
    category TEXT,
    dt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confidence REAL
)"""

def _now():
    return datetime.now()

def _run(sql, args=(), commit=False):
    with closing(sqlite3.connect(DB_PATH)) as c:
        c.row_factory = sqlite3.Row
        c.execute(SCHEMA)
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        last = cur.lastrowid
        if commit:
            c.commit()
        return rows, last

def to_paise(x):
    d = Decimal(str(x).replace(",", "").strip())
    return int((d * 100).to_integral_value())

def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def add(rec):
    """rec: source, kind('expense'|'received'), amount, merchant, category, datetime(ISO, optional), txn_id, confidence.
    Returns (ok, message, row_id)."""
    try:
        paise = to_paise(rec["amount"])
    except (InvalidOperation, KeyError, ValueError):
        return False, "Invalid amount", None
    if paise <= 0:
        return False, "Amount must be positive", None
    dt = (rec.get("datetime") or _now().strftime("%Y-%m-%dT%H:%M"))[:16]
    source, merchant = rec.get("source", "chat"), (rec.get("merchant") or "Unknown").strip()
    txn_id = rec.get("txn_id") or None
    fp = None
    if txn_id:
        rows, _ = _run("SELECT id FROM transactions WHERE txn_id = ?", (txn_id,))
        if rows:
            return False, "Already recorded (same UPI transaction ID).", rows[0]["id"]
    elif source != "chat":
        fp = hashlib.md5(f"{_norm(merchant)}|{dt}|{paise}".encode()).hexdigest()
        rows, _ = _run("SELECT id FROM transactions WHERE fp = ?", (fp,))
        if rows:
            return False, "Looks like a duplicate (same merchant, time and amount).", rows[0]["id"]
    _, rid = _run("""INSERT INTO transactions (txn_id, fp, source, kind, amount_paise, merchant, category, dt, created_at, confidence)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""",
                  (txn_id, fp, source, rec.get("kind", "expense"), paise, merchant, rec.get("category"), dt,
                   _now().strftime("%Y-%m-%dT%H:%M:%S"), rec.get("confidence")), commit=True)
    return True, "Recorded", rid

def has_rows():
    rows, _ = _run("SELECT 1 FROM transactions LIMIT 1")
    return bool(rows)

def bounds(label):
    """-> (start_iso, end_iso) for a period label; (None, None) = all time."""
    now = _now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if label == "today":
        s, e = day0, day0 + timedelta(days=1)
    elif label == "this week":
        s = day0 - timedelta(days=now.weekday()); e = s + timedelta(days=7)
    elif label == "this month":
        s = day0.replace(day=1); e = (s + timedelta(days=32)).replace(day=1)
    elif label == "last month":
        e = day0.replace(day=1); s = (e - timedelta(days=1)).replace(day=1)
    else:
        return None, None
    f = "%Y-%m-%dT%H:%M"
    return s.strftime(f), e.strftime(f)

def _where(start, end, kind="expense", no_chat=False, extra="", args=()):
    cond, a = [], []
    if kind:
        cond.append("kind = ?"); a.append(kind)
    if no_chat:
        cond.append("source != 'chat'")
    if start:
        cond.append("dt >= ?"); a.append(start)
    if end:
        cond.append("dt < ?"); a.append(end)
    if extra:
        cond.append(extra); a += list(args)
    return (" WHERE " + " AND ".join(cond)) if cond else "", a

def recent(n=10, kind=None):
    w, a = _where(None, None, kind=kind)
    rows, _ = _run(f"SELECT * FROM transactions{w} ORDER BY dt DESC, id DESC LIMIT ?", a + [n])
    return [dict(r) for r in rows]

def biggest(n, start, end):
    w, a = _where(start, end)
    rows, _ = _run(f"SELECT * FROM transactions{w} ORDER BY amount_paise DESC LIMIT ?", a + [n])
    return [dict(r) for r in rows]

def top_merchants(n, start, end):
    w, a = _where(start, end, no_chat=True)
    rows, _ = _run(f"SELECT merchant, SUM(amount_paise) AS paise, COUNT(*) AS n FROM transactions{w} "
                   f"GROUP BY lower(merchant) ORDER BY paise DESC LIMIT ?", a + [n])
    return [dict(r) for r in rows]

def merchants():
    rows, _ = _run("SELECT DISTINCT merchant FROM transactions WHERE source != 'chat' AND kind = 'expense'")
    return [r["merchant"] for r in rows if r["merchant"]]

def merchant_total(names, start, end):
    ph = ",".join("?" * len(names))
    w, a = _where(start, end, extra=f"merchant IN ({ph})", args=names)
    rows, _ = _run(f"SELECT COALESCE(SUM(amount_paise),0) AS paise, COUNT(*) AS n FROM transactions{w}", a)
    return rows[0]["paise"], rows[0]["n"]

def totals(start, end):
    w, a = _where(start, end)
    rows, _ = _run(f"SELECT COALESCE(SUM(amount_paise),0) AS paise, COUNT(*) AS n FROM transactions{w}", a)
    return rows[0]["paise"], rows[0]["n"]

def rows_for_month(year_month):
    rows, _ = _run("SELECT * FROM transactions WHERE kind='expense' AND dt LIKE ? ORDER BY dt", (year_month + "%",))
    return [dict(r) for r in rows]

def export_csv():
    rows, _ = _run("SELECT dt, kind, merchant, category, amount_paise, source, txn_id FROM transactions ORDER BY dt")
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["datetime", "kind", "merchant", "category", "amount_inr", "source", "upi_txn_id"])
    for r in rows:
        w.writerow([r["dt"], r["kind"], r["merchant"], r["category"], f"{r['amount_paise'] / 100:.2f}", r["source"], r["txn_id"] or ""])
    return buf.getvalue()


def count():
    rows, _ = _run("SELECT COUNT(*) AS n FROM transactions")
    return rows[0]["n"]

def delete(ids):
    """Delete ledger rows by id. Returns the deleted rows (so callers can undo their effect on totals)."""
    ids = [int(i) for i in ids]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows, _ = _run(f"SELECT * FROM transactions WHERE id IN ({ph})", ids)
    _run(f"DELETE FROM transactions WHERE id IN ({ph})", ids, commit=True)
    return [dict(r) for r in rows]

def clear_all():
    rows, _ = _run("SELECT * FROM transactions")
    _run("DELETE FROM transactions", commit=True)
    return [dict(r) for r in rows]
