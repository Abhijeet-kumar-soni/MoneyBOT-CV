"""scan_page.py - Streamlit page: upload UPI payment screenshots (Google Pay / Paytm) or paper receipts (beta) -> review -> ledger + totals."""
import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pandas as pd
import streamlit as st

import ledger
import vision_engine
from chatbot_backend import get_user_profile

CATEGORIES = ["food", "groceries", "travel", "entertainment", "bills", "shopping", "health", "misc"]
FLAG_TEXT = {
    "no_date": "date/time not found", "no_txn_id": "UPI transaction ID not found",
    "amount_mismatch": "the two amount readings disagree", "amount_symbol_unverified": "amount may include the ₹ symbol",
    "unknown_template": "unfamiliar screenshot layout", "no_amount": "amount not found", "no_merchant": "merchant not found",
    "no_direction": "could not tell paid vs received", "status_unknown": "payment status not found",
    "paytm_received_layout_unverified": "Paytm 'received' layout not verified yet",
    "receipt_beta": "paper-receipt reading is approximate (about 60% accurate)",
    "foreign_currency": "this receipt is not in rupees - enter the exchange rate below",
    "date_format_assumed": "date format guessed (day/month vs month/day)",
}
REJECT_TEXT = {
    "payment_failed": "This payment **failed**, so no money was debited. It won't be recorded.",
    "payment_pending": "This payment is still **pending**. Scan it again once it completes.",
    "payment_unknown": "Couldn't tell whether this payment went through, so it won't be recorded.",
    "no_text": "Couldn't read any text in this image.",
}
BADGES = {"gpay": "Google Pay payment", "paytm": "Paytm payment", "unknown": "UPI payment (unfamiliar layout)"}


@st.cache_resource(show_spinner="Loading OCR engine (first scan only)...")
def _ocr_engine():
    return vision_engine.get_ocr()


# ------------------------------------------------------------------ session totals (same bookkeeping as the chatbot)
def _recalc(p):
    p["savings"] = p.get("income", 0) - p.get("monthly_spent", 0)
    p["savings_rate"] = p["savings"] / p["income"] if p.get("income") else 0

def _apply_expense(p, rupees, category):
    p["monthly_spent"] = p.get("monthly_spent", 0) + rupees
    p[category] = p.get(category, 0) + rupees
    _recalc(p)

def _remove_expense(p, rupees, category):
    p["monthly_spent"] = max(0, p.get("monthly_spent", 0) - rupees)
    if category in p:
        p[category] = max(0, p.get(category, 0) - rupees)
    _recalc(p)

def _apply_income(p, rupees):
    p["income"] = p.get("income", 0) + rupees
    _recalc(p)

def _applied():
    return st.session_state.setdefault("ledger_applied_ids", set())


# ------------------------------------------------------------------ one uploaded screenshot
def _scan_card(f):
    data = f.getvalue()
    h = hashlib.md5(data).hexdigest()[:12]
    results = st.session_state.setdefault("scan_results", {})
    done = st.session_state.setdefault("scan_done", {})          # h -> (message, ledger id)
    if h not in results:
        try:
            with st.spinner(f"Reading {f.name} ..."):
                results[h] = vision_engine.scan_image(data, ocr=_ocr_engine())
        except Exception as e:
            st.error(f"Could not read {f.name}: {e}")
            return
    res = results[h]
    rec = res["record"]

    with st.container(border=True):
        left, right = st.columns([1, 2], gap="large")
        with left:
            st.image(data, caption=f.name[:38])
        with right:
            if h in done:
                st.success(done[h][0])
                return
            if rec["kind"] == "rejected":
                st.error(REJECT_TEXT.get(rec.get("reason"), "This image can't be recorded."))
                return

            kind = rec["kind"]
            badge = BADGES.get(rec.get("template"), BADGES["unknown"]) if res["doc_type"] == "upi" else "Paper receipt (beta)"
            st.markdown(f"**{badge}** · read with {res['engine']} in "
                        f"{res['seconds']}s · confidence {rec['confidence']:.2f}")
            if kind == "received":
                st.info("💰 Money **received** — not an expense.")
            labels = [FLAG_TEXT.get(x, x) for x in rec.get("flags", [])]
            if labels:
                st.warning("Please double-check: " + "; ".join(labels))

            c1, c2 = st.columns(2)
            amount = c1.text_input("Amount (₹)", value=rec.get("amount") or "", key=f"amt_{h}")
            merchant = c2.text_input("Paid to" if kind == "expense" else "Received from", value=rec.get("merchant") or "", key=f"mer_{h}")
            c3, c4 = st.columns(2)
            cat = rec.get("category") if rec.get("category") in CATEGORIES else "misc"
            category = c3.selectbox("Category", CATEGORIES, index=CATEGORIES.index(cat), key=f"cat_{h}", disabled=(kind == "received"))
            when = c4.text_input("Date & time (YYYY-MM-DDTHH:MM)", value=rec.get("datetime") or datetime.now().strftime("%Y-%m-%dT%H:%M"),
                                 key=f"when_{h}")
            currency = rec.get("currency", "INR")
            rate = 1.0
            if currency != "INR" and kind == "expense":
                rate = st.number_input(f"Looks like a {currency} receipt. Exchange rate: 1 {currency} = ₹", min_value=0.0, value=0.0,
                                       step=0.5, key=f"fx_{h}", help="MoneyBOT tracks rupees; enter the rate you want to use.")
            add_income = False
            if kind == "received":
                add_income = st.checkbox("Also add this to my monthly income", value=False, key=f"inc_{h}")
            if rec.get("txn_id"):
                st.caption(f"UPI transaction ID: {rec['txn_id']}")

            with st.expander("How it was read"):
                st.image(vision_engine.overlay_image(data, res["dets"]), caption="green = confident, orange = unsure, red = low confidence")

            if st.button("✅ Confirm & add", key=f"ok_{h}", type="primary"):
                try:
                    amt = Decimal(amount.replace(",", "").strip())
                    if amt <= 0:
                        raise InvalidOperation
                except InvalidOperation:
                    st.error("Enter a valid amount.")
                    return
                try:
                    when_iso = datetime.fromisoformat(when.strip()).strftime("%Y-%m-%dT%H:%M")
                except ValueError:
                    st.error("Date must look like 2026-10-07T19:49")
                    return
                if currency != "INR" and kind == "expense":
                    if rate <= 0:
                        st.error(f"Enter the {currency} → ₹ exchange rate first.")
                        return
                    amt = (amt * Decimal(str(rate))).quantize(Decimal("0.01"))
                ok, msg, rid = ledger.add({
                    "source": "upi" if res["doc_type"] == "upi" else "receipt", "kind": kind, "amount": str(amt), "merchant": merchant.strip() or "Unknown",
                    "category": category if kind == "expense" else "received", "datetime": when_iso,
                    "txn_id": rec.get("txn_id"), "confidence": rec.get("confidence")})
                if not ok:
                    st.warning(msg)
                    return
                p, rupees = get_user_profile(), int(round(float(amt)))
                if kind == "expense":
                    _apply_expense(p, rupees, category)
                    _applied().add(rid)
                    done[h] = (f"✅ Recorded ₹{amt:,} at {merchant.strip() or 'Unknown'} ({category}). Totals updated.", rid)
                else:
                    if add_income:
                        _apply_income(p, rupees)
                    done[h] = (f"✅ Recorded ₹{amt:,} received from {merchant.strip() or 'Unknown'}"
                               + (" and added to income." if add_income else "."), rid)
                st.rerun()


# ------------------------------------------------------------------ ledger history + delete
def _reapply():
    """Profile totals are per-session; the ledger persists. Re-add this month's ledger expenses once per session."""
    p, applied, n = get_user_profile(), _applied(), 0
    for r in ledger.rows_for_month(datetime.now().strftime("%Y-%m")):
        if r["id"] in applied:
            continue
        cat = r["category"] if r["category"] in CATEGORIES else "misc"
        _apply_expense(p, int(round(r["amount_paise"] / 100)), cat)
        applied.add(r["id"])
        n += 1
    return n

def _delete_rows(rows):
    """rows: ledger rows already deleted from the DB. Undo their effect on this session's totals and re-open their scan cards."""
    p, applied, undone = get_user_profile(), _applied(), 0
    gone = {r["id"] for r in rows}
    for r in rows:
        if r["id"] in applied and r["kind"] == "expense":
            cat = r["category"] if r["category"] in CATEGORIES else "misc"
            _remove_expense(p, int(round(r["amount_paise"] / 100)), cat)
            undone += 1
        applied.discard(r["id"])
    done = st.session_state.get("scan_done", {})
    for h in [h for h, (_, rid) in done.items() if rid in gone]:
        del done[h]
    return undone

def _cb_delete_selected(labels):
    ids = [labels[s] for s in st.session_state.get("del_sel", []) if s in labels]
    rows = ledger.delete(ids)
    undone = _delete_rows(rows)
    st.session_state["ledger_msg"] = f"Deleted {len(rows)} payment(s)" + (f" and took {undone} off this session's totals." if undone else ".")
    st.session_state["del_sel"] = []

def _cb_delete_all():
    rows = ledger.clear_all()
    undone = _delete_rows(rows)
    st.session_state["ledger_msg"] = f"Ledger cleared ({len(rows)} entries)" + (f"; {undone} taken off this session's totals." if undone else ".")
    st.session_state["del_all_ok"] = False

def _history():
    st.markdown("### Recent payments")
    flash = st.session_state.pop("ledger_msg", None)
    if flash:
        st.success(flash)
    rows = ledger.recent(10)
    if not rows:
        st.info("Nothing in the ledger yet. Scan a payment above or log an expense in the chat.")
        return
    df = pd.DataFrame([{"When": r["dt"].replace("T", " "), "Merchant": r["merchant"], "Category": r["category"],
                        "Amount (₹)": r["amount_paise"] / 100, "Type": r["kind"], "Source": r["source"]} for r in rows])
    st.dataframe(df, hide_index=True)
    st.caption("The ledger is saved on disk (moneybot.db), so entries survive a refresh or restart. Use **Delete payments** below to remove any.")
    c1, c2 = st.columns(2)
    if c1.button("↻ Re-apply this month's ledger to my totals"):
        n = _reapply()
        st.success(f"Added {n} ledger expense(s) to this session's totals." if n else "Totals already include everything in this month's ledger.")
    c2.download_button("⬇ Export ledger (CSV)", ledger.export_csv(), "moneybot_ledger.csv", "text/csv")

    with st.expander("🗑 Delete payments"):
        labels = {f"{r['dt'].replace('T', ' ')} · {r['merchant']} · ₹{r['amount_paise'] / 100:g}  (#{r['id']})": r["id"]
                  for r in ledger.recent(50)}
        sel = st.multiselect("Select payments to delete", list(labels), key="del_sel")
        st.button("Delete selected", key="del_btn", disabled=not sel, on_click=_cb_delete_selected, args=(labels,))
        st.markdown("---")
        sure = st.checkbox("I want to delete the whole ledger", key="del_all_ok")
        st.button("Delete ALL entries", key="del_all_btn", disabled=not sure, on_click=_cb_delete_all)


def page_scan():
    st.markdown("# 📷 Scan Payment")
    st.markdown("<hr>", unsafe_allow_html=True)
    st.caption("Upload Google Pay / Paytm payment screenshots (works best) or paper receipts (beta, always flagged for review). "
               "Check what was read, fix anything wrong, then confirm. Failed payments are never recorded.")
    files = st.file_uploader("Payment screenshots or receipts", type=["png", "jpg", "jpeg"], accept_multiple_files=True, key="scan_files")
    for f in files or []:
        _scan_card(f)
    _history()
