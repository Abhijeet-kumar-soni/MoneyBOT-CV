#!/usr/bin/env python3
"""Adds the CV features to the existing MoneyBOT app (run from the same folder as app.py).
Safe to run twice. Makes app.py.bak_cv / chatbot_backend.py.bak_cv backups first.
If an anchor isn't found it prints the exact lines to add by hand."""
import os, re, shutil, textwrap

HERE = os.path.dirname(os.path.abspath(__file__))

LEDGER_QUERY_BLOCK = '''# ── 4d. Ledger queries (scanned UPI payments / receipts) ──────────────────
try:
    from ledger_queries import answer as _ledger_answer
    _lq = _ledger_answer(q)
    if _lq:
        return _lq
except Exception as _e:
    print("[chatbot] ledger query skipped:", _e)

'''
CHAT_LOG_BLOCK = '''try:
    import ledger as _ledger
    _ok, _msg, _rid = _ledger.add({"source": "chat", "kind": "expense", "amount": str(amt),
                                   "merchant": cat, "category": cat})
    if _ok:
        import streamlit as _st
        _st.session_state.setdefault("ledger_applied_ids", set()).add(_rid)
except Exception as _e:
    print("[chatbot] ledger log skipped:", _e)
'''

def _ind(block, indent):
    return textwrap.indent(block, indent)

APP_EDITS = [
    ("from scan_page import page_scan", r'^from chatbot_backend import .*$', lambda m: m.group(0) + "\nfrom scan_page import page_scan",
     "import the Scan page", "  add after the chatbot_backend import:  from scan_page import page_scan"),
    ('("📷", "Scan Payment")', r'^(\s*)\("🤖",\s*"BOT"\),.*$', lambda m: m.group(0) + "\n" + m.group(1) + '("📷", "Scan Payment"),',
     "add 'Scan Payment' to the sidebar", '  add to the `pages` list in render_sidebar:  ("📷", "Scan Payment"),'),
    ("page_scan()", r'^else:\n\s+st\.error\(f?["\']Unknown page', lambda m: 'elif page == "Scan Payment":    page_scan()\n' + m.group(0),
     "route the page", '  add before the final else in the router:  elif page == "Scan Payment":    page_scan()'),
]
def _eval_tab(m):
    ind, inner = m.group(1), m.group(2)
    return (f'{ind}tab1, tab2, tab3 = st.tabs([{inner}, "  👁️  Computer Vision"])\n'
            f'{ind}with tab3:\n{ind}    from cv_eval_page import render_cv_evaluation\n{ind}    render_cv_evaluation()')

APP_EDITS.append(("render_cv_evaluation", r'^(\s*)tab1, tab2 = st\.tabs\(\[(.*)\]\)\s*$', _eval_tab,
                  "add the 'Computer Vision' tab to Model Evaluation",
                  '  in page_model_evaluation(), change the tabs line to three tabs and add:\n'
                  '      with tab3:\n          from cv_eval_page import render_cv_evaluation\n          render_cv_evaluation()'))

CHAT_EDITS = [
    ("ledger_queries", r'^(\s*)# ── 5\. BERT intent prediction.*$', lambda m: _ind(LEDGER_QUERY_BLOCK, m.group(1)) + m.group(0),
     "chat queries over the ledger (before BERT)", "  add before the BERT step in chatbot():\n" + _ind(LEDGER_QUERY_BLOCK, "    ")),
    ("_ledger.add(", r'^(\s+)inc = p\["income"\]\n\s+spent_pct = p\["monthly_spent"\]', lambda m: _ind(CHAT_LOG_BLOCK, m.group(1)) + m.group(0),
     "log chat expenses to the ledger", "  (optional) after the expense totals are updated in chatbot():\n" + _ind(CHAT_LOG_BLOCK, "    ")),
]

def patch_file(name, edits):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        print(f"[skip] {name} not found next to this script")
        return
    print(f"{name}:")
    src = orig = open(path, encoding="utf-8").read()
    for marker, regex, repl, desc, manual in edits:
        if marker in src:
            print(f"  [already] {desc}")
            continue
        m = re.search(regex, src, re.M)
        if not m:
            print(f"  [MANUAL ] {desc}\n{manual}")
            continue
        src = src[:m.start()] + repl(m) + src[m.end():]
        print(f"  [done   ] {desc}")
    if src != orig:
        bak = path + ".bak_cv"
        if not os.path.exists(bak):
            shutil.copy(path, bak)
        open(path, "w", encoding="utf-8").write(src)

if __name__ == "__main__":
    patch_file("app.py", APP_EDITS)
    patch_file("chatbot_backend.py", CHAT_EDITS)
    print("\nDone. Install an OCR engine if you haven't:  pip install easyocr   (or: pip install rapidocr-onnxruntime)")
