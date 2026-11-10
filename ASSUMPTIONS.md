# Assumptions

The brief treats ambiguity as part of the exercise. These are the calls I made, and why.

## Business context
- **Buyer:** Kestrel Manufacturing Pvt Ltd, a mid-size manufacturer in Pune, Maharashtra (GSTIN state code 27). Indian GST rules apply.
- **Currency:** INR only. Invoices are single-page, one invoice per file.
- **Master data:** the vendor master, open POs and prior invoice history live in the "procurement system". It is simulated with CSVs in `data/seed/` and loaded into SQLite. In production these would come from the ERP.
- **Three-way match for goods** (PO ↔ goods receipt ↔ invoice): quantity billed must be within what is still open on the PO *and* what stores have received but not yet been invoiced for. Goods receipts are simulated in `goods_receipts.csv`.
- **Service POs are two-way** (housekeeping, pest control, freight, courier), because services have no goods receipt. In a real company a service-entry sheet would play that role.
- **Payment terms** come from the vendor master (the contract). Terms printed on an invoice are read and compared; shorter terms are noted, but payment is scheduled on the agreed terms.

## Decision policy
Thresholds and duplicate rules are part of the reference dataset (`data/seed/policy.json`), so finance can change them
without code. An UPPER_CASE environment variable of the same name overrides a value.

| Rule | Value | Reasoning |
|---|---|---|
| Unit-price tolerance | +2% | Common AP policy for rounding and freight pass-through. Lower prices are always accepted. |
| Invoice-value tolerance over the open PO value | min(1%, ₹2,500) | A percentage alone is too loose on large POs, so it is capped in rupees. |
| Rounding tolerance on arithmetic | ₹1 | Paise rounding on tax lines. |
| Invoice age limit | 180 days | Stale invoices are a common route to duplicate payment. |
| Near-duplicate window | 30 days | Same vendor and same amount under a different number → review, not reject (could be a recurring monthly charge). |
| Inferred PO | Always REVIEW | Matching on a guess is how wrong payments happen; the system suggests the PO, a human confirms it. |
| Bank account ≠ vendor master | REVIEW (not reject) | Vendors do legitimately change banks. The fix is verification by call-back, not refusal. |
| Unknown or blocked vendor | REJECT | No payment to anyone outside the approved vendor master. |
| Exact or format-variant duplicate | REJECT | `INV-0042` = `INV42` = `inv 42` after normalisation (case, punctuation, leading zeros). |

| Billed qty > received and not yet invoiced | REVIEW → Stores | Paying for undelivered goods is a direct cash loss; stores confirm delivery. |
| Field confidence below 60% on a critical field | REVIEW | Never pay on a shaky read. |
| Bundled lump-sum line for a multi-line PO | Approve with note *only if* total = open PO value within tolerance; else REVIEW | Many small vendors don't itemise. Matching on the total and allocating across PO lines keeps the qty and goods-receipt controls. |
| GST rate vs PO rate | ±0.5 percentage points | Rounding on tax lines; anything more is a wrong rate. |

**Severity model:** any REJECT → Rejected; otherwise any REVIEW flag → Flagged for review; otherwise → Approved. Warnings (within tolerance) are recorded as notes on approved invoices.

## GST
- Vendor state = buyer state → CGST + SGST expected; different states → IGST. A wrong GST type blocks input tax credit, so it is flagged for the tax team.
- The effective tax rate must be within 0.5 percentage points of the rate on the PO.
- Tax-inclusive invoices: the pre-tax value is derived as total − tax, and unit prices are compared net of tax.
- GSTINs are validated for format and checksum (the real GSTIN check-digit algorithm), not against the live GST portal.

## AI usage
- The vision LLM only **extracts** data into a fixed JSON schema, with an instruction never to infer values that aren't printed. It never makes the decision.
- An independent rule-based parser cross-checks the AI on 5 key fields. Disagreements are flagged as warnings; the AI value is used.
- If the AI API fails or no key is set, the rule-based parser (plus Tesseract OCR for scans) takes over, and the run notes that a fallback was used.
- Vendor emails are template-based, not generated, so the wording is predictable and auditable.

## Confidence scores
- AI path: the model's own per-field confidence. Offline path: 98% for a digital text layer; for scans, Tesseract's real mean word confidence.
- When the AI and the rule parser disagree on a field, it is capped at 50%. Passing an independent check raises it: arithmetic (≥97%) or a GSTIN match against the vendor master (99%).
- The run's "data confidence" is the lowest score across the required fields.

## Human corrections
- A reviewer can correct any extracted field or line item on a held or rejected run. This creates a **new run** that repeats every check on the corrected data; the original is marked *superseded*, with an old → new record of each change, who made it and why.
- Corrections never bypass rules: a corrected invoice still has to pass vendor, duplicate, PO, goods-receipt and tolerance checks.

## Processing order
- One worker processes runs in arrival order. That is a correctness choice: two invoices against the same PO must not be checked against the same open balance at the same time (tested in `tests/`).

## Demo mechanics
- Each stage has a 0.45 s visual pacing delay (`STAGE_DELAY`) so a viewer can follow the live view. Reported processing times exclude this delay.
- **Reset demo data** restores the master data and clears runs, so the split-PO and duplicate scenarios can be re-run in order.
- Approved invoices consume PO quantities. That is why re-running scenario 01 after it was approved is correctly rejected as a duplicate.
