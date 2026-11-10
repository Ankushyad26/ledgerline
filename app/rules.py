"""Business rules & thresholds.

Thresholds and duplicate rules live in the reference dataset (data/seed/policy.json) so finance can change them
without touching code. An environment variable of the same name in UPPER_CASE overrides a value (handy for demos).
"""
import json
import os

_POLICY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "seed", "policy.json")
try:
    with open(_POLICY_PATH) as _fh:
        POLICY = json.load(_fh)
except FileNotFoundError:
    POLICY = {}


def _f(name, default):
    key = name.lower()
    return float(os.environ.get(name, POLICY.get(key, default)))


RULES = {
    "price_tolerance_pct": _f("PRICE_TOLERANCE_PCT", 2.0),       # unit price may exceed PO price by up to 2%
    "amount_tolerance_pct": _f("AMOUNT_TOLERANCE_PCT", 1.0),     # invoice may exceed remaining PO value by 1% ...
    "amount_tolerance_abs": _f("AMOUNT_TOLERANCE_ABS", 2500),    # ... capped at ₹2,500 (whichever is lower)
    "rounding_tolerance": _f("ROUNDING_TOLERANCE", 1.0),
    "tax_rate_tolerance_pp": _f("TAX_RATE_TOLERANCE_PP", 0.5),         # arithmetic checks allow ₹1 rounding
    "max_invoice_age_days": _f("MAX_INVOICE_AGE_DAYS", 180),
    "near_duplicate_window_days": _f("NEAR_DUPLICATE_WINDOW_DAYS", 30),
    "vendor_name_match_threshold": _f("VENDOR_NAME_MATCH_THRESHOLD", 85),
    "line_match_threshold": _f("LINE_MATCH_THRESHOLD", 70),
    "po_inference_min_confidence": _f("PO_INFERENCE_MIN_CONFIDENCE", 60),
    "low_confidence_threshold": _f("LOW_CONFIDENCE_THRESHOLD", 0.6),
    "payment_terms_days": _f("PAYMENT_TERMS_DAYS", 30),
}

RULEBOOK = [
    ("Data quality", [
        "Invoice number, date, vendor identity and total must be present — otherwise FLAG FOR REVIEW and ask the vendor.",
        "Line items must add up to the subtotal, and subtotal + tax must equal the total (₹1 rounding).",
        "Invoice date cannot be in the future or older than {max_invoice_age_days:.0f} days.",
        "Invoice must be billed to our GSTIN (else input tax credit is at risk).",
        "Tax-inclusive invoices are normalised to pre-tax value before comparing with the PO.",
    ]),
    ("Vendor", [
        "Vendor must exist in the vendor master (GSTIN match, or name match ≥ {vendor_name_match_threshold:.0f}%) — else REJECT.",
        "Blocked vendors are REJECTED.",
        "GSTIN must pass format + checksum validation.",
        "Bank account on the invoice must match the vendor master — else FLAG FOR REVIEW as a payment-fraud risk.",
        "Payment is scheduled on the vendor's agreed terms; shorter terms printed on an invoice are noted, not followed.",
    ]),
    ("Duplicates", [
        "Same vendor + same invoice number (ignoring formatting like INV-0042 vs INV42) → REJECT.",
        "Same file already posted → REJECT.",
        "Same vendor + same total within {near_duplicate_window_days:.0f} days under a different number → FLAG FOR REVIEW.",
    ]),
    ("PO matching", [
        "PO number on the invoice must exist, belong to this vendor and be open.",
        "No PO number: suggest the best open PO (item similarity + amount fit) but always FLAG FOR REVIEW for a human to confirm.",
    ]),
    ("Reconciliation", [
        "Every invoice line must map to a PO line (similarity ≥ {line_match_threshold:.0f}%).",
        "Bundled invoice (one lump-sum line for a multi-line PO): accepted only if the total matches the open PO value within tolerance; every PO line is then checked for receipt.",
        "Unit price may exceed PO price by up to {price_tolerance_pct:.1f}% (note); more is FLAGGED FOR REVIEW.",
        "Quantity billed cannot exceed quantity still open on the PO (handles split / partial invoices).",
        "Three-way match for goods: quantity billed cannot exceed quantity received (GRN) and not yet invoiced. Service POs are two-way.",
        "Invoice value may exceed the remaining PO value by min({amount_tolerance_pct:.1f}%, ₹{amount_tolerance_abs:,.0f}).",
        "GST type must match the supply: same state = CGST+SGST, different state = IGST; rate must match the PO within {tax_rate_tolerance_pp:.1f} pp.",
    ]),
    ("Decision", [
        "Any REJECT rule → Rejected. Any REVIEW rule → Flagged for review. Otherwise → Approved and posted.",
        "AI reads the document; it never makes the decision. Every decision traces to a rule above.",
    ]),
    ("Confidence & human review", [
        "Every extracted field carries a confidence score (AI reader score, or Tesseract's word confidence for scans).",
        "Two independent readers disagreeing caps a field at 50%; passing arithmetic or master-data checks raises it.",
        "A critical field below {low_confidence_threshold:.0%} confidence → FLAG FOR REVIEW by a human to verify.",
        "Reviewers can correct any extracted field; the invoice is re-run through every check and the original is kept for audit.",
        "Invoices are processed one at a time in arrival order, so two invoices never draw on the same PO balance at once.",
    ]),
]


def rulebook():
    return [{"section": s, "rules": [r.format(**RULES) for r in rs]} for s, rs in RULEBOOK]


def duplicate_rules():
    return POLICY.get("duplicate_rules", [])
