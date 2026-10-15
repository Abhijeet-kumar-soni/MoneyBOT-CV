"""vision_engine.py - image bytes -> OCR -> structured payment record: UPI screenshots (Google Pay / Paytm), paper receipts as beta.
No Streamlit imports. OCR engine: EasyOCR if it installs/loads, else RapidOCR (both validated on the dev set)."""
import io, re, time
import numpy as np
import upi_extractor, receipt_extractor

MAX_SIDE = 4800
_OCR = None

def get_ocr():
    """Returns (engine_name, fn(bgr_image) -> detections). Detections: text, conf, x0, y0, x1, y1, h."""
    global _OCR
    if _OCR is not None:
        return _OCR
    errors = []
    try:
        import easyocr, torch
        reader = easyocr.Reader(["en"], gpu=torch.cuda.is_available(), verbose=False)
        def run(img):
            out = []
            for bbox, text, conf in reader.readtext(img):
                p = np.array(bbox, dtype=np.float32)
                x0, y0 = p.min(axis=0); x1, y1 = p.max(axis=0)
                out.append({"text": text, "conf": float(conf), "x0": float(x0), "y0": float(y0),
                            "x1": float(x1), "y1": float(y1), "h": float(y1 - y0)})
            return out
        _OCR = ("EasyOCR", run)
        return _OCR
    except Exception as e:
        errors.append(f"easyocr: {e}")
    try:
        from rapidocr_onnxruntime import RapidOCR
        eng = RapidOCR()
        def run(img):
            res, _ = eng(img)
            out = []
            for box, text, score in (res or []):
                p = np.array(box, dtype=float)
                x0, y0 = p.min(axis=0); x1, y1 = p.max(axis=0)
                out.append({"text": text, "conf": float(score), "x0": float(x0), "y0": float(y0),
                            "x1": float(x1), "y1": float(y1), "h": float(y1 - y0)})
            return out
        _OCR = ("RapidOCR", run)
        return _OCR
    except Exception as e:
        errors.append(f"rapidocr: {e}")
    raise RuntimeError("No OCR engine available. Install one:  pip install easyocr   or   pip install rapidocr-onnxruntime\n"
                       + " | ".join(errors))

def decode(data):
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    arr = np.array(im)[:, :, ::-1].copy()                       # RGB -> BGR
    s = MAX_SIDE / max(arr.shape[:2])
    if s < 1:
        import cv2
        arr = cv2.resize(arr, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return arr

def overlay_image(data, dets):
    """RGB image with OCR boxes (green >=0.7, orange >=0.4, red below) - the 'how it was read' view."""
    import cv2
    img = decode(data)
    for d in dets:
        col = (0, 200, 0) if d["conf"] >= 0.7 else (0, 165, 255) if d["conf"] >= 0.4 else (0, 0, 255)
        cv2.rectangle(img, (int(d["x0"]), int(d["y0"])), (int(d["x1"]), int(d["y1"])), col, 3)
    return img[:, :, ::-1]

STOP_WORDS = re.compile(r"survey|tell us|visit|thank|welcome|www|\.com|code|complete|enter|free|feedback|reward|win\b|chance|"
                        r"customer copy|invoice|cash memo|bill no|receipt|order|^tel|phone|\bph\b|mob|gstin|\bgst\b|date|time", re.I)

def _merchant_from_lines(lines):
    """First of the opening lines that reads like a shop name (not a slogan, address, phone or survey blurb)."""
    for ln in lines[:14]:
        t = ln["text"].strip()
        letters = sum(ch.isalpha() for ch in t)
        core = len(t.replace(" ", ""))
        if (letters >= 4 and core and letters / core >= 0.6 and not STOP_WORDS.search(t)
                and not t[0].isdigit() and len(t.split()) <= 7):
            return t
    return None

def _currency(text):
    if re.search(r"₹|\brs\.?\s*\d|\binr\b", text, re.I):
        return "INR"
    if re.search(r"\$|\busd\b", text, re.I) or re.search(r"\bS\d{1,6}\.\d{2}\b", text):     # '$' is often read as 'S'
        return "USD"
    if "€" in text or re.search(r"\beur\b", text, re.I):
        return "EUR"
    if "£" in text or re.search(r"\bgbp\b", text, re.I):
        return "GBP"
    return "INR"

def _receipt_date(text):
    """dd/mm/yyyy (India), mm/dd/yyyy (US, detected when the 2nd number is > 12) or yyyy-mm-dd, optional time."""
    flags = []
    m = re.search(r"(20\d\d)[/\-.](\d{1,2})[/\-.](\d{1,2})", text)
    if m:
        y, mo, d, end = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.end()
    else:
        m = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", text)
        if not m:
            return None, flags
        a, b, y, end = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.end()
        y = y + 2000 if y < 100 else y
        if a > 12:
            d, mo = a, b
        elif b > 12:
            mo, d = a, b
        else:
            d, mo = a, b
            flags.append("date_format_assumed")
    if not (1 <= d <= 31 and 1 <= mo <= 12 and 2000 <= y <= 2100):
        return None, flags
    hh, mm = 12, 0
    t = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?", text[end:end + 25])
    if t:
        h, mi = int(t.group(1)), int(t.group(2))
        if t.group(3):
            h = h % 12 + (12 if t.group(3).lower() == "pm" else 0)
        if 0 <= h <= 23 and 0 <= mi <= 59:
            hh, mm = h, mi
    return f"{y:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mm:02d}", flags

def _receipt_record(dets):
    lines = receipt_extractor.group_lines(dets)
    text = " ".join(l["text"] for l in lines)
    total, _ = receipt_extractor.extract_total(dets, keep_decimals=True)
    merchant = _merchant_from_lines(lines) or "Receipt"
    dt, dflags = _receipt_date(text)
    currency = _currency(text)
    flags = ["receipt_beta"] + dflags + ([] if total else ["no_amount"]) + ([] if dt else ["no_date"])
    if currency != "INR":
        flags.append("foreign_currency")
    return {"kind": "expense", "reason": None, "template": "receipt", "direction": "debit", "status": "success",
            "amount": total, "merchant": merchant, "datetime": dt, "txn_id": None, "currency": currency,
            "category": upi_extractor.categorise_merchant(merchant), "flags": flags, "notes": [],
            "confidence": round(float(np.mean([d["conf"] for d in dets])), 3) if dets else 0.0,
            "recordable": bool(total), "needs_review": True}

def scan_image(data, ocr=None):
    """-> {record, doc_type ('upi'|'receipt'), dets, engine, seconds}. UPI screens use the UPI extractor; everything else is read as a receipt (beta)."""
    name, run = ocr or get_ocr()
    t0 = time.perf_counter()
    dets = run(decode(data))
    rec = upi_extractor.extract_upi(dets)
    is_upi = (rec.get("template") in ("gpay", "paytm") or upi_extractor.looks_like_upi_screen(dets)
              or bool(rec.get("txn_id") and rec.get("amount") and rec.get("status") in ("success", "failed")))
    doc = "upi" if is_upi else "receipt"
    if doc == "receipt" and dets:
        rec = _receipt_record(dets)
    return {"record": rec, "doc_type": doc, "dets": dets, "engine": name, "seconds": round(time.perf_counter() - t0, 2)}
