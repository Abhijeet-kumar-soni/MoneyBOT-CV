"""cv_eval_page.py - the 'Computer Vision' tab on the Model Evaluation page.
Every number below is a measured result from the Colab/offline runs. Update the constants when new test sets are scored."""
import plotly.graph_objects as go
import streamlit as st

LAYOUT = dict(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
              font=dict(color="#9dbda8", family="DM Sans"), margin=dict(l=20, r=20, t=50, b=20))

# ---- UPI (Google Pay, 20 screenshots, EasyOCR raw) -------------------------------------------------------
UPI_FIELDS = ["Amount", "Merchant", "Date & time", "UPI txn ID", "Status", "Direction", "Full record"]
UPI_FIRST  = [95, 80, 65, 100, 100, 100, 45]       # first contact: extractor had never seen EasyOCR output
UPI_AFTER  = [100, 95, 100, 100, 100, 100, 95]     # after patching on those same 20 (development accuracy)

# ---- Receipts (CORD, EasyOCR): held-out 47 receipts, % correct totals ----------------------------------
CFGS  = ["raw", "gray", "gray+up2", "gray+mag2", "gray+up2+clahe", "gray+loose"]
V1    = [61.7, 59.6, 59.6, 46.8, 53.2, 61.7]
V2    = [63.8, 61.7, 59.6, 55.3, 55.3, 61.7]
V2_LO = [49.5, 47.4, 45.3, 41.2, 41.2, 47.4]
V2_HI = [76.0, 74.2, 72.4, 68.6, 68.6, 74.2]
PROG  = {"n": 95, "v0": 35.8, "v1": 57.9, "v2": 62.1, "ceil": 64.2}     # all CORD receipts, gray config

STATUS = [   # dataset, engine, n, result, kind, status
    ("Google Pay - first contact",        "EasyOCR", "20",  "full records 45% (raw) / 50% (gray)", "held-out for the extractor", "measured"),
    ("Google Pay - after patches",        "EasyOCR", "20",  "19/20 full records",         "development",  "measured"),
    ("Google Pay - after patches",        "RapidOCR", "20", "20/20 full records",         "development",  "measured"),
    ("Google Pay - fresh screenshots",    "EasyOCR", "-",   "not scored yet",             "held-out",     "pending"),
    ("Paytm screenshots",                 "EasyOCR", "-",   "template built, set not scored", "held-out", "pending"),
    ("Paper receipts (CORD) - beta",   "EasyOCR", "47",  "61.7% held-out (CI 47.4-74.2)", "held-out", "measured"),
]


def _card(label, value, sub):
    st.markdown(f"""<div class="metric-card"><div class="metric-label">{label}</div>
        <div class="metric-value" style="font-size:1.35rem">{value}</div><div class="metric-sub">{sub}</div></div>""",
                unsafe_allow_html=True)

def _h(text):
    st.markdown(f"""<div style="font-family:'Space Mono',monospace; font-size:0.7rem; color:#5a8a68; text-transform:uppercase;
                letter-spacing:1.5px; margin:1.4rem 0 0.7rem 0">{text}</div>""", unsafe_allow_html=True)

def _note(html, color="#3ddc84"):
    st.markdown(f"""<div style="background:rgba(61,220,132,0.07); border:1px solid #2d4a35; border-left:3px solid {color};
        border-radius:8px; padding:10px 14px; font-size:0.82rem; color:#9dbda8; line-height:1.6;
        font-family:'DM Sans',sans-serif; margin:0.6rem 0">{html}</div>""", unsafe_allow_html=True)


def render_cv_evaluation():
    st.markdown("### Computer Vision: UPI screenshots and paper receipts (beta)")
    c1, c2, c3, c4 = st.columns(4)
    with c1: _card("Google Pay (dev set)", "19 / 20", "full records · EasyOCR · after tuning")
    with c2: _card("Google Pay first contact", "45-50%", "full records before tuning")
    with c3: _card("Paytm", "template built", "test set not scored yet")
    with c4: _card("Fresh Google Pay set", "pending", "held-out test not scored yet")
    _note("<strong style='color:#3ddc84'>How to read these numbers:</strong> the extractor was tuned on the same 20 Google Pay "
          "screenshots it is scored on, so <em>19/20 is development accuracy</em>. The honest transfer figure is the "
          "<em>first-contact</em> result (45-50%), when it met a new OCR engine for the first time. Receipts use a proper "
          "dev/held-out split. Fresh screenshots are the next real test.")

    _h("Google Pay screenshots: first contact vs after patching (EasyOCR, 20 screenshots)")
    fig = go.Figure()
    fig.add_bar(x=UPI_FIELDS, y=UPI_FIRST, name="First contact", marker_color="#f39c12",
                text=[f"{v}%" for v in UPI_FIRST], textposition="outside", textfont=dict(color="#e8f5ec"))
    fig.add_bar(x=UPI_FIELDS, y=UPI_AFTER, name="After patches (dev)", marker_color="#3ddc84",
                text=[f"{v}%" for v in UPI_AFTER], textposition="outside", textfont=dict(color="#e8f5ec"))
    fig.update_layout(barmode="group", yaxis=dict(range=[0, 115], gridcolor="#1c2d21", title="% correct"),
                      xaxis=dict(gridcolor="#1c2d21"), legend=dict(orientation="h", y=1.12), height=380, **LAYOUT)
    st.plotly_chart(fig, use_container_width=True)
    _note("<strong>What EasyOCR did differently:</strong> the rupee sign is read as 8/7/2/r/=/* and glued to the digits "
          "(\"8370\" for Rs 370); the date line is split across boxes with the colon misread; <code>S</code> becomes "
          "<code>$</code>. Fixes: cross-check the big amount against the \"Payment of X completed\" sentence (Paytm: against the "
          "amount written in words), parse the joined date row, and compare names ignoring look-alike characters. "
          "Review flags fell from 20/20 to 2-3/20, so the flag now means something.", "#f39c12")

    _h("Paper receipts (beta)")
    _note(f"Paper receipts (CORD dataset, EasyOCR) work as a <strong>beta</strong> feature and are always flagged for review. Held-out "
          f"total-amount accuracy was <strong>61.7%</strong> (95% CI 47.4-74.2, 47 receipts); the extractor improved from {PROG['v0']}% to "
          f"{PROG['v2']}% on all {PROG['n']} receipts, against an OCR ceiling of {PROG['ceil']}% (total present in the text). Upscaling, "
          f"magnification and CLAHE gave no significant gain, so no further tuning is planned. UPI screenshots are the main feature.", "#f39c12")

    _h("Test status")
    rows = "".join(
        f"""<tr style="border-bottom:1px solid #2d4a35"><td style="padding:8px 12px;color:#e8f5ec">{a}</td>
        <td style="padding:8px 12px;color:#9dbda8">{b}</td><td style="padding:8px 12px;color:#9dbda8;text-align:center">{c}</td>
        <td style="padding:8px 12px;color:#e8f5ec">{d}</td><td style="padding:8px 12px;color:#9dbda8">{e}</td>
        <td style="padding:8px 12px;color:{'#3ddc84' if f == 'measured' else '#f39c12'}">{f}</td></tr>"""
        for a, b, c, d, e, f in STATUS)
    st.markdown(f"""<div style="background:#1c2d21;border:1px solid #2d4a35;border-radius:10px;overflow:hidden">
        <table style="width:100%;border-collapse:collapse;font-size:0.8rem;font-family:'DM Sans',sans-serif">
        <thead><tr style="background:#162318;color:#3ddc84;font-family:'Space Mono',monospace;font-size:0.68rem;text-align:left">
        <th style="padding:9px 12px">Dataset</th><th style="padding:9px 12px">Engine</th><th style="padding:9px 12px">n</th>
        <th style="padding:9px 12px">Result</th><th style="padding:9px 12px">Kind of test</th><th style="padding:9px 12px">Status</th></tr></thead>
        <tbody>{rows}</tbody></table></div>""", unsafe_allow_html=True)

    _h("Known limitations")
    st.markdown("""
- Names with a digit that OCR reads as a letter ("1MG" read as "IMG") cannot be corrected from the screenshot; the review step catches them.
- If both amount readings glue the **same** wrong digit onto the amount, the extractor cannot tell. Paytm is protected by the amount-in-words check; Google Pay is not.
- Paper receipts are beta (about 62% of totals right on held-out data) and are always flagged for review. Foreign-currency receipts are detected but not converted; you enter the exchange rate.
- Only Google Pay and Paytm layouts are built. Other apps are read generically and always flagged.""")
