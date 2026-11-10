"""Generate realistic sample invoice PDFs (4 vendor layouts, one scanned) for the demo.

Each sample is designed to exercise a specific path through the pipeline.
"""
import io
import os
import random
import sys
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.seed_data import BUYER, VENDORS  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "samples")
W, H = A4
V = {v["vendor_id"]: v for v in VENDORS}
ADDR = {
    "V001": "402, Crystal Plaza, Andheri East, Mumbai, Maharashtra 400069",
    "V002": "3rd Floor, Prestige Tech Park, Outer Ring Road, Bengaluru, Karnataka 560103",
    "V003": "Plot 88, GIDC Phase II, Vatva, Ahmedabad, Gujarat 382445",
    "V004": "Office 7, Sai Heights, Baner Road, Pune, Maharashtra 411045",
    "V005": "B-21, Okhla Industrial Area Phase I, New Delhi 110020",
    "V006": "17, Makarpura GIDC, Vadodara, Gujarat 390010",
}


def inr(x: float) -> str:
    neg = x < 0
    x = abs(round(x, 2))
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
    return ("-" if neg else "") + whole + "." + frac


def fmt_date(d: str | None, style: str) -> str:
    if not d:
        return ""
    y, m, dd = map(int, d.split("-"))
    dt = date(y, m, dd)
    return {
        "dmy-mon": dt.strftime("%d-%b-%Y"),
        "dmy-slash": dt.strftime("%d/%m/%Y"),
        "long": f"{dt.day} {dt.strftime('%B %Y')}",
        "iso": dt.isoformat(),
    }[style]


def totals(inv):
    sub = sum(q * p for _, _, q, p in inv["lines"])
    rate = 0.18
    if inv.get("inclusive"):
        gross = sub
        tax = round(gross - gross / (1 + rate), 2)
        return dict(sub=round(gross - tax, 2), tax=tax, total=gross)
    tax = round(sub * rate, 2)
    return dict(sub=sub, tax=tax, total=sub + tax)


def intra(v):
    return v["gstin"][:2] == BUYER["gstin"][:2]


# ---------------------------------------------------------------- layouts
def classic(c, inv, accent):
    v = V[inv["vendor"]]
    t = totals(inv)
    c.setFillColor(colors.HexColor(accent))
    c.rect(0, H - 8, W, 8, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 17)
    c.drawString(40, H - 55, v["name"])
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 70, ADDR[v["vendor_id"]])
    c.drawString(40, H - 82, f"GSTIN: {v['gstin']}    Email: {v['email']}")
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(colors.HexColor(accent))
    c.drawRightString(W - 40, H - 55, "TAX INVOICE")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 10)
    y = H - 120
    meta = [("Invoice No.", inv.get("number") or ""), ("Invoice Date", fmt_date(inv.get("date"), "dmy-mon")),
            ("PO Number", inv.get("po") or "")]
    c.setStrokeColor(colors.HexColor("#cccccc"))
    c.rect(W - 250, y - 48, 210, 62, stroke=1, fill=0)
    for i, (k, val) in enumerate(meta):
        c.setFont("Helvetica-Bold", 9)
        c.drawString(W - 240, y - i * 18, k + ":")
        c.setFont("Helvetica", 9)
        c.drawString(W - 160, y - i * 18, val)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Bill To:")
    c.setFont("Helvetica", 9)
    c.drawString(40, y - 14, BUYER["name"])
    c.drawString(40, y - 26, BUYER["address"])
    c.drawString(40, y - 38, f"GSTIN: {BUYER['gstin']}")
    # table
    y = H - 210
    cols = [40, 65, 330, 385, 440, 500]
    c.setFillColor(colors.HexColor("#f0f0f0"))
    c.rect(36, y - 6, W - 72, 20, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    for x, h in zip(cols, ["#", "Description", "HSN/SAC", "Qty", "Rate", "Amount"]):
        c.drawString(x, y, h)
    c.setFont("Helvetica", 9)
    for i, (desc, hsn, q, p) in enumerate(inv["lines"], 1):
        y -= 22
        c.drawString(cols[0], y, str(i))
        c.drawString(cols[1], y, desc)
        c.drawString(cols[2], y, hsn)
        c.drawRightString(cols[3] + 40, y, f"{q:g}")
        c.drawRightString(cols[4] + 50, y, inr(p))
        c.drawRightString(W - 42, y, inr(q * p))
    y -= 14
    c.line(36, y, W - 36, y)
    rows = [("Sub Total", t["sub"])]
    if intra(v):
        rows += [("CGST @ 9%", t["tax"] / 2), ("SGST @ 9%", t["tax"] / 2)]
    else:
        rows += [("IGST @ 18%", t["tax"])]
    for k, val in rows:
        y -= 18
        c.drawString(360, y, k)
        c.drawRightString(W - 42, y, inr(val))
    y -= 22
    c.setFont("Helvetica-Bold", 11)
    c.drawString(360, y, "Total Amount (INR)")
    c.drawRightString(W - 42, y, inr(t["total"]))
    bank = inv.get("bank") or (v["bank_name"], v["bank_account"], v["ifsc"])
    y -= 60
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Bank Details for Payment")
    c.setFont("Helvetica", 9)
    c.drawString(40, y - 14, f"Bank: {bank[0]}")
    c.drawString(40, y - 26, f"A/c No.: {bank[1]}")
    c.drawString(40, y - 38, f"IFSC: {bank[2]}")
    if inv.get("note"):
        c.setFillColor(colors.HexColor("#b00020"))
        c.setFont("Helvetica-Bold", 9)
        for j, line in enumerate(inv["note"]):
            c.drawString(40, y - 62 - j * 13, line)
        c.setFillColor(colors.black)
    c.setFont("Helvetica", 8)
    c.drawString(40, 60, f"Payment terms: {inv.get('terms') or 'Net %d days' % v['payment_terms_days']}. "
                         "This is a computer generated invoice.")
    c.drawRightString(W - 40, 80, f"For {v['name']}")
    c.drawRightString(W - 40, 60, "Authorised Signatory")


def modern(c, inv, accent):
    v = V[inv["vendor"]]
    t = totals(inv)
    c.setFillColor(colors.HexColor(accent))
    c.rect(0, H - 110, W, 110, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, H - 50, v["name"].upper())
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 68, ADDR[v["vendor_id"]])
    c.drawString(40, H - 82, f"GSTIN {v['gstin']}  |  {v['email']}")
    c.setFont("Helvetica-Bold", 24)
    c.drawRightString(W - 40, H - 95, "INVOICE")
    c.setFillColor(colors.black)
    y = H - 145
    c.setFont("Helvetica", 10)
    fields = []
    if inv.get("number"):
        fields.append(("Bill No.", inv["number"]))
    if inv.get("date"):
        fields.append(("Dated", fmt_date(inv["date"], "dmy-slash")))
    if inv.get("po"):
        fields.append(("Buyer's Order No.", inv["po"]))
    for i, (k, val) in enumerate(fields):
        c.setFont("Helvetica-Bold", 10)
        c.drawString(40, y - i * 16, f"{k}:")
        c.setFont("Helvetica", 10)
        c.drawString(150, y - i * 16, val)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(330, y, "Buyer")
    c.setFont("Helvetica", 9)
    c.drawString(330, y - 14, BUYER["name"])
    c.drawString(330, y - 26, "MIDC Chakan, Pune 410501")
    c.drawString(330, y - 38, f"GSTIN {BUYER['gstin']}")
    y = H - 230
    c.setFillColor(colors.HexColor(accent))
    c.rect(36, y - 6, W - 72, 20, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(44, y, "Item")
    c.drawRightString(390, y, "Quantity")
    c.drawRightString(470, y, "Unit Price (Rs.)")
    c.drawRightString(W - 44, y, "Line Total (Rs.)")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    for desc, hsn, q, p in inv["lines"]:
        y -= 22
        c.drawString(44, y, desc)
        c.drawRightString(390, y, f"{q:g}")
        c.drawRightString(470, y, inr(p))
        c.drawRightString(W - 44, y, inr(q * p))
        c.setStrokeColor(colors.HexColor("#e5e5e5"))
        c.line(36, y - 8, W - 36, y - 8)
    y -= 30
    c.setFont("Helvetica", 10)
    c.drawString(360, y, "Taxable Value")
    c.drawRightString(W - 44, y, inr(t["sub"]))
    y -= 18
    c.drawString(360, y, "IGST 18%")
    c.drawRightString(W - 44, y, inr(t["tax"]))
    y -= 24
    c.setFont("Helvetica-Bold", 12)
    c.drawString(360, y, "Grand Total")
    c.drawRightString(W - 44, y, "Rs. " + inr(t["total"]))
    y -= 70
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Remit to")
    c.setFont("Helvetica", 9)
    c.drawString(40, y - 14, f"{v['bank_name']}  |  A/c {v['bank_account']}  |  IFSC {v['ifsc']}")
    c.setFont("Helvetica", 9)
    c.drawString(40, y - 34, f"Terms: {inv.get('terms') or 'Net %d days' % v['payment_terms_days']}")
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(40, 60, "Goods once sold will not be taken back. Subject to Ahmedabad jurisdiction.")


def service(c, inv, accent):
    v = V[inv["vendor"]]
    t = totals(inv)
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(W / 2, H - 60, v["name"])
    c.setFont("Helvetica", 10)
    c.drawCentredString(W / 2, H - 76, ADDR[v["vendor_id"]])
    c.drawCentredString(W / 2, H - 90, f"GSTIN: {v['gstin']}")
    c.line(40, H - 100, W - 40, H - 100)
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(W / 2, H - 125, "TAX INVOICE")
    c.setFont("Helvetica", 11)
    c.drawString(50, H - 160, f"Invoice #: {inv['number']}")
    c.drawString(50, H - 178, f"Date: {fmt_date(inv['date'], 'long')}")
    c.drawString(50, H - 210, "To: " + BUYER["name"])
    c.drawString(50, H - 226, "     Pune Plant, MIDC Chakan, Pune 410501")
    c.drawString(50, H - 242, f"     GSTIN: {BUYER['gstin']}")
    y = H - 290
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, "Description")
    c.drawRightString(380, y, "Qty")
    c.drawRightString(460, y, "Rate")
    c.drawRightString(W - 50, y, "Amount")
    c.line(45, y - 6, W - 45, y - 6)
    c.setFont("Helvetica", 11)
    for desc, hsn, q, p in inv["lines"]:
        y -= 24
        c.drawString(50, y, desc)
        c.drawRightString(380, y, f"{q:g}")
        c.drawRightString(460, y, inr(p))
        c.drawRightString(W - 50, y, inr(q * p))
    y -= 14
    c.line(45, y, W - 45, y)
    for k, val in [("Sub Total", t["sub"]), ("CGST 9%", t["tax"] / 2), ("SGST 9%", t["tax"] / 2)]:
        y -= 22
        c.drawString(330, y, k)
        c.drawRightString(W - 50, y, inr(val))
    y -= 26
    c.setFont("Helvetica-Bold", 12)
    c.drawString(330, y, "Total Payable")
    c.drawRightString(W - 50, y, inr(t["total"]))
    c.setFont("Helvetica", 10)
    c.drawString(50, y - 60, f"Bank: {v['bank_name']}  A/c: {v['bank_account']}  IFSC: {v['ifsc']}")
    c.drawString(50, y - 78, f"Payment terms: {inv.get('terms') or 'Net %d days' % v['payment_terms_days']}")
    c.drawString(50, y - 100, "Thank you for your business.")
    c.drawRightString(W - 50, 90, "for " + v["name"])
    c.setFont("Helvetica-Oblique", 14)
    c.drawRightString(W - 60, 110, "R. Kulkarni")


def apex(c, inv, accent):
    v = V[inv["vendor"]]
    t = totals(inv)
    c.setFillColor(colors.HexColor(accent))
    c.circle(58, H - 58, 20, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(58, H - 64, "A")
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(88, H - 52, v["name"])
    c.setFont("Helvetica", 8.5)
    c.drawString(88, H - 66, ADDR[v["vendor_id"]] + f"  |  GSTIN {v['gstin']}")
    y = H - 110
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, "Freight & Logistics Bill")
    c.setFont("Helvetica", 9.5)
    y -= 26
    for k, val in [("Invoice Ref", inv["number"]), ("Bill Date", fmt_date(inv["date"], "iso")),
                   ("Your PO", inv.get("po") or "-"), ("Billed to", f"{BUYER['name']} ({BUYER['gstin']})")]:
        c.drawString(40, y, f"{k}")
        c.drawString(130, y, f": {val}")
        y -= 15
    y -= 20
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(40, y, "Particulars")
    c.drawRightString(390, y, "Qty")
    c.drawRightString(470, y, "Rate")
    c.drawRightString(W - 40, y, "Amount")
    c.line(36, y - 6, W - 36, y - 6)
    c.setFont("Helvetica", 9.5)
    for desc, hsn, q, p in inv["lines"]:
        y -= 22
        c.drawString(40, y, desc)
        c.drawRightString(390, y, f"{q:g}")
        c.drawRightString(470, y, inr(p))
        c.drawRightString(W - 40, y, inr(q * p))
    y -= 14
    c.line(36, y, W - 36, y)
    if inv.get("inclusive"):
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(40, y - 18, "Rates are inclusive of GST.")
        c.setFont("Helvetica-Bold", 11)
        y -= 40
        c.drawString(300, y, "Amount payable (incl. GST)")
        c.drawRightString(W - 40, y, inr(t["total"]))
        c.setFont("Helvetica", 9)
        y -= 16
        c.drawString(300, y, "Includes IGST @ 18%")
        c.drawRightString(W - 40, y, inr(t["tax"]))
    else:
        c.setFont("Helvetica", 10)
        y -= 20
        c.drawString(300, y, "Taxable amount")
        c.drawRightString(W - 40, y, inr(t["sub"]))
        y -= 16
        c.drawString(300, y, "IGST @ 18%")
        c.drawRightString(W - 40, y, inr(t["tax"]))
        y -= 22
        c.setFont("Helvetica-Bold", 11)
        c.drawString(300, y, "Amount payable")
        c.drawRightString(W - 40, y, inr(t["total"]))
    c.setFont("Helvetica", 9)
    c.drawString(40, y - 60, f"Pay to: {v['bank_name']}, A/c {v['bank_account']}, IFSC {v['ifsc']}")
    c.drawString(40, y - 76, f"Payment terms: {inv.get('terms') or 'Net %d days' % v['payment_terms_days']}")
    c.drawString(40, 60, "E. & O.E.  Queries: " + v["email"])


LAYOUTS = {"classic": classic, "modern": modern, "service": service, "apex": apex}


def render(inv) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Invoice {inv.get('number') or ''}")
    c.setAuthor(V[inv["vendor"]]["name"])
    LAYOUTS[inv["layout"]](c, inv, inv.get("accent", "#1f4e79"))
    c.showPage()
    c.save()
    return buf.getvalue()


def scanify(pdf_bytes: bytes) -> bytes:
    """Rasterise to a noisy, slightly rotated greyscale image - like a phone scan."""
    import pdfplumber
    from PIL import ImageFilter

    random.seed(7)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        img = pdf.pages[0].to_image(resolution=150).original.convert("L")
    img = img.rotate(-0.9, expand=True, fillcolor=245)
    px = img.load()
    for _ in range(int(img.width * img.height * 0.004)):
        x, y = random.randrange(img.width), random.randrange(img.height)
        px[x, y] = random.choice([90, 140, 200])
    img = img.filter(ImageFilter.GaussianBlur(0.5))
    img = img.point(lambda p: min(255, int(p * 0.94 + 8)))
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PDF", resolution=150)
    return out.getvalue()


L = lambda *a: a  # noqa: E731

SAMPLES = [
    dict(file="01_happy_path_nimbus.pdf", layout="classic", accent="#1f4e79", vendor="V001",
         number="NOS/2026/1042", date="2026-11-06", po="PO-1001",
         lines=[L("A4 copier paper 75gsm (ream)", "4802", 200, 260), L("HP 26A toner cartridge", "8443", 10, 4800)]),
    dict(file="02_split_po_part2_orbit.pdf", layout="classic", accent="#6a1b9a", vendor="V002",
         number="ORB/26-27/0398", date="2026-11-03", po="PO-1002",
         lines=[L("Dell Latitude 5450 laptop (i5/16GB/512GB)", "8471", 8, 80000)]),
    dict(file="03_split_po_overrun_orbit.pdf", layout="classic", accent="#6a1b9a", vendor="V002",
         number="ORB/26-27/0421", date="2026-11-08", po="PO-1002",
         lines=[L("Dell Latitude 5450 laptop (i5/16GB/512GB)", "8471", 7, 80000)]),
    dict(file="04_within_tolerance_shakti.pdf", layout="modern", accent="#c0392b", vendor="V003",
         number="SIC/1187", date="2026-11-04", po="PO/1003",
         lines=[L("MS steel mounting bracket", "7326", 2000, 45.40), L("Hex bolt M10 x 40 (zinc plated)", "7318", 5000, 6),
                L("Nitrile rubber gasket 50mm", "4016", 1000, 30)]),
    dict(file="05_price_variance_shakti.pdf", layout="modern", accent="#c0392b", vendor="V003",
         number="SIC/1192", date="2026-11-07", po="PO 1004",
         lines=[L("Corrugated shipping box 5-ply", "4819", 3000, 22), L("Stretch film roll 23 micron", "3920", 200, 486)]),
    dict(file="06_scanned_no_po_greenleaf.pdf", layout="service", vendor="V004", scanned=True,
         number="GL/INV/2026/311", date="2026-11-05", po=None,
         lines=[L("Housekeeping services - November 2026", "9985", 1, 120000)]),
    dict(file="07_duplicate_resubmission_apex.pdf", layout="apex", accent="#00897b", vendor="V005",
         number="INV-0042", date="2026-10-20", po="PO-1007",
         lines=[L("Freight Pune-Delhi FTL (per trip)", "9965", 4, 10000)]),
    dict(file="08_bank_details_changed_nimbus.pdf", layout="classic", accent="#1f4e79", vendor="V001",
         number="NOS/2026/1057", date="2026-11-09", po="PO-1008",
         lines=[L("Ergonomic office chair (mesh back)", "9401", 20, 6500)],
         bank=("Yes Bank", "023481900007711", "YESB0000234"),
         note=["IMPORTANT: Our bank details have changed with immediate effect.",
               "Kindly update your records and remit this payment to the new account above."]),
    dict(file="09_tax_inclusive_apex.pdf", layout="apex", accent="#00897b", vendor="V005", inclusive=True,
         terms="Due on receipt",
         number="APX/2026/1310", date="2026-11-02", po="PO-1009",
         lines=[L("Courier & last-mile services - October 2026", "9968", 1, 29500)]),
    dict(file="10_missing_fields_shakti.pdf", layout="modern", accent="#c0392b", vendor="V003",
         number=None, date=None, po="PO-1010",
         lines=[L("Wooden pallet 1200x1000mm", "4415", 100, 850)]),
    dict(file="11_blocked_vendor_vertex.pdf", layout="classic", accent="#455a64", vendor="V006",
         number="VCL/2026/0219", date="2026-11-01", po="PO-1011",
         lines=[L("Industrial degreaser (20L can)", "3402", 40, 2200)]),
    dict(file="13_bundled_lines_shakti.pdf", layout="modern", accent="#c0392b", vendor="V003",
         number="SIC/1196", date="2026-11-06", po="PO 1013",
         lines=[L("Maintenance spares kit as per order (bearings, belts, grease)", "8482", 1, 35000)]),
    dict(file="12_not_yet_received_orbit.pdf", layout="classic", accent="#6a1b9a", vendor="V002",
         number="ORB/26-27/0433", date="2026-11-09", po="PO-1012",
         lines=[L("Dell 27-inch 4K monitor P2723QE", "8528", 30, 18000)]),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    for s in SAMPLES:
        data = render(s)
        if s.get("scanned"):
            data = scanify(data)
        with open(os.path.join(OUT, s["file"]), "wb") as f:
            f.write(data)
        print("wrote", s["file"])


if __name__ == "__main__":
    main()
