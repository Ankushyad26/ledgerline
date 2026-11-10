"""Single source of truth for the fictional company's master data.

Buyer: Kestrel Manufacturing Pvt Ltd (Pune, Maharashtra).
Running this file writes data/seed/*.csv which the app loads on startup / reset.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.gstin import make_gstin  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
SEED = os.path.join(ROOT, "data", "seed")

BUYER = {
    "name": "Kestrel Manufacturing Pvt Ltd",
    "gstin": make_gstin("27", "AAACK4321R"),
    "address": "Plot 14, MIDC Chakan, Pune, Maharashtra 410501",
}

VENDORS = [
    dict(vendor_id="V001", name="Nimbus Office Supplies Pvt Ltd", gstin=make_gstin("27", "AABCN1234F"),
         city="Mumbai", state="Maharashtra", bank_name="HDFC Bank", bank_account="50100234567890",
         ifsc="HDFC0000123", email="accounts@nimbusoffice.in", status="active", status_note="", payment_terms_days=30),
    dict(vendor_id="V002", name="Orbit Systems India Pvt Ltd", gstin=make_gstin("29", "AACCO5678K"),
         city="Bengaluru", state="Karnataka", bank_name="ICICI Bank", bank_account="002705001234",
         ifsc="ICIC0000027", email="billing@orbitsystems.co.in", status="active", status_note="", payment_terms_days=45),
    dict(vendor_id="V003", name="Shakti Industrial Components", gstin=make_gstin("24", "ABAFS9012M"),
         city="Ahmedabad", state="Gujarat", bank_name="State Bank of India", bank_account="30987654321",
         ifsc="SBIN0001234", email="accounts@shaktiindustrial.com", status="active", status_note="", payment_terms_days=30),
    dict(vendor_id="V004", name="GreenLeaf Facility Services LLP", gstin=make_gstin("27", "AAJFG3456P"),
         city="Pune", state="Maharashtra", bank_name="Axis Bank", bank_account="917020045678901",
         ifsc="UTIB0000456", email="invoices@greenleaffs.in", status="active", status_note="", payment_terms_days=15),
    dict(vendor_id="V005", name="Apex Logistics Solutions Pvt Ltd", gstin=make_gstin("07", "AAKCA7890Q"),
         city="New Delhi", state="Delhi", bank_name="Kotak Mahindra Bank", bank_account="4512345678",
         ifsc="KKBK0000958", email="ar@apexlogistics.in", status="active", status_note="", payment_terms_days=30),
    dict(vendor_id="V006", name="Vertex Chemicals Ltd", gstin=make_gstin("24", "AABCV2468H"),
         city="Vadodara", state="Gujarat", bank_name="Bank of Baroda", bank_account="11220100004455",
         ifsc="BARB0VADODA", email="accounts@vertexchem.in", status="blocked",
         status_note="Blocked after failed compliance audit (Sep 2026)", payment_terms_days=30),
]

# (po_number, vendor_id, po_date, status, gst_rate, description, [(item, qty, unit_price, invoiced_qty)])
# Service POs (housekeeping, pest control, freight, courier) need no goods receipt -> two-way match.
SERVICE_POS = {"PO-1005", "PO-1006", "PO-1007", "PO-1009"}
POS = [
    ("PO-0990", "V001", "2026-08-10", "closed", 18, "Stationery top-up",
     [("Whiteboard markers (box of 10)", 100, 40, 100)]),
    ("PO-1001", "V001", "2026-10-05", "open", 18, "Q4 printing consumables",
     [("A4 copier paper 75gsm (ream)", 200, 260, 0), ("HP 26A toner cartridge", 10, 4800, 0)]),
    ("PO-1002", "V002", "2026-09-15", "open", 18, "Laptop refresh - engineering team",
     [("Dell Latitude 5450 laptop (i5/16GB/512GB)", 25, 80000, 12)]),
    ("PO-1003", "V003", "2026-10-12", "open", 18, "Assembly line components",
     [("MS steel mounting bracket", 2000, 45, 0), ("Hex bolt M10 x 40 (zinc plated)", 5000, 6, 0),
      ("Nitrile rubber gasket 50mm", 1000, 30, 0)]),
    ("PO-1004", "V003", "2026-10-12", "open", 18, "Packaging material",
     [("Corrugated shipping box 5-ply", 3000, 22, 0), ("Stretch film roll 23 micron", 200, 450, 0)]),
    ("PO-1005", "V004", "2026-10-28", "open", 18, "Housekeeping - Pune plant",
     [("Housekeeping services - November 2026 (monthly)", 1, 120000, 0)]),
    ("PO-1006", "V004", "2026-10-28", "open", 18, "Pest control - Pune plant",
     [("Pest control - quarterly service (Q4)", 1, 36000, 0)]),
    ("PO-1007", "V005", "2026-10-01", "open", 18, "Outbound freight Pune-Delhi",
     [("Freight Pune-Delhi FTL (per trip)", 4, 10000, 4)]),
    ("PO-1008", "V001", "2026-10-20", "open", 18, "Office furniture",
     [("Ergonomic office chair (mesh back)", 20, 6500, 0)]),
    ("PO-1009", "V005", "2026-10-25", "open", 18, "Courier services",
     [("Courier & last-mile services - October 2026", 1, 25000, 0)]),
    ("PO-1010", "V003", "2026-10-30", "open", 18, "Warehouse pallets",
     [("Wooden pallet 1200x1000mm", 100, 850, 0)]),
    ("PO-1011", "V006", "2026-08-01", "open", 18, "Cleaning chemicals",
     [("Industrial degreaser (20L can)", 40, 2200, 0)]),
    ("PO-1012", "V002", "2026-10-15", "open", 18, "Monitors - design studio",
     [("Dell 27-inch 4K monitor P2723QE", 30, 18000, 0)]),
    ("PO-1013", "V003", "2026-10-20", "open", 18, "Maintenance spares kit",
     [("Ball bearing 6205-2Z", 50, 180, 0), ("V-belt B-52", 40, 350, 0), ("Lithium grease cartridge 400g", 100, 120, 0)]),
]

# Goods receipt notes (what the stores team actually received): grn, po, line_no, received_qty, date
GRNS = [
    ("GRN-5001", "PO-0990", 1, 100, "2026-08-20"),
    ("GRN-5010", "PO-1001", 1, 200, "2026-11-04"), ("GRN-5010", "PO-1001", 2, 10, "2026-11-04"),
    ("GRN-5002", "PO-1002", 1, 12, "2026-09-29"), ("GRN-5011", "PO-1002", 1, 8, "2026-11-01"),
    ("GRN-5012", "PO-1003", 1, 2000, "2026-11-02"), ("GRN-5012", "PO-1003", 2, 5000, "2026-11-02"),
    ("GRN-5012", "PO-1003", 3, 1000, "2026-11-02"),
    ("GRN-5013", "PO-1004", 1, 3000, "2026-11-05"), ("GRN-5013", "PO-1004", 2, 200, "2026-11-05"),
    ("GRN-5014", "PO-1008", 1, 20, "2026-11-07"),
    ("GRN-5015", "PO-1010", 1, 100, "2026-11-03"),
    ("GRN-5003", "PO-1011", 1, 40, "2026-08-12"),
    ("GRN-5016", "PO-1012", 1, 18, "2026-11-06"),   # only 18 of 30 monitors delivered so far
    ("GRN-5017", "PO-1013", 1, 50, "2026-11-04"), ("GRN-5017", "PO-1013", 2, 40, "2026-11-04"),
    ("GRN-5017", "PO-1013", 3, 100, "2026-11-04"),
]

# AP policy: tolerance thresholds and duplicate-detection rules (part of the reference dataset, editable by finance)
POLICY = {
    "price_tolerance_pct": 2.0,
    "amount_tolerance_pct": 1.0,
    "amount_tolerance_abs": 2500,
    "tax_rate_tolerance_pp": 0.5,
    "rounding_tolerance": 1.0,
    "max_invoice_age_days": 180,
    "near_duplicate_window_days": 30,
    "vendor_name_match_threshold": 85,
    "line_match_threshold": 70,
    "po_inference_min_confidence": 60,
    "low_confidence_threshold": 0.6,
    "payment_terms_days": 30,
    "duplicate_rules": [
        {"rule": "Same vendor + same invoice number after normalising case, punctuation and leading zeros",
         "action": "reject"},
        {"rule": "Same file fingerprint (SHA-256) as an invoice already posted", "action": "reject"},
        {"rule": "Same vendor + same total within near_duplicate_window_days under a different number",
         "action": "review"},
    ],
}

# Invoices already processed & approved before the demo starts.
HISTORY = [
    dict(vendor_id="V001", invoice_number="NOS/2026/0877", invoice_date="2026-08-25", po_number="PO-0990",
         subtotal=4000, tax=720, total=4720, source="seed"),
    dict(vendor_id="V002", invoice_number="ORB/26-27/0311", invoice_date="2026-10-02", po_number="PO-1002",
         subtotal=960000, tax=172800, total=1132800, source="seed"),
    dict(vendor_id="V005", invoice_number="INV42", invoice_date="2026-10-20", po_number="PO-1007",
         subtotal=40000, tax=7200, total=47200, source="seed"),
]


def write():
    os.makedirs(SEED, exist_ok=True)
    with open(os.path.join(SEED, "vendors.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(VENDORS[0].keys()))
        w.writeheader()
        w.writerows(VENDORS)
    with open(os.path.join(SEED, "purchase_orders.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["po_number", "vendor_id", "po_date", "status", "gst_rate", "description", "receipt_required"])
        for po in POS:
            w.writerow(list(po[:6]) + [0 if po[0] in SERVICE_POS else 1])
    with open(os.path.join(SEED, "po_lines.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["po_number", "line_no", "item", "qty", "unit_price", "invoiced_qty"])
        for po in POS:
            for i, (item, qty, price, inv) in enumerate(po[6], 1):
                w.writerow([po[0], i, item, qty, price, inv])
    with open(os.path.join(SEED, "goods_receipts.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["grn_number", "po_number", "line_no", "received_qty", "grn_date"])
        w.writerows(GRNS)
    with open(os.path.join(SEED, "invoice_history.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(HISTORY[0].keys()))
        w.writeheader()
        w.writerows(HISTORY)
    with open(os.path.join(SEED, "buyer.json"), "w") as f:
        json.dump(BUYER, f, indent=2)
    with open(os.path.join(SEED, "policy.json"), "w") as f:
        json.dump(POLICY, f, indent=2)
    print("seed written to", SEED)


if __name__ == "__main__":
    write()
