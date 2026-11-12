"""Turn an invoice file (digital PDF, scanned PDF or image) into structured fields.

Strategy
  1. Read the document: text layer if it has one, otherwise mark it as scanned.
  2. Primary extractor: a vision LLM reads the original file and returns JSON (schema below).
  3. Independent rule-based parser (text layer or Tesseract OCR) runs as a cross-check, and as the
     fallback if the LLM is not configured or fails - so the process never stops dead.
"""
import os
import re
from datetime import date, datetime

import pdfplumber

from . import llm

SCHEMA_PROMPT = """You are an accounts-payable data extraction engine. Read this vendor invoice and return ONLY a JSON
object with exactly these keys:
{
 "vendor_name": string|null, "vendor_gstin": string|null,
 "buyer_name": string|null, "buyer_gstin": string|null,
 "invoice_number": string|null, "invoice_date": "YYYY-MM-DD"|null,
 "po_reference": string|null,
 "currency": string,
 "line_items": [{"description": string, "quantity": number, "unit_price": number, "amount": number}],
 "subtotal": number|null,
 "tax_lines": [{"type": "CGST"|"SGST"|"IGST"|"UTGST"|"OTHER", "rate": number|null, "amount": number}],
 "tax_total": number|null,
 "total": number|null,
 "amounts_include_tax": boolean,
 "bank_name": string|null, "bank_account": string|null, "bank_ifsc": string|null,
 "payment_terms": string|null,
 "notes": [string],
 "confidence": {"vendor": 0-1, "invoice_number": 0-1, "invoice_date": 0-1, "po_reference": 0-1, "total": 0-1,
                "line_items": 0-1, "payment_terms": 0-1, "bank_account": 0-1}
}
Rules:
- Copy identifiers exactly as printed. NEVER invent or infer a value that is not printed: use null.
- po_reference: only a purchase order / buyer's order number explicitly printed on the invoice, else null.
- Dates in Indian invoices are day-first (04/11/2026 = 4 Nov 2026). Output ISO format.
- Numbers as plain numbers without commas or currency symbols.
- amounts_include_tax = true only if the invoice says prices/amounts are inclusive of GST/tax.
- subtotal = pre-tax taxable value if printed. tax_total = sum of tax lines.
- payment_terms: as printed (e.g. "Net 30 days", "Due on receipt"), else null.
- notes: copy any payment instructions or unusual statements (e.g. change of bank details, urgency).
- confidence: your certainty for each field (lower for blurry scans or ambiguous layouts)."""

NUM = r"-?\d[\d,]*(?:\.\d+)?"
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def to_num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r"(rs\.?|inr|₹)", "", str(s), flags=re.I).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip().rstrip(".,")
    pats = [
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", lambda m: (int(m[1]), int(m[2]), int(m[3]))),
        (r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", lambda m: (int(m[3]), int(m[2]), int(m[1]))),
        (r"(\d{1,2})[- ]([A-Za-z]{3,9})[- ,]+(\d{4})",
         lambda m: (int(m[3]), MONTHS.get(m[2][:3].lower()), int(m[1]))),
        (r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", lambda m: (int(m[3]), MONTHS.get(m[1][:3].lower()), int(m[2]))),
    ]
    for p, f in pats:
        m = re.search(p, s)
        if m:
            try:
                y, mo, d = f(m)
                return date(y, mo, d).isoformat()
            except (TypeError, ValueError):
                continue
    return None


DATE_ANY = r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.]\d{1,2}[/.]\d{4}|\d{1,2}[- ][A-Za-z]{3,9}[- ,]+\d{4})"


# ------------------------------------------------------------------ reading
def read_document(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        return {"kind": "scanned", "pages": 1, "text": "", "chars": 0,
                "mime": "image/png" if ext == ".png" else ("image/webp" if ext == ".webp" else "image/jpeg")}
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        pages = len(pdf.pages)
    chars = len(re.sub(r"\s", "", text))
    kind = "digital" if chars >= 40 * max(1, pages) else "scanned"
    return {"kind": kind, "pages": pages, "text": text if kind == "digital" else "", "chars": chars,
            "mime": "application/pdf"}


def ocr(path: str) -> tuple[str, float]:
    """Returns (text, mean word confidence 0-1) using Tesseract's own per-word confidence."""
    import pytesseract
    from PIL import Image
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        imgs = [Image.open(path)]
    else:
        with pdfplumber.open(path) as pdf:
            imgs = [p.to_image(resolution=300).original.copy() for p in pdf.pages]
    lines, confs = [], []
    for im in imgs:
        d = pytesseract.image_to_data(im.convert("L"), config="--psm 6", output_type=pytesseract.Output.DICT)
        cur, key = [], None
        for i, word in enumerate(d["text"]):
            k = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            if k != key and cur:
                lines.append(" ".join(cur))
                cur = []
            key = k
            if word.strip():
                cur.append(word)
                c = float(d["conf"][i])
                if c >= 0:
                    confs.append(c)
        if cur:
            lines.append(" ".join(cur))
    return "\n".join(lines), (sum(confs) / len(confs) / 100 if confs else 0.0)


# ------------------------------------------------------------------ rule-based parser
def _label_value(text, labels, value_pat):
    for lab in labels:
        m = re.search(lab + r"\s*[:#.]*\s*" + value_pat, text, re.I)
        if m:
            return m.group(1).strip()
    return None


def _last_num(line):
    nums = re.findall(NUM, line)
    return to_num(nums[-1]) if nums else None


def _is_num(tok):
    return re.fullmatch(r"(?:rs\.?|inr)?" + NUM, tok, re.I) is not None


def rule_parse(text: str, buyer_gstin: str | None = None) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    flat = "\n".join(lines)
    out = {"line_items": [], "tax_lines": [], "notes": [], "currency": "INR"}

    # Vendor: first meaningful line; GSTINs: vendor = first one that isn't ours
    skip = re.compile(r"^(tax invoice|invoice|bill)$", re.I)
    for l in lines[:4]:
        cand = re.sub(r"^[A-Z]\s+(?=[A-Z][a-z])", "", l)  # drop logo letter
        if len(cand) > 4 and not skip.match(cand) and not re.search(r"\d{3,}", cand):
            out["vendor_name"] = re.sub(r"\s+(tax\s+)?invoice$", "", cand, flags=re.I)
            break
    gstins = re.findall(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z])\b", flat.upper())
    others = [g for g in gstins if g != (buyer_gstin or "")]
    out["vendor_gstin"] = others[0] if others else None
    out["buyer_gstin"] = buyer_gstin if buyer_gstin in gstins else (gstins[1] if len(gstins) > 1 else None)

    inv_val = r"([A-Z0-9][A-Z0-9/\-_.]*\d[A-Z0-9/\-_.]*)"
    out["invoice_number"] = _label_value(flat, [r"invoice\s*(?:no\.?|number|#)", r"bill\s*no\.?",
                                                r"invoice\s*ref(?:erence)?", r"inv\.?\s*no\.?"], inv_val)
    d = _label_value(flat, [r"invoice\s*date", r"bill\s*date", r"dated", r"(?<!due )(?<!po )date"], DATE_ANY)
    out["invoice_date"] = parse_date(d) if d else None
    out["po_reference"] = _label_value(
        flat, [r"\bP\.?O\.?\s*(?:number|no\.?|#)", r"purchase\s*order\s*(?:no\.?|number)?",
               r"buyer'?s\s*order\s*(?:no\.?)?", r"your\s*PO", r"order\s*ref\.?"],
        r"([A-Z]{0,3}[\s/\-]?\d{3,})")

    for l in lines:
        low = l.lower()
        toks = l.split()
        # line items: trailing qty, rate, amount where qty*rate == amount
        trail = []
        for t in reversed(toks):
            if _is_num(t):
                trail.insert(0, to_num(t))
            else:
                break
        if len(trail) >= 3:
            qy, pr, am = trail[-3:]
            if qy and pr and am and qy > 0 and abs(qy * pr - am) <= max(1.0, 0.005 * am):
                desc_toks = toks[:len(toks) - len(trail)]
                extra = trail[:-3]  # e.g. HSN code, or a year that belongs to the description
                keep = [f"{int(e)}" for e in extra if 1990 <= e <= 2100]
                if desc_toks and re.fullmatch(r"\d{1,3}", desc_toks[0]):
                    desc_toks = desc_toks[1:]
                desc = " ".join(desc_toks + keep).strip(" -:")
                if desc and not re.search(r"total|gst|amount payable", desc, re.I):
                    out["line_items"].append({"description": desc, "quantity": qy, "unit_price": pr, "amount": am})
                    continue
        m = re.search(r"\b(CGST|SGST|IGST|UTGST)\b\s*@?\s*(\d+(?:\.\d+)?)?\s*%?", l, re.I)
        if m and _last_num(l) is not None and not re.search(r"GSTIN", l, re.I):
            out["tax_lines"].append({"type": m.group(1).upper(), "rate": to_num(m.group(2)), "amount": _last_num(l)})
            if "include" in low:
                out["amounts_include_tax"] = True
            continue
        if re.search(r"sub\s*-?\s*total|taxable\s*(value|amount)", low) and _last_num(l) is not None:
            out["subtotal"] = _last_num(l)
            continue
        if re.search(r"grand\s*total|total\s*amount|total\s*payable|amount\s*payable|invoice\s*total|net\s*payable"
                     r"|^total\b", low) and _last_num(l) is not None:
            out["total"] = _last_num(l)
            continue
        if re.search(r"have changed|has changed|new account|update your records|urgent", low):
            out["notes"].append(l)

    if re.search(r"inclusive of (gst|tax)|incl\.?\s*gst|includes\s+[ics]?gst", flat, re.I):
        out["amounts_include_tax"] = True
    out.setdefault("amounts_include_tax", False)
    acct = re.search(r"(?:A/?c\.?(?:\s*No\.?)?|Account\s*(?:No\.?|Number))\s*[:.]?\s*(\d{9,18})", flat, re.I)
    out["bank_account"] = acct.group(1) if acct else None
    ifsc = re.search(r"\b([A-Z]{4}0[A-Z0-9]{6})\b", flat)
    out["bank_ifsc"] = ifsc.group(1) if ifsc else None
    bank = re.search(r"Bank\s*:\s*([A-Za-z .]+?Bank)", flat) or re.search(r"(?:Pay to|Remit to)\s*:?\s*([A-Za-z .]+?Bank)", flat) \
        or re.search(r"^([A-Z][A-Za-z .]*?Bank(?: of [A-Z][a-z]+)?)\b", flat, re.M)
    out["bank_name"] = bank.group(1).strip() if bank else None
    terms = re.search(r"(?:payment\s*terms?|terms)\s*[:\-]?\s*(net\s*\d+(?:\s*days?)?|\d+\s*days|due\s*on\s*receipt|"
                      r"immediate)", flat, re.I) or re.search(r"(due\s*within\s*\d+\s*days)", flat, re.I)
    out["payment_terms"] = terms.group(1).strip() if terms else None
    if out["tax_lines"]:
        out["tax_total"] = round(sum(t["amount"] for t in out["tax_lines"]), 2)
    out.setdefault("subtotal", None)
    out.setdefault("total", None)
    out.setdefault("tax_total", None)
    out["confidence"] = {}
    return out


# ------------------------------------------------------------------ orchestration
def normalise(f: dict) -> dict:
    f = dict(f or {})
    for k in ("subtotal", "tax_total", "total"):
        f[k] = to_num(f.get(k))
    items = []
    for it in f.get("line_items") or []:
        items.append({"description": str(it.get("description") or "").strip(),
                      "quantity": to_num(it.get("quantity")), "unit_price": to_num(it.get("unit_price")),
                      "amount": to_num(it.get("amount"))})
    f["line_items"] = items
    f["tax_lines"] = [{"type": (t.get("type") or "OTHER").upper(), "rate": to_num(t.get("rate")),
                       "amount": to_num(t.get("amount"))} for t in (f.get("tax_lines") or [])]
    if f.get("tax_total") is None and f["tax_lines"]:
        f["tax_total"] = round(sum(t["amount"] or 0 for t in f["tax_lines"]), 2)
    if f.get("invoice_date"):
        f["invoice_date"] = parse_date(str(f["invoice_date"])) or f["invoice_date"]
    for k in ("vendor_gstin", "buyer_gstin", "bank_ifsc"):
        if f.get(k):
            f[k] = re.sub(r"\s", "", str(f[k])).upper()
    if f.get("bank_account"):
        f["bank_account"] = re.sub(r"\D", "", str(f["bank_account"]))
    for k in ("invoice_number", "po_reference", "vendor_name"):
        v = f.get(k)
        f[k] = str(v).strip() if v not in (None, "", "null", "N/A", "-") else None
    f["notes"] = [n for n in (f.get("notes") or []) if n]
    v = f.get("payment_terms")
    f["payment_terms"] = str(v).strip() if v not in (None, "", "null") else None
    f["payment_terms_days"] = terms_days(f["payment_terms"])
    f["amounts_include_tax"] = bool(f.get("amounts_include_tax"))
    f.setdefault("confidence", {})
    return f


def terms_days(t):
    if not t:
        return None
    if re.search(r"receipt|immediate", t, re.I):
        return 0
    m = re.search(r"(\d+)", t)
    return int(m.group(1)) if m else None


CONF_FIELDS = [("vendor", "Vendor / GSTIN", True), ("invoice_number", "Invoice number", True),
               ("invoice_date", "Invoice date", True), ("po_reference", "PO reference", False),
               ("total", "Total amount", True), ("line_items", "Line items", True),
               ("payment_terms", "Payment terms", False), ("bank_account", "Bank account", False)]


def field_confidence(f: dict, base: float, cross: list) -> dict:
    """Per-field confidence 0-1. Reader's own score (AI) or a base score (text layer / OCR mean),
    capped at 0.5 when the two independent readers disagree. Missing required field = 0."""
    reader = f.get("confidence") or {}
    dis = {c["field"] for c in cross if not c["agree"] and c["rules"] is not None}
    out = {}
    for key, _, required in CONF_FIELDS:
        val = (f.get("vendor_gstin") or f.get("vendor_name")) if key == "vendor" else f.get(key)
        if not val:
            out[key] = 0.0 if required else None
            continue
        c = reader.get(key)
        c = float(c) if isinstance(c, (int, float)) else base
        if key in dis or (key == "vendor" and "vendor_gstin" in dis):
            c = min(c, 0.5)
        out[key] = round(max(0.0, min(1.0, c)), 2)
    return out


KEY_FIELDS = ["invoice_number", "invoice_date", "po_reference", "total", "vendor_gstin"]


def compare(a: dict, b: dict) -> list[dict]:
    """Field-by-field agreement between the AI read and the rule-based read."""
    from .db import norm_invoice_no
    rows = []
    for k in KEY_FIELDS:
        va, vb = a.get(k), b.get(k)
        if k == "total":
            same = va is not None and vb is not None and abs(va - vb) < 1
        elif k in ("invoice_number", "po_reference"):
            same = norm_invoice_no(va) == norm_invoice_no(vb)
        else:
            same = (va or None) == (vb or None)
        rows.append({"field": k, "ai": va, "rules": vb, "agree": same})
    return rows


def extract(path: str, doc: dict, buyer_gstin: str) -> dict:
    """Returns {fields, method, reader, cross_check, warnings, ocr_text}"""
    warnings = []
    text = doc["text"]
    reader = "text layer"
    ocr_conf = None
    if doc["kind"] == "scanned":
        try:
            text, ocr_conf = ocr(path)
            reader = f"Tesseract OCR (mean word confidence {ocr_conf:.0%})"
        except Exception as e:  # noqa: BLE001
            warnings.append(f"OCR unavailable: {e}")
            text = ""
    rules = normalise(rule_parse(text, buyer_gstin)) if text.strip() else None

    ai = None
    if llm.provider():
        try:
            with open(path, "rb") as fh:
                ai = normalise(llm.extract(fh.read(), doc["mime"], SCHEMA_PROMPT,
                                           budget=20 if (doc["kind"] == "digital" and rules) else None))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"AI extraction failed ({type(e).__name__}: {str(e)[:120]}); fell back to rule parser")

    if ai:
        desc = llm.describe()
        method = f"AI vision ({desc['provider']} · {desc['model']})"
        cross = compare(ai, rules) if rules else []
        ai["field_confidence"] = field_confidence(ai, 0.9, cross)
        return {"fields": ai, "method": method, "reader": reader, "cross_check": cross,
                "warnings": warnings, "ocr_text": text if doc["kind"] == "scanned" else None}
    if rules:
        method = "Rule-based parser on " + ("OCR text" if doc["kind"] == "scanned" else "PDF text layer")
        base = round(ocr_conf, 2) if ocr_conf is not None else 0.98
        rules["field_confidence"] = field_confidence(rules, base, [])
        return {"fields": rules, "method": method, "reader": reader, "cross_check": [], "warnings": warnings,
                "ocr_text": text if doc["kind"] == "scanned" else None}
    return {"fields": None, "method": "none", "reader": reader, "cross_check": [], "warnings": warnings,
            "ocr_text": None}


def today() -> date:
    o = os.environ.get("TODAY")
    return datetime.strptime(o, "%Y-%m-%d").date() if o else date.today()
