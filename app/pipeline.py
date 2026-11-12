"""The invoice pipeline: 10 visible stages from PDF to a reasoned decision.

Design principle: AI reads the document; deterministic rules make the decision. Every outcome traces
back to named checks with an owner and a next action, and everything is written to an audit trail.
"""
import hashlib
import os
import queue
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta

from rapidfuzz import fuzz

from . import db, gstin
from .extract import CONF_FIELDS, extract, normalise, read_document, today
from .rules import RULES

STAGES = [
    ("intake", "Invoice received"),
    ("read", "Read document"),
    ("extract", "Data extraction"),
    ("quality", "Data quality checks"),
    ("vendor", "Vendor verification"),
    ("duplicate", "Duplicate detection"),
    ("po_match", "Purchase order matching"),
    ("reconcile", "Line & amount reconciliation"),
    ("decision", "Decision"),
    ("action", "Action & routing"),
]
SEV = {"pass": 0, "info": 1, "warn": 2, "hold": 3, "reject": 4, "skipped": 0}
DELAY = float(os.environ.get("STAGE_DELAY", "0.45"))
LEGAL_SUFFIX = re.compile(r"\b(pvt|private|ltd|limited|llp|inc|co|company|india)\b\.?", re.I)


def inr(x):
    if x is None:
        return "-"
    neg = x < 0
    x = abs(round(float(x), 2))
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts) + "," + tail
    return ("-" if neg else "") + "₹" + whole + ("" if frac == "00" else "." + frac)


def norm_name(s):
    s = LEGAL_SUFFIX.sub(" ", (s or "").lower())
    return re.sub(r"[^a-z0-9 ]", " ", s).split()


def name_score(a, b):
    return fuzz.token_sort_ratio(" ".join(norm_name(a)), " ".join(norm_name(b)))


def norm_po(ref):
    if not ref:
        return None
    nums = re.findall(r"\d{3,}", ref)
    return f"PO-{nums[-1]}" if nums else None


def check(code, severity, title, detail="", owner=None, action=None):
    return {"code": code, "severity": severity, "title": title, "detail": detail, "owner": owner, "action": action}


def worst(checks, default="pass"):
    if not checks:
        return default
    return max((c["severity"] for c in checks), key=lambda s: SEV[s])


# ===================================================================== reconciliation (pure)
def reconcile(f, po, vendor, net, buyer_gstin):
    """Compare invoice against a PO. Returns (checks, table, allocation, summary)."""
    checks, rows, alloc = [], [], []
    rate = po["gst_rate"]
    incl = f["amounts_include_tax"]
    open_lines = {l["line_no"]: dict(l, open_qty=max(0.0, l["qty"] - l["invoiced_qty"]),
                                     billable_qty=max(0.0, l["received_qty"] - l["invoiced_qty"])) for l in po["lines"]}
    three_way = bool(po.get("receipt_required"))
    price_tol = RULES["price_tolerance_pct"]
    remaining = po["remaining_value"]
    allowed = min(remaining * RULES["amount_tolerance_pct"] / 100, RULES["amount_tolerance_abs"])

    # Bundled invoice: one lump-sum line covering a multi-line PO. Accept only if no single PO line matches it
    # and the total equals the open PO value within tolerance; then allocate it across every open PO line.
    open_now = [l for l in open_lines.values() if l["open_qty"] > 0]
    bundled = False
    if len(f["line_items"]) == 1 and len(open_now) > 1:
        it0 = f["line_items"][0]
        single = max(fuzz.token_set_ratio(it0["description"].lower(), l["item"].lower()) for l in open_now)
        if single < RULES["line_match_threshold"] and abs(net - remaining) <= allowed + RULES["rounding_tolerance"]:
            bundled = True
            checks.append(check("BUNDLED", "warn", f"Bundled invoice — one lump-sum line for {len(open_now)} PO lines",
                                f"'{it0['description']}' ({inr(net)}) doesn't itemise; accepted because it equals the open "
                                f"value of {po['po_number']} ({inr(remaining)}) within tolerance. Allocated line-by-line "
                                "to the PO so quantities and goods receipts are still checked."))
            for l in open_now:
                qty = l["open_qty"]
                status = "pass"
                if three_way:
                    if qty > l["billable_qty"] + 1e-9:
                        status = "hold"
                        checks.append(check("GOODS_NOT_RECEIVED", "hold",
                                            f"Billed for {qty - l['billable_qty']:g} unit(s) not yet received",
                                            f"'{l['item']}' (bundled): {qty:g} billed, {l['billable_qty']:g} received and unbilled.",
                                            "Stores / receiving", "Confirm delivery before paying the bundled invoice."))
                rows.append({"invoice_item": "↳ bundled", "po_item": l["item"], "match": 0, "qty": qty,
                             "open_qty": l["open_qty"], "received": l["billable_qty"] if three_way else "n/a (service)",
                             "price": l["unit_price"], "po_price": l["unit_price"], "variance_pct": 0.0,
                             "amount": qty * l["unit_price"], "status": status})
                alloc.append({"line_no": l["line_no"], "qty": qty})
            if three_way and not any(c["code"] == "GOODS_NOT_RECEIVED" for c in checks):
                checks.append(check("THREE_WAY_OK", "pass", "Three-way match: all bundled PO lines received",
                                    "Every PO line covered by the lump sum has a goods receipt."))

    for it in ([] if bundled else f["line_items"]):
        best, score = None, 0
        for l in open_lines.values():
            s = fuzz.token_set_ratio(it["description"].lower(), l["item"].lower())
            if s > score:
                best, score = l, s
        qty = it["quantity"] or 0
        price = it["unit_price"] or 0
        net_price = price / (1 + rate / 100) if incl else price
        if not best or score < RULES["line_match_threshold"]:
            rows.append({"invoice_item": it["description"], "po_item": "— no match on PO", "match": round(score),
                         "qty": qty, "open_qty": None, "received": None, "price": net_price, "po_price": None,
                         "variance_pct": None, "amount": it["amount"], "status": "hold"})
            checks.append(check("LINE_NOT_ON_PO", "hold", f"Item not on {po['po_number']}",
                                f"'{it['description']}' does not match any line on the PO (best similarity {score:.0f}%).",
                                "Procurement", "Confirm whether this item was ordered; amend PO or ask vendor for a credit note."))
            continue
        var = (net_price - best["unit_price"]) / best["unit_price"] * 100 if best["unit_price"] else 0
        status = "pass"
        if var > price_tol + 1e-9:
            status = "hold"
            checks.append(check("PRICE_VARIANCE", "hold", f"Unit price {var:.1f}% above PO on '{best['item']}'",
                                f"Billed {inr(net_price)} vs PO {inr(best['unit_price'])} per unit "
                                f"(tolerance {price_tol:.1f}%). Over-billing on this line: "
                                f"{inr((net_price - best['unit_price']) * qty)}.",
                                "Procurement", "Ask the buyer to confirm the agreed price, or request a revised invoice / credit note."))
        elif var > 0.005:
            status = "warn"
            checks.append(check("PRICE_WITHIN_TOL", "warn", f"Unit price {var:.2f}% above PO on '{best['item']}' — within tolerance",
                                f"Billed {inr(net_price)} vs PO {inr(best['unit_price'])}. Accepted under the "
                                f"{price_tol:.1f}% price tolerance; extra cost {inr((net_price - best['unit_price']) * qty)}."))
        elif var < -0.005:
            checks.append(check("PRICE_BELOW_PO", "info", f"Unit price below PO on '{best['item']}'",
                                f"Billed {inr(net_price)} vs PO {inr(best['unit_price'])} — favourable to us."))
        if qty > best["open_qty"] + 1e-9:
            status = "hold"
            checks.append(check("QTY_EXCEEDS_OPEN", "hold", f"Quantity billed exceeds what is still open on {po['po_number']}",
                                f"'{best['item']}': billed {qty:g}, but only {best['open_qty']:g} open "
                                f"({best['qty']:g} ordered, {best['invoiced_qty']:g} already invoiced on earlier invoices).",
                                "Procurement", "Check goods receipt. If extra units were delivered, amend the PO; otherwise ask vendor to re-bill for the open quantity."))
        if three_way:
            grns = ", ".join(sorted({g["grn_number"] for g in po.get("grns", []) if g["line_no"] == best["line_no"]})) or "none"
            if qty > best["billable_qty"] + 1e-9:
                status = "hold"
                short = qty - best["billable_qty"]
                checks.append(check("GOODS_NOT_RECEIVED", "hold", f"Billed for {short:g} unit(s) not yet received",
                                    f"'{best['item']}': billed {qty:g}; stores received {best['received_qty']:g} "
                                    f"(GRN: {grns}), of which {best['invoiced_qty']:g} already invoiced → only "
                                    f"{best['billable_qty']:g} can be billed now.",
                                    "Stores / receiving", "Confirm delivery. Pay only for received units, or ask the "
                                    "vendor to re-bill when the balance is delivered."))
            else:
                checks.append(check("THREE_WAY_OK", "pass", f"Three-way match: '{best['item']}' received",
                                    f"Billed {qty:g} ≤ {best['billable_qty']:g} received and not yet invoiced (GRN: {grns})."))
        rows.append({"invoice_item": it["description"], "po_item": best["item"], "match": round(score), "qty": qty,
                     "open_qty": best["open_qty"], "received": best["billable_qty"] if three_way else "n/a (service)",
                     "price": round(net_price, 2), "po_price": best["unit_price"],
                     "variance_pct": round(var, 2), "amount": it["amount"], "status": status})
        alloc.append({"line_no": best["line_no"], "qty": qty})
        best["open_qty"] -= qty
        best["billable_qty"] -= qty
    if not three_way:
        checks.append(check("TWO_WAY", "info", "Service PO — two-way match",
                            "Services have no goods receipt; matched invoice ↔ PO only. Service confirmation is "
                            "covered by the PO owner."))

    over = net - remaining
    if over > allowed + RULES["rounding_tolerance"]:
        checks.append(check("AMOUNT_EXCEEDS_PO", "hold", f"Invoice exceeds remaining value on {po['po_number']} by {inr(over)}",
                            f"Pre-tax invoice value {inr(net)} vs remaining PO value {inr(remaining)} "
                            f"(PO {inr(po['value'])}, already invoiced {inr(po['invoiced_value'])}). Allowed over-run is {inr(allowed)}.",
                            "Procurement", "Raise a PO amendment for the excess or reject the excess portion."))
    elif over > RULES["rounding_tolerance"]:
        checks.append(check("AMOUNT_WITHIN_TOL", "warn", f"Invoice is {inr(over)} above remaining PO value — within tolerance",
                            f"Allowed over-run is min({RULES['amount_tolerance_pct']:.1f}%, {inr(RULES['amount_tolerance_abs'])}) = {inr(allowed)}."))
    else:
        checks.append(check("AMOUNT_OK", "pass", "Invoice value within remaining PO balance",
                            f"{inr(net)} billed against {inr(remaining)} still open on {po['po_number']}."))

    # GST type & rate
    vstate, bstate = gstin.state_of(vendor["gstin"]), gstin.state_of(buyer_gstin)
    types = {t["type"] for t in f["tax_lines"]}
    tax = f.get("tax_total") or 0
    if vstate and bstate:
        intra = vstate == bstate
        expected = "CGST + SGST" if intra else "IGST"
        wrong = (intra and "IGST" in types) or (not intra and types & {"CGST", "SGST"})
        if wrong:
            checks.append(check("GST_TYPE", "hold", "Wrong GST type for this supply",
                                f"Vendor state {gstin.STATE_CODES.get(vstate, vstate)} → buyer "
                                f"{gstin.STATE_CODES.get(bstate, bstate)} requires {expected}; invoice charges {', '.join(sorted(types))}.",
                                "Tax team", "Ask vendor for a corrected tax invoice; wrong GST type blocks input tax credit."))
        elif types:
            checks.append(check("GST_TYPE", "pass", f"GST type correct ({expected})",
                                f"{'Intra' if intra else 'Inter'}-state supply "
                                f"({gstin.STATE_CODES.get(vstate, vstate)} → {gstin.STATE_CODES.get(bstate, bstate)})."))
    eff = tax / net * 100 if net else 0
    if abs(eff - rate) > RULES["tax_rate_tolerance_pp"]:
        checks.append(check("GST_RATE", "hold", f"GST rate {eff:.1f}% does not match PO rate {rate:g}%",
                            f"Tax {inr(tax)} on taxable value {inr(net)}.", "Tax team",
                            "Confirm HSN/SAC rate with vendor; request corrected invoice."))
    else:
        checks.append(check("GST_RATE", "pass", f"GST rate {eff:.0f}% matches PO", f"Tax {inr(tax)} on {inr(net)}."))
    inv_date = f.get("invoice_date")
    if inv_date and inv_date < po["po_date"]:
        checks.append(check("DATE_BEFORE_PO", "warn", "Invoice dated before the PO",
                            f"Invoice {inv_date} vs PO {po['po_date']} — possible retro-PO; noted for audit."))
    summary = {"po_value": po["value"], "already_invoiced": po["invoiced_value"], "remaining_before": remaining,
               "this_invoice": net, "remaining_after": remaining - net, "over": over, "allowed_over": allowed}
    return checks, rows, alloc, summary


def infer_po(f, vendor, net):
    cands = []
    for p in db.pos_for_vendor(vendor["vendor_id"]):
        if p["status"] != "open" or p["remaining_value"] <= 0:
            continue
        sims = []
        for it in f["line_items"]:
            sims.append(max((fuzz.token_set_ratio(it["description"].lower(), l["item"].lower()) for l in p["lines"]),
                            default=0))
        item_sim = sum(sims) / len(sims) if sims else 0
        fit = max(0.0, 1 - abs(net - p["remaining_value"]) / max(net, p["remaining_value"])) * 100
        conf = min(95, round(0.65 * item_sim + 0.35 * fit))  # an inference is never certain
        cands.append({"po_number": p["po_number"], "description": p["description"], "remaining": p["remaining_value"],
                      "item_similarity": round(item_sim), "amount_fit": round(fit), "confidence": conf})
    return sorted(cands, key=lambda c: -c["confidence"])


# ===================================================================== run orchestration
class Runner:
    def __init__(self, rid):
        self.rid = rid
        self.stages = [{"key": k, "title": t, "status": "pending", "summary": "", "blocks": [], "checks": [],
                        "ms": None} for k, t in STAGES]
        self.checks = []
        self.audit = [{"at": datetime.now().isoformat(timespec="seconds"), "actor": "system",
                       "event": "Run started"}]
        db.update_run(rid, stages=self.stages)

    def _st(self, key):
        return next(s for s in self.stages if s["key"] == key)

    def start(self, key):
        s = self._st(key)
        s["status"] = "running"
        db.update_run(self.rid, stages=self._clean())
        time.sleep(DELAY)  # visual pacing only, so a human can follow the live view; excluded from timings
        s["_t"] = time.time()

    def finish(self, key, summary, checks=(), blocks=(), status=None):
        s = self._st(key)
        for c in checks:
            c["stage"] = key
        s["checks"] = list(checks)
        s["blocks"] = list(blocks)
        s["summary"] = summary
        s["status"] = status or worst(list(checks))
        s["ms"] = int((time.time() - s.pop("_t", time.time())) * 1000)
        self.checks.extend(checks)
        db.update_run(self.rid, stages=self._clean())

    def skip(self, key, reason):
        self.start(key)
        self.finish(key, reason, status="skipped")

    def _clean(self):
        return [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.stages]


def _verify(f, key, floor=0.97):
    """A field that passes an independent check (arithmetic, master-data match) earns high confidence."""
    fc = f.get("field_confidence")
    if fc and fc.get(key):
        fc[key] = max(fc[key], floor)


def kv(items):
    return {"type": "kv", "items": [[k, v] for k, v in items]}


def table(columns, rows, title=None):
    return {"type": "table", "title": title, "columns": columns, "rows": rows}


def run_pipeline(rid: str):
    t0 = time.time()
    run = db.get_run(rid)
    R = Runner(rid)
    buyer = db.buyer()
    ctx = {}
    try:
        _run(R, run, buyer, ctx)
        # report real processing time (sum of stage work), excluding the visual pacing delay
        work = sum(s["ms"] or 0 for s in R.stages)
        db.update_run(rid, status="done", duration_ms=max(work, 1), audit=R.audit)
    except Exception as e:  # noqa: BLE001 - surface any failure in the UI rather than dying silently
        traceback.print_exc()
        for s in R.stages:
            if s["status"] in ("running", "pending"):
                s["status"] = "error" if s["status"] == "running" else "skipped"
                s["summary"] = s["summary"] or (f"Error: {e}" if s["status"] == "error" else "Not run")
        R.audit.append({"at": datetime.now().isoformat(timespec="seconds"), "actor": "system",
                        "event": f"Run failed: {e}"})
        db.update_run(rid, status="error", decision="ERROR", stages=R._clean(), audit=R.audit,
                      summary=f"Processing error: {e}", duration_ms=int((time.time() - t0) * 1000))


def _run(R, run, buyer, ctx):
    path = run["file_path"]

    # 1 ---------------------------------------------------------------- intake
    R.start("intake")
    size = os.path.getsize(path)
    R.finish("intake", f"{run['filename']} received ({size / 1024:.0f} KB)", [], [kv([
        ("File", run["filename"]), ("Source", run["source"]), ("Size", f"{size / 1024:.1f} KB"),
        ("Fingerprint (SHA-256)", run["file_hash"][:16] + "…"), ("Received", run["created_at"].replace("T", " "))])],
        status="pass")

    # 2 ---------------------------------------------------------------- read
    R.start("read")
    doc = read_document(path)
    if doc["kind"] == "digital":
        msg = f"Digital PDF — machine-readable text layer ({doc['chars']:,} characters, {doc['pages']} page)"
    else:
        from . import llm
        msg = ("Scanned image — no text layer. Routed to AI vision (OCR as cross-check)" if llm.provider()
               else "Scanned image — no text layer. Routed to Tesseract OCR")
    R.finish("read", msg, [], [kv([("Document type", "Digital PDF" if doc["kind"] == "digital" else "Scanned / image"),
                                    ("Pages", doc["pages"]), ("Text characters", doc["chars"])])],
             status="pass" if doc["kind"] == "digital" else "info")

    # 3 ---------------------------------------------------------------- extract
    R.start("extract")
    ov = run.get("overrides")
    if ov:   # a reviewer corrected the extracted data -> re-run every check on the corrected values
        f = normalise(ov["fields"])
        f["field_confidence"] = {k: (1.0 if ((f.get("vendor_gstin") or f.get("vendor_name")) if k == "vendor" else f.get(k))
                                     else (0.0 if req else None)) for k, _, req in CONF_FIELDS}
        ex = {"fields": f, "method": f"Reviewer-corrected data (from {run['parent_run']})", "reader": "reviewer",
              "cross_check": [], "warnings": []}
    else:
        ex = extract(path, doc, buyer["gstin"])
        f = ex["fields"]
    db.update_run(R.rid, extraction_method=ex["method"])
    checks = []
    if not f or not (f.get("total") or f.get("line_items")):
        if doc["kind"] == "digital":
            checks.append(check("NOT_INVOICE", "hold", "This document does not look like an invoice",
                                "Text was readable but no amounts, line items or invoice fields were found.",
                                "AP reviewer", "Check whether the wrong attachment was sent; ask the vendor for the invoice."))
        elif any("AI extraction failed" in w for w in ex["warnings"]):
            checks.append(check("AI_UNAVAILABLE", "hold", "AI reader was unavailable — scan not read",
                                "The AI service was busy or unreachable even after retries and a backup model, and no "
                                "OCR is available on this machine. Nothing was guessed. Details: " + "; ".join(ex["warnings"]),
                                "AP reviewer", "Click Re-run in a minute, or open the PDF and correct the fields by hand."))
        else:
            checks.append(check("UNREADABLE", "hold", "Could not read the invoice",
                                "Neither the AI reader nor OCR could recover amounts from this document. " +
                                "; ".join(ex["warnings"]), "Vendor", "Request a clearer copy or the original digital PDF."))
        R.finish("extract", "Unable to extract invoice data", checks, [kv([("Method", ex["method"])])])
        for k in ["quality", "vendor", "duplicate", "po_match", "reconcile"]:
            R.skip(k, "Skipped — no readable data")
        return _decide(R, run, ctx, f or {}, None, None, None, None, buyer)
    ai_used = ex["method"].startswith("AI vision")
    for w in ex["warnings"]:
        if w.startswith("OCR unavailable") and ai_used:
            # AI read the scan fine; only the optional independent OCR cross-check is missing on this machine
            checks.append(check("NO_OCR_CROSSCHECK", "info", "OCR cross-check skipped",
                                "Tesseract isn't installed on this machine, so the AI read could not be compared with "
                                "an independent OCR read. Confidence and arithmetic checks still apply."))
        elif w.startswith("AI extraction failed"):
            why = ("overloaded (HTTP 503)" if "503" in w else "rate-limited (HTTP 429)" if "429" in w
                   else "too slow to answer" if ("slow" in w or "Timeout" in w) else "unavailable")
            if doc["kind"] == "digital":
                # digital PDF: the text-layer parser is exact, and the checks below verify every value
                checks.append(check("AI_FALLBACK", "info", "AI reader busy — read from the PDF's text layer instead",
                                    f"The AI service was {why}, so the built-in parser read this digital PDF. "
                                    "Arithmetic and master-data checks below verify the values."))
            else:
                checks.append(check("AI_FALLBACK", "warn", "AI reader busy — read with OCR instead",
                                    f"The AI service was {why}, so Tesseract OCR read this scan. "
                                    "Verify the key fields against the document."))
        else:
            checks.append(check("EXTRACT_WARN", "warn", "Extraction fallback used", w))
    fc = f.get("field_confidence") or {}
    labels = {k: lab for k, lab, _ in CONF_FIELDS}
    for k, v in fc.items():
        if v and v < RULES["low_confidence_threshold"]:   # missing fields (0) are handled by data-quality checks
            sev = "hold" if k in ("total", "invoice_number", "line_items", "vendor") else "warn"
            checks.append(check("LOW_CONFIDENCE", sev, f"Low read confidence on {labels[k].lower()}",
                                f"Confidence {v:.0%} — verify against the original document.",
                                "AP reviewer", "Check this field on the PDF, and correct it if needed before approving."))
    if ov:
        checks.append(check("CORRECTED", "info", f"{len(ov['changes'])} field(s) corrected by {ov['by']}",
                            "; ".join(f"{c['label']}: '{c['old'] or '—'}' → '{c['new'] or '—'}'" for c in ov["changes"])
                            + (f". Note: {ov['note']}" if ov.get("note") else "")))
    dis = [c for c in ex["cross_check"] if not c["agree"] and c["rules"] is not None]
    for c in dis:
        checks.append(check("CROSS_CHECK", "warn", f"Readers disagree on {c['field'].replace('_', ' ')}",
                            f"AI read '{c['ai']}', rule-based read '{c['rules']}'. Using AI value; flagged for audit."))
    if ex["cross_check"] and not dis:
        checks.append(check("CROSS_CHECK", "pass", "Independent cross-check agrees",
                            "AI extraction and rule-based parser agree on invoice number, date, PO, GSTIN and total."))
    shown = {"vendor": f"{f.get('vendor_name') or '—'} · {f.get('vendor_gstin') or 'no GSTIN'}",
             "invoice_number": f.get("invoice_number"), "invoice_date": f.get("invoice_date"),
             "po_reference": f.get("po_reference"), "total": inr(f.get("total")) if f.get("total") is not None else None,
             "line_items": f"{len(f['line_items'])} line(s)" if f["line_items"] else None,
             "payment_terms": f.get("payment_terms"), "bank_account": f.get("bank_account")}
    blocks = [{"type": "fields", "method": ex["method"], "reader": ex.get("reader"),
               "rows": [{"label": lab, "value": shown[k], "confidence": fc.get(k), "required": req}
                        for k, lab, req in CONF_FIELDS]},
              kv([("Subtotal", inr(f.get("subtotal"))), ("Tax", inr(f.get("tax_total"))),
                  ("Prices include tax", "Yes" if f["amounts_include_tax"] else "No"), ("IFSC", f.get("bank_ifsc") or "—")]),
              table(["Description", "Qty", "Unit price", "Amount"],
                    [[i["description"], i["quantity"], inr(i["unit_price"]), inr(i["amount"])] for i in f["line_items"]],
                    "Line items"),
              ]
    if f["tax_lines"]:
        blocks.append(table(["Tax", "Rate", "Amount"],
                            [[t["type"], f"{t['rate']:g}%" if t["rate"] else "—", inr(t["amount"])] for t in f["tax_lines"]],
                            "Tax lines"))
    if ex["cross_check"]:
        blocks.append(table(["Field", "AI reader", "Rule parser", "Agree"],
                            [[c["field"], c["ai"], c["rules"], "✓" if c["agree"] else "✗"] for c in ex["cross_check"]],
                            "Independent cross-check"))
    if f["notes"]:
        blocks.append({"type": "text", "title": "Notes printed on invoice", "text": "\n".join(f["notes"])})
    n = len([v for v in [f.get("invoice_number"), f.get("invoice_date"), f.get("total"), f.get("vendor_gstin"),
                         f.get("po_reference")] if v])
    R.finish("extract", f"{n}/5 key fields and {len(f['line_items'])} line item(s) extracted via "
             f"{ex['method'].split(' (')[0].lower()}", checks, blocks, status=worst(checks) if checks else "pass")
    ctx["fields"] = f
    db.update_run(R.rid, vendor_name=f.get("vendor_name"), invoice_number=f.get("invoice_number"), total=f.get("total"))

    # 4 ---------------------------------------------------------------- data quality
    R.start("quality")
    checks = []
    tol = RULES["rounding_tolerance"]
    missing = [lab for k, lab in [("invoice_number", "invoice number"), ("invoice_date", "invoice date"),
                                  ("total", "total amount")] if not f.get(k)]
    if not (f.get("vendor_name") or f.get("vendor_gstin")):
        missing.append("vendor identity")
    if missing:
        checks.append(check("MISSING_FIELDS", "hold", f"Missing mandatory field(s): {', '.join(missing)}",
                            "A valid GST tax invoice must carry an invoice number and date; without them we cannot "
                            "claim input tax credit or run duplicate control.",
                            "Vendor", f"Ask vendor to reissue the invoice with: {', '.join(missing)}."))
    else:
        checks.append(check("MANDATORY", "pass", "All mandatory fields present",
                            "Invoice number, date, vendor and total found."))
    tax = f.get("tax_total") or 0
    lines_sum = sum(i["amount"] or 0 for i in f["line_items"])
    total = f.get("total")
    if f["amounts_include_tax"]:
        gross = total if total is not None else lines_sum
        net = round(gross - tax, 2)
        checks.append(check("TAX_INCLUSIVE", "info", "Tax-inclusive pricing normalised",
                            f"Invoice states prices include GST. Pre-tax value derived as {inr(gross)} − {inr(tax)} tax "
                            f"= {inr(net)}; unit prices are compared net of tax."))
        if f["line_items"] and total is not None and abs(lines_sum - total) > tol:
            checks.append(check("LINES_SUM", "hold", "Line items do not add up to the total",
                                f"Lines sum to {inr(lines_sum)} but total is {inr(total)}.", "AP reviewer",
                                "Verify the arithmetic on the original; request a corrected invoice if wrong."))
        elif f["line_items"] and total is not None:
            _verify(f, "line_items")
            _verify(f, "total")
    else:
        sub = f.get("subtotal")
        if sub is None:
            sub = lines_sum if f["line_items"] else ((total or 0) - tax)
            f["subtotal"] = sub
        net = sub
        if f["line_items"] and abs(lines_sum - sub) > tol:
            checks.append(check("LINES_SUM", "hold", "Line items do not add up to the subtotal",
                                f"Lines sum to {inr(lines_sum)} but subtotal is {inr(sub)} (difference {inr(sub - lines_sum)}).",
                                "AP reviewer", "Verify arithmetic; request a corrected invoice if wrong."))
        elif f["line_items"]:
            checks.append(check("LINES_SUM", "pass", "Line items add up to subtotal", f"{inr(lines_sum)}"))
            _verify(f, "line_items")
        if total is not None and abs(sub + tax - total) > tol:
            checks.append(check("TOTAL_MATH", "hold", "Subtotal + tax does not equal total",
                                f"{inr(sub)} + {inr(tax)} = {inr(sub + tax)}, but invoice total is {inr(total)}.",
                                "AP reviewer", "Verify arithmetic; request a corrected invoice if wrong."))
        elif total is not None:
            checks.append(check("TOTAL_MATH", "pass", "Subtotal + tax = total", f"{inr(sub)} + {inr(tax)} = {inr(total)}"))
            _verify(f, "total")
    if not f["tax_lines"] and not tax:
        checks.append(check("NO_TAX", "warn", "No GST shown on invoice", "Will be validated against PO tax rate."))
    d = f.get("invoice_date")
    if d:
        try:
            dd = date.fromisoformat(d)
            age = (today() - dd).days
            if age < 0:
                checks.append(check("FUTURE_DATE", "hold", "Invoice is dated in the future", f"Dated {d}.",
                                    "Vendor", "Ask vendor to confirm the invoice date."))
            elif age > RULES["max_invoice_age_days"]:
                checks.append(check("STALE", "hold", f"Invoice is {age} days old", "Older than policy limit.",
                                    "AP reviewer", "Confirm it was not already paid through another channel."))
            else:
                checks.append(check("DATE_OK", "pass", "Invoice date valid", f"{d} ({age} days ago)"))
        except ValueError:
            checks.append(check("BAD_DATE", "hold", "Invoice date unreadable", str(d), "AP reviewer", "Verify date."))
    bg = f.get("buyer_gstin")
    if bg and bg != buyer["gstin"]:
        checks.append(check("BILL_TO", "hold", "Invoice not billed to our GSTIN",
                            f"Billed to {bg}; ours is {buyer['gstin']}. Input tax credit would be lost.",
                            "Vendor", "Ask vendor to reissue the invoice to the correct GSTIN."))
    elif bg:
        checks.append(check("BILL_TO", "pass", "Billed to our GSTIN", bg))
    R.finish("quality", f"{sum(c['severity'] == 'pass' for c in checks)} passed, "
             f"{sum(SEV[c['severity']] >= 3 for c in checks)} blocking issue(s)", checks,
             [kv([("Pre-tax value used for matching", inr(net)), ("Tax", inr(tax)), ("Invoice total", inr(total))])])
    ctx["net"] = net

    # 5 ---------------------------------------------------------------- vendor
    R.start("vendor")
    checks = []
    vendors = db.vendors()
    vg = f.get("vendor_gstin")
    vendor = next((v for v in vendors if vg and v["gstin"] == vg), None)
    how = "GSTIN exact match" if vendor else None
    scored = sorted(((name_score(f.get("vendor_name"), v["name"]), v) for v in vendors), key=lambda t: -t[0])
    if not vendor and scored and scored[0][0] >= RULES["vendor_name_match_threshold"]:
        vendor = scored[0][1]
        how = f"name match ({scored[0][0]:.0f}%)"
        if vg:
            checks.append(check("GSTIN_MISMATCH", "hold", "GSTIN on invoice differs from vendor master",
                                f"Name matches {vendor['name']} but invoice GSTIN {vg} ≠ master {vendor['gstin']}.",
                                "AP lead", "Verify with vendor; could be a different branch registration or impersonation."))
    if vg:
        ok, why = gstin.validate(vg)
        checks.append(check("GSTIN_VALID", "pass" if ok else "hold", f"GSTIN {'valid' if ok else 'invalid'}",
                            f"{vg}: {why}", None if ok else "AP reviewer", None if ok else "Verify GSTIN on the GST portal."))
    if not vendor:
        checks.append(check("UNKNOWN_VENDOR", "reject", "Vendor is not in the approved vendor master",
                            f"No GSTIN or name match for '{f.get('vendor_name')}' "
                            f"(closest: {scored[0][1]['name']} at {scored[0][0]:.0f}%).",
                            "Procurement", "Vendor must complete onboarding before any invoice can be paid."))
        R.finish("vendor", "Vendor not found in master", checks)
        for k in ["duplicate", "po_match", "reconcile"]:
            R.skip(k, "Skipped — vendor unknown")
        return _decide(R, run, ctx, f, None, None, None, None, buyer)
    checks.append(check("VENDOR_FOUND", "pass", f"Matched {vendor['name']} ({vendor['vendor_id']})", how))
    if how.startswith("GSTIN"):
        _verify(f, "vendor", 0.99)
    if vendor["status"] != "active":
        checks.append(check("VENDOR_BLOCKED", "reject", f"Vendor is {vendor['status']}", vendor["status_note"],
                            "Procurement", "Do not pay. Escalate to procurement / compliance."))
    ba = f.get("bank_account")
    if ba and ba != vendor["bank_account"]:
        note = (" Invoice also says: “" + " ".join(f["notes"]) + "”") if f["notes"] else ""
        checks.append(check("BANK_MISMATCH", "hold", "Bank account differs from vendor master — payment fraud risk",
                            f"Invoice asks for payment to {f.get('bank_name') or 'bank'} a/c ••••{ba[-4:]} "
                            f"(IFSC {f.get('bank_ifsc') or '?'}); master has {vendor['bank_name']} ••••"
                            f"{vendor['bank_account'][-4:]} (IFSC {vendor['ifsc']}).{note}",
                            "AP lead (fraud check)",
                            "Call the vendor on the phone number in the vendor master (never the one on the invoice) "
                            "to verify. Update master only with a signed bank letter. Do not pay until verified."))
    elif ba:
        checks.append(check("BANK_MATCH", "pass", "Bank details match vendor master", f"a/c ••••{ba[-4:]}"))
    else:
        checks.append(check("BANK_ABSENT", "info", "No bank details read from invoice",
                            "Payment will go to the account on the vendor master."))
    agreed = vendor.get("payment_terms_days")
    inv_terms = f.get("payment_terms_days")
    if agreed is not None and inv_terms is not None and inv_terms < agreed:
        checks.append(check("TERMS_SHORTER", "warn", f"Invoice asks for {f.get('payment_terms')}; agreed terms are Net {agreed}",
                            f"Payment will be scheduled per the contracted Net {agreed} days, not the invoice's "
                            f"{inv_terms}-day terms (protects working capital)."))
    elif agreed is not None:
        checks.append(check("TERMS_OK", "pass", f"Payment terms: Net {agreed} (vendor master)",
                            f"Invoice states: {f.get('payment_terms') or 'not printed'}."))
    R.finish("vendor", f"{vendor['name']} · {vendor['status']}", checks,
             [kv([("Vendor ID", vendor["vendor_id"]), ("Master GSTIN", vendor["gstin"]),
                  ("State", vendor["state"]), ("Master bank", f"{vendor['bank_name']} ••••{vendor['bank_account'][-4:]}"),
                  ("Agreed payment terms", f"Net {agreed} days" if agreed is not None else "—"),
                  ("Matched by", how)])])
    ctx["vendor"] = vendor
    db.update_run(R.rid, vendor_id=vendor["vendor_id"], vendor_name=vendor["name"])

    # 6 ---------------------------------------------------------------- duplicates
    R.start("duplicate")
    checks = []
    hist = db.q("SELECT * FROM invoices WHERE vendor_id=?", (vendor["vendor_id"],))
    nn = db.norm_invoice_no(f.get("invoice_number"))
    exact = [h for h in hist if nn and h["invoice_number_norm"] == nn]
    same_file = db.q("SELECT * FROM invoices WHERE file_hash=?", (run["file_hash"],))
    if exact:
        h = exact[0]
        same_fmt = h["invoice_number"] == f.get("invoice_number")
        checks.append(check("DUPLICATE", "reject",
                            "Duplicate invoice — already processed" if same_fmt else
                            "Duplicate invoice — same number in a different format",
                            f"'{f.get('invoice_number')}' matches '{h['invoice_number']}' "
                            f"(normalised '{nn}') from {h['invoice_date']}, {inr(h['total'])}, posted to {h['po_number']}"
                            f" ({h['source']}).", "Vendor", "Inform vendor the invoice was already received; no payment."))
    elif same_file:
        checks.append(check("DUPLICATE_FILE", "reject", "This exact file was already posted",
                            f"Same SHA-256 as invoice {same_file[0]['invoice_number']} (run {same_file[0]['run_id']}).",
                            "AP reviewer", "No action — duplicate submission."))
    else:
        checks.append(check("NO_DUP_NUMBER", "pass", "Invoice number not seen before",
                            f"Checked {len(hist)} prior invoice(s) from this vendor (format-insensitive)."))
    tot = f.get("total")
    if tot and not exact and f.get("invoice_date"):
        win = RULES["near_duplicate_window_days"]
        near = [h for h in hist if abs(h["total"] - tot) <= 1 and
                abs((date.fromisoformat(h["invoice_date"]) - date.fromisoformat(f["invoice_date"])).days) <= win]
        if near:
            h = near[0]
            checks.append(check("NEAR_DUPLICATE", "hold", "Possible duplicate under a different number",
                                f"Same amount {inr(tot)} as {h['invoice_number']} dated {h['invoice_date']}.",
                                "AP reviewer", "Compare both invoices before approving."))
    R.finish("duplicate", "Duplicate found" if exact or same_file else "No duplicates found", checks)

    # 7 ---------------------------------------------------------------- PO match
    R.start("po_match")
    checks, blocks = [], []
    po, inferred, cands = None, False, []
    ref = norm_po(f.get("po_reference"))
    if ref:
        po = db.po(ref)
        if not po:
            checks.append(check("PO_NOT_FOUND", "hold", f"PO {f.get('po_reference')} not found",
                                f"Normalised to {ref}; no such PO in the system.", "Procurement",
                                "Confirm the correct PO number with the requester."))
        elif po["vendor_id"] != vendor["vendor_id"]:
            checks.append(check("PO_OTHER_VENDOR", "hold", f"{ref} belongs to a different vendor",
                                f"PO is issued to {po['vendor_id']}, invoice is from {vendor['vendor_id']}.",
                                "Procurement", "Confirm the correct PO."))
            po = None
        else:
            msg = f"'{f.get('po_reference')}' → {ref}" + ("" if f.get("po_reference") == ref else " (format normalised)")
            checks.append(check("PO_FOUND", "pass", f"Matched {ref} — {po['description']}", msg))
            if po["status"] != "open":
                checks.append(check("PO_CLOSED", "hold", f"{ref} is {po['status']}", "", "Procurement",
                                    "Re-open the PO or confirm this invoice is not payable."))
    else:
        cands = infer_po(f, vendor, ctx["net"])
        if cands and cands[0]["confidence"] >= RULES["po_inference_min_confidence"]:
            best = cands[0]
            po = db.po(best["po_number"])
            inferred = True
            checks.append(check("PO_INFERRED", "hold", f"No PO on invoice — best match {best['po_number']} "
                                f"({best['confidence']}% confidence)",
                                f"Ranked {len(cands)} open PO(s) for {vendor['name']} by item similarity "
                                f"({best['item_similarity']}%) and amount fit ({best['amount_fit']}%). "
                                "Policy: an inferred PO is never auto-approved.",
                                "AP reviewer", f"Confirm {best['po_number']} with one click, or pick another PO."))
        else:
            checks.append(check("PO_MISSING", "hold", "No PO reference and no confident match",
                                f"{len(cands)} open PO(s) considered.", "Vendor",
                                "Ask the vendor to quote the PO number, or the requester to raise a PO."))
        if cands:
            blocks.append(table(["Candidate PO", "Description", "Open value", "Item match", "Amount fit", "Confidence"],
                                [[c["po_number"], c["description"], inr(c["remaining"]), f"{c['item_similarity']}%",
                                  f"{c['amount_fit']}%", f"{c['confidence']}%"] for c in cands], "PO candidates"))
    if po:
        blocks.insert(0, kv([("PO", po["po_number"]), ("PO date", po["po_date"]), ("PO value", inr(po["value"])),
                             ("Already invoiced", inr(po["invoiced_value"])), ("Open value", inr(po["remaining_value"])),
                             ("GST rate", f"{po['gst_rate']:g}%")]))
    R.finish("po_match", (f"{'Suggested' if inferred else 'Matched'} {po['po_number']}" if po else "No PO matched"),
             checks, blocks)
    ctx.update(po=po, inferred=inferred, candidates=cands)
    if po:
        db.update_run(R.rid, po_number=po["po_number"] + (" (suggested)" if inferred else ""))

    # 8 ---------------------------------------------------------------- reconcile
    if not po:
        R.skip("reconcile", "Skipped — no PO to reconcile against")
        return _decide(R, run, ctx, f, vendor, None, None, None, buyer)
    R.start("reconcile")
    checks, rows, alloc, summ = reconcile(f, po, vendor, ctx["net"], buyer["gstin"])
    if inferred:
        for c in checks:
            c["detail"] = "[against suggested PO] " + c["detail"]
    blocks = [table(["Invoice item", "PO item", "Match", "Qty billed", "Open on PO", "Received, unbilled",
                     "Unit price", "PO price", "Variance", "Status"],
                    [[r["invoice_item"], r["po_item"], f"{r['match']}%", r["qty"],
                      "—" if r["open_qty"] is None else r["open_qty"], "—" if r["received"] is None else r["received"],
                      inr(r["price"]),
                      inr(r["po_price"]), "—" if r["variance_pct"] is None else f"{r['variance_pct']:+.2f}%",
                      r["status"]] for r in rows], "Three-way view: invoice vs PO vs goods receipt" if po.get("receipt_required") else "Invoice vs PO (service)"),
              kv([("PO value", inr(summ["po_value"])), ("Already invoiced", inr(summ["already_invoiced"])),
                  ("Open before this invoice", inr(summ["remaining_before"])),
                  ("This invoice (pre-tax)", inr(summ["this_invoice"])),
                  ("Open after this invoice", inr(summ["remaining_after"]))])]
    R.finish("reconcile", f"{len(rows)} line(s) reconciled · {inr(ctx['net'])} vs {inr(summ['remaining_before'])} open",
             checks, blocks)
    _decide(R, run, ctx, f, vendor, po, rows, alloc, buyer, summ)


# ===================================================================== decision + action
OWNER_ORDER = ["Vendor", "AP lead (fraud check)", "AP lead", "Procurement", "Tax team", "AP reviewer"]


def _decide(R, run, ctx, f, vendor, po, rows, alloc, buyer, summ=None):
    R.start("decision")
    checks = R.checks
    sev = worst(checks)
    blocking = [c for c in checks if SEV[c["severity"]] >= 3]
    notes = [c for c in checks if c["severity"] == "warn"]
    if sev == "reject":
        decision = "REJECTED"
        rej = [c for c in checks if c["severity"] == "reject"]
        summary = "Rejected — " + "; ".join(c["title"] for c in rej) + "."
    elif sev == "hold":
        decision = "ON_HOLD"
        summary = (f"Flagged for review — {len(blocking)} issue(s) need a human: "
                   + "; ".join(c["title"] for c in blocking) + ".")
    else:
        decision = "APPROVED"
        summary = (f"Approved — matches {po['po_number']}, vendor verified, no duplicates"
                   + (f"; {len(notes)} note(s) within tolerance." if notes else "; clean match."))
    R.finish("decision", summary, [], [{
        "type": "decision", "decision": decision,
        "reasons": [{"severity": c["severity"], "title": c["title"], "detail": c["detail"]} for c in
                    sorted(checks, key=lambda c: -SEV[c["severity"]]) if SEV[c["severity"]] >= 2]}],
        status={"APPROVED": "pass", "ON_HOLD": "hold", "REJECTED": "reject"}[decision])

    # 10 action
    R.start("action")
    blocks, email, routing = [], None, []
    due = None
    if decision == "APPROVED" and not db.get_run(R.rid):
        return   # run was deleted by a reset while it was processing: never post it to the fresh ledger
    if decision == "APPROVED":
        db.post_invoice(vendor["vendor_id"], f.get("invoice_number"), f.get("invoice_date"), po["po_number"],
                        ctx["net"], f.get("tax_total") or 0, f.get("total"), R.rid, run["file_hash"], alloc)
        terms = vendor.get("payment_terms_days")
        terms = RULES["payment_terms_days"] if terms is None else terms
        if f.get("invoice_date"):
            due = (date.fromisoformat(f["invoice_date"]) + timedelta(days=terms)).isoformat()
        after = db.po(po["po_number"])
        act = f"Posted to ledger against {po['po_number']} and queued for payment"
        blocks.append(kv([("Posted to", po["po_number"]), ("Payable", inr(f.get("total"))),
                          ("Pay to", f"{vendor['bank_name']} ••••{vendor['bank_account'][-4:]} (vendor master)"),
                          ("Payment due", f"{due} (Net {terms:.0f}, agreed terms)" if due else "—"),
                          ("PO open value now", inr(after["remaining_value"]))]))
        R.audit.append({"at": datetime.now().isoformat(timespec="seconds"), "actor": "system",
                        "event": f"Auto-approved and posted to {po['po_number']}"})
    else:
        by_owner = {}
        for c in blocking:
            by_owner.setdefault(c.get("owner") or "AP reviewer", []).append(c)
        for o in sorted(by_owner, key=lambda o: OWNER_ORDER.index(o) if o in OWNER_ORDER else 99):
            routing.append({"owner": o, "items": [{"title": c["title"], "action": c["action"]} for c in by_owner[o]]})
        email = draft_email(decision, f, vendor, po, blocking, buyer)
        act = ("Routed to " + ", ".join(r["owner"] for r in routing)) if routing else "Routed to AP reviewer"
        blocks.append({"type": "routing", "routing": routing})
        if email:
            blocks.append({"type": "email", "email": email})
        R.audit.append({"at": datetime.now().isoformat(timespec="seconds"), "actor": "system",
                        "event": f"{decision.replace('_', ' ').title()} — {act}"})
    R.finish("action", act, [], blocks, status="pass" if decision == "APPROVED" else
             ("hold" if decision == "ON_HOLD" else "reject"))

    trace = [{"step": i + 1, "title": st["title"], "status": st["status"], "summary": st["summary"]}
             for i, st in enumerate(R.stages[:8])]
    fc = f.get("field_confidence") or {}
    req = [fc.get(k) for k, _, r in CONF_FIELDS if r and fc.get(k) is not None]
    result = {"decision": decision, "summary": summary, "checks": checks, "fields": f, "trace": trace,
              "confidence": round(min(req), 2) if req else None,
              "payment_terms": (f"Net {vendor['payment_terms_days']} days" if vendor and vendor.get("payment_terms_days")
                                is not None else None),
              "vendor": vendor, "po": po["po_number"] if po else None, "po_inferred": ctx.get("inferred", False),
              "candidates": ctx.get("candidates", []), "reconciliation": rows, "allocation": alloc,
              "recon_summary": summ, "net": ctx.get("net"), "routing": routing, "email": email, "due_date": due}
    db.update_run(R.rid, decision=decision, summary=summary, result=result, audit=R.audit)


def draft_email(decision, f, vendor, po, blocking, buyer):
    vendor_items = [c for c in blocking if c.get("owner") == "Vendor"]
    to_vendor = bool(vendor_items) or (decision == "REJECTED" and any(c["code"].startswith("DUPLICATE") for c in blocking))
    if not to_vendor or not vendor:
        return None
    inv = f.get("invoice_number") or "(no invoice number)"
    amt = inr(f.get("total"))
    if decision == "REJECTED":
        subject = f"Invoice {inv} — already processed (duplicate)"
        body = (f"Dear {vendor['name']} team,\n\nThank you for your invoice {inv} for {amt}. Our records show this "
                f"invoice has already been received and processed, so we will not be processing it again.\n\n"
                + "\n".join(f"• {c['detail']}" for c in blocking) +
                "\n\nIf you believe this is a new, separate invoice, please reply with a corrected invoice number "
                "and supporting delivery details.\n\nRegards,\nAccounts Payable\n" + buyer["name"])
    else:
        subject = f"Action needed: invoice {inv} ({amt}) is on hold"
        body = (f"Dear {vendor['name']} team,\n\nWe have received your invoice {inv} for {amt}"
                + (f" against {po['po_number']}" if po else "") + ". We are unable to process it yet because:\n\n"
                + "\n".join(f"• {c['title']} — {c['action']}" for c in vendor_items) +
                "\n\nPlease send a corrected invoice to this address. Payment terms start from receipt of a valid "
                "invoice.\n\nRegards,\nAccounts Payable\n" + buyer["name"])
    return {"to": vendor["email"], "subject": subject, "body": body}


# ===================================================================== human review
def review(rid, action, note, po_number=None, reviewer="AP reviewer"):
    run = db.get_run(rid)
    if not run or run["decision"] != "ON_HOLD":
        raise ValueError("Only runs flagged for review can be reviewed")
    res = run["result"]
    audit = run["audit"] or []
    now = datetime.now().isoformat(timespec="seconds")
    if action == "reject":
        db.update_run(rid, decision="REJECTED", summary=f"Rejected by {reviewer}: {note or 'no note'}",
                      review={"action": "reject", "note": note, "by": reviewer, "at": now},
                      audit=audit + [{"at": now, "actor": reviewer, "event": f"Rejected after review — {note}"}])
        return db.get_run(rid)
    f = res["fields"]
    vendor = res["vendor"]
    if not vendor:
        raise ValueError("Cannot approve: vendor is unknown")
    pn = po_number or res["po"]
    if not pn:
        raise ValueError("Select a PO to approve against")
    po = db.po(pn)
    if not po or po["vendor_id"] != vendor["vendor_id"]:
        raise ValueError(f"{pn} is not a valid PO for this vendor")
    _, rows, alloc, _ = reconcile(f, po, vendor, res["net"], db.buyer()["gstin"])
    db.post_invoice(vendor["vendor_id"], f.get("invoice_number"), f.get("invoice_date"), pn, res["net"],
                    f.get("tax_total") or 0, f.get("total"), rid, run["file_hash"], alloc)
    res["po"] = pn
    db.update_run(rid, decision="APPROVED", po_number=pn, result=res,
                  summary=f"Approved by {reviewer} against {pn}" + (f": {note}" if note else ""),
                  review={"action": "approve", "note": note, "by": reviewer, "at": now, "po": pn},
                  audit=audit + [{"at": now, "actor": reviewer,
                                  "event": f"Approved after review against {pn} — {note or 'no note'}"},
                                 {"at": now, "actor": "system", "event": f"Posted to ledger against {pn}"}])
    return db.get_run(rid)


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


# ===================================================================== processing queue
# One worker processes runs strictly in arrival order. This matters for correctness, not just tidiness:
# two invoices against the same PO must not be checked against the same open balance at the same time.
_q: "queue.Queue[str]" = queue.Queue()
_worker_started = False
_wlock = threading.Lock()


def enqueue(rid: str):
    global _worker_started
    with _wlock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True).start()
            _worker_started = True
    _q.put(rid)


def _worker():
    while True:
        rid = _q.get()
        try:
            if db.claim(rid):          # skip duplicates in the queue and runs removed by a reset
                run_pipeline(rid)
        finally:
            _q.task_done()


def clear_queue():
    """Drop every waiting job (used by reset)."""
    with _q.mutex:
        n = len(_q.queue)
        _q.queue.clear()
        _q.unfinished_tasks = max(0, _q.unfinished_tasks - n)
        _q.all_tasks_done.notify_all()


def requeue_unfinished():
    """After a server restart, put runs that were waiting or mid-way back in the queue."""
    for r in db.q("SELECT id FROM runs WHERE status IN ('queued','running') ORDER BY seq"):
        db.update_run(r["id"], status="queued")
        enqueue(r["id"])


def queue_position(rid: str) -> int | None:
    with _q.mutex:
        items = list(_q.queue)
    return items.index(rid) + 1 if rid in items else None


def rerun(rid, reviewer="AP reviewer"):
    """Process the same file again from scratch (e.g. the AI service was busy). Original is kept as superseded."""
    run = db.get_run(rid)
    if not run or run["decision"] == "APPROVED" or run.get("review") or run["status"] != "done":
        raise ValueError("Only finished, unreviewed runs that were not approved can be re-run")
    now = datetime.now().isoformat(timespec="seconds")
    nrid = db.new_run(run["filename"], f"re-run of {rid}", run["file_path"], run["file_hash"])
    db.update_run(nrid, parent_run=rid)
    db.update_run(rid, decision="SUPERSEDED", summary=f"Re-run as {nrid} by {reviewer}",
                  review={"action": "rerun", "by": reviewer, "at": now, "new_run": nrid, "changes": []},
                  audit=(run["audit"] or []) + [{"at": now, "actor": reviewer, "event": f"Re-run as {nrid}"}])
    enqueue(nrid)
    return nrid


# ===================================================================== reviewer corrections
EDITABLE = [("vendor_name", "Vendor name"), ("vendor_gstin", "Vendor GSTIN"), ("invoice_number", "Invoice number"),
            ("invoice_date", "Invoice date"), ("po_reference", "PO reference"), ("subtotal", "Subtotal"),
            ("tax_total", "Tax"), ("total", "Total"), ("payment_terms", "Payment terms"),
            ("bank_account", "Bank account"), ("bank_ifsc", "IFSC")]


def correct(rid, fields: dict, line_items: list | None, note, reviewer="AP reviewer"):
    """Reviewer fixes extracted data -> a new run re-checks everything on the corrected values.
    The original run is kept (superseded) so the audit trail shows exactly what changed and who changed it."""
    run = db.get_run(rid)
    if not run or run["decision"] not in ("ON_HOLD", "REJECTED") or run.get("review"):
        raise ValueError("Only held or rejected runs that have not been reviewed can be corrected")
    old = dict((run["result"] or {}).get("fields") or {})
    new = dict(old)
    changes = []
    for k, label in EDITABLE:
        if k not in fields:
            continue
        nv = fields[k]
        nv = None if nv in ("", None) else nv
        ov = old.get(k)
        if str(ov if ov is not None else "") != str(nv if nv is not None else ""):
            try:
                same = nv is not None and ov is not None and float(str(nv).replace(",", "")) == float(ov)
            except (TypeError, ValueError):
                same = False
            if not same:
                changes.append({"field": k, "label": label, "old": ov, "new": nv})
                new[k] = nv
    if line_items is not None:
        clean = [{"description": li.get("description", ""), "quantity": li.get("quantity"),
                  "unit_price": li.get("unit_price"), "amount": li.get("amount")} for li in line_items
                 if str(li.get("description", "")).strip()]
        fmt = lambda items: "; ".join(f"{i['description']} × {i['quantity']} @ {i['unit_price']}" for i in items)  # noqa: E731
        if fmt(clean) != fmt([{k: i.get(k) for k in ("description", "quantity", "unit_price", "amount")}
                              for i in old.get("line_items") or []]):
            changes.append({"field": "line_items", "label": "Line items", "old": fmt(old.get("line_items") or []),
                            "new": fmt(clean)})
            new["line_items"] = clean
    if not changes:
        raise ValueError("No changes made")
    if any(c["field"] in ("subtotal", "tax_total") for c in changes):
        new["tax_lines"] = [] if any(c["field"] == "tax_total" for c in changes) else new.get("tax_lines")
        if any(c["field"] == "tax_total" for c in changes):
            # keep GST type from original read so the GST-type check still works
            types = [t["type"] for t in old.get("tax_lines") or []] or ["IGST"]
            tax = float(str(new.get("tax_total") or 0).replace(",", ""))
            new["tax_lines"] = [{"type": t, "rate": None, "amount": round(tax / len(types), 2)} for t in types]
    new.pop("field_confidence", None)
    new["confidence"] = {}
    now = datetime.now().isoformat(timespec="seconds")
    nrid = db.new_run(run["filename"], f"correction of {rid}", run["file_path"], run["file_hash"])
    db.update_run(nrid, parent_run=rid,
                  overrides={"fields": new, "changes": changes, "note": note, "by": reviewer, "at": now})
    db.update_run(rid, decision="SUPERSEDED",
                  summary=f"Corrected by {reviewer} and re-run as {nrid} ({len(changes)} field(s) changed)",
                  review={"action": "corrected", "note": note, "by": reviewer, "at": now, "new_run": nrid,
                          "changes": changes},
                  audit=(run["audit"] or []) + [{"at": now, "actor": reviewer,
                                                 "event": f"Corrected {', '.join(c['label'] for c in changes)} "
                                                          f"→ re-run as {nrid}" + (f" — {note}" if note else "")}])
    enqueue(nrid)
    return nrid
