"""SQLite storage: master data (vendors, POs), the invoice ledger, and pipeline runs."""
import csv
import json
import os
import sqlite3
import threading
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("DATA_DIR", os.path.join(ROOT, "data"))
SEED = os.path.join(ROOT, "data", "seed")
DB_PATH = os.path.join(DATA, "ledgerline.db")
UPLOADS = os.path.join(DATA, "uploads")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
  vendor_id TEXT PRIMARY KEY, name TEXT, gstin TEXT, city TEXT, state TEXT,
  bank_name TEXT, bank_account TEXT, ifsc TEXT, email TEXT, status TEXT, status_note TEXT,
  payment_terms_days INTEGER);
CREATE TABLE IF NOT EXISTS purchase_orders (
  po_number TEXT PRIMARY KEY, vendor_id TEXT, po_date TEXT, status TEXT, gst_rate REAL, description TEXT,
  receipt_required INTEGER);
CREATE TABLE IF NOT EXISTS goods_receipts (
  grn_number TEXT, po_number TEXT, line_no INTEGER, received_qty REAL, grn_date TEXT);
CREATE TABLE IF NOT EXISTS po_lines (
  po_number TEXT, line_no INTEGER, item TEXT, qty REAL, unit_price REAL, invoiced_qty REAL,
  PRIMARY KEY (po_number, line_no));
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id TEXT, invoice_number TEXT, invoice_number_norm TEXT,
  invoice_date TEXT, po_number TEXT, subtotal REAL, tax REAL, total REAL, source TEXT, run_id TEXT,
  file_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, seq INTEGER, created_at TEXT, filename TEXT, source TEXT, file_path TEXT, file_hash TEXT,
  status TEXT, decision TEXT, vendor_name TEXT, vendor_id TEXT, invoice_number TEXT, total REAL,
  po_number TEXT, summary TEXT, stages TEXT, result TEXT, extraction_method TEXT, duration_ms INTEGER,
  review TEXT, audit TEXT, overrides TEXT, parent_run TEXT);
"""
SCHEMA_VERSION = 2


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(DATA, exist_ok=True)
        os.makedirs(UPLOADS, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        if _conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            # demo database: on schema change, rebuild from seed
            for t in ("vendors", "purchase_orders", "po_lines", "invoices", "runs", "goods_receipts"):
                _conn.execute(f"DROP TABLE IF EXISTS {t}")
            _conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _conn.executescript(SCHEMA)
        if not _conn.execute("SELECT 1 FROM vendors LIMIT 1").fetchone():
            load_seed()
    return _conn


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def x(sql, args=()):
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur


def norm_invoice_no(s: str | None) -> str:
    import re
    if not s:
        return ""
    n = re.sub(r"[^A-Z0-9]", "", s.upper())
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", n)


def load_seed(clear_runs: bool = False):
    with _lock:
        c = conn()
        for t in ["vendors", "purchase_orders", "po_lines", "invoices", "goods_receipts"] + (["runs"] if clear_runs else []):
            c.execute(f"DELETE FROM {t}")

        def rows(name):
            with open(os.path.join(SEED, name)) as f:
                return list(csv.DictReader(f))

        for r in rows("vendors.csv"):
            c.execute("INSERT INTO vendors VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (*[r[k] for k in ("vendor_id", "name", "gstin", "city", "state", "bank_name", "bank_account",
                                        "ifsc", "email", "status", "status_note")], int(r["payment_terms_days"])))
        for r in rows("purchase_orders.csv"):
            c.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?)",
                      (r["po_number"], r["vendor_id"], r["po_date"], r["status"], float(r["gst_rate"]), r["description"],
                       int(r["receipt_required"])))
        for r in rows("goods_receipts.csv"):
            c.execute("INSERT INTO goods_receipts VALUES (?,?,?,?,?)",
                      (r["grn_number"], r["po_number"], int(r["line_no"]), float(r["received_qty"]), r["grn_date"]))
        for r in rows("po_lines.csv"):
            c.execute("INSERT INTO po_lines VALUES (?,?,?,?,?,?)",
                      (r["po_number"], int(r["line_no"]), r["item"], float(r["qty"]), float(r["unit_price"]),
                       float(r["invoiced_qty"])))
        for r in rows("invoice_history.csv"):
            c.execute("INSERT INTO invoices (vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number,"
                      " subtotal, tax, total, source, run_id, file_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r["vendor_id"], r["invoice_number"], norm_invoice_no(r["invoice_number"]), r["invoice_date"],
                       r["po_number"], float(r["subtotal"]), float(r["tax"]), float(r["total"]), "history (seed)",
                       None, None, r["invoice_date"] + "T10:00:00"))
        c.commit()


def buyer() -> dict:
    with open(os.path.join(SEED, "buyer.json")) as f:
        return json.load(f)


# ------------------------------------------------------------------ master data
def vendors():
    return q("SELECT * FROM vendors ORDER BY vendor_id")


def po(po_number):
    p = q1("SELECT * FROM purchase_orders WHERE po_number=?", (po_number,))
    if p:
        p["lines"] = q("SELECT l.*, COALESCE((SELECT SUM(received_qty) FROM goods_receipts g WHERE g.po_number=l.po_number"
                       " AND g.line_no=l.line_no), 0) AS received_qty FROM po_lines l WHERE l.po_number=? ORDER BY line_no",
                       (po_number,))
        p["grns"] = q("SELECT grn_number, line_no, received_qty, grn_date FROM goods_receipts WHERE po_number=? "
                      "ORDER BY grn_date", (po_number,))
        p["value"] = sum(l["qty"] * l["unit_price"] for l in p["lines"])
        p["invoiced_value"] = sum(l["invoiced_qty"] * l["unit_price"] for l in p["lines"])
        p["remaining_value"] = sum(max(0, l["qty"] - l["invoiced_qty"]) * l["unit_price"] for l in p["lines"])
    return p


def pos_for_vendor(vendor_id):
    return [po(r["po_number"]) for r in q("SELECT po_number FROM purchase_orders WHERE vendor_id=?", (vendor_id,))]


def ledger():
    out = []
    for r in q("SELECT p.po_number, v.name vendor_name FROM purchase_orders p JOIN vendors v USING(vendor_id) "
               "ORDER BY p.po_number"):
        p = po(r["po_number"])
        p["vendor_name"] = r["vendor_name"]
        p["invoices"] = q("SELECT invoice_number, invoice_date, subtotal, total, source, run_id FROM invoices "
                          "WHERE po_number=? ORDER BY created_at", (r["po_number"],))
        out.append(p)
    return out


def post_invoice(vendor_id, invoice_number, invoice_date, po_number, subtotal, tax, total, run_id, file_hash,
                 line_alloc):
    """Record an approved invoice and consume PO quantities."""
    with _lock:
        x("INSERT INTO invoices (vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number, subtotal,"
          " tax, total, source, run_id, file_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
          (vendor_id, invoice_number, norm_invoice_no(invoice_number), invoice_date, po_number, subtotal, tax, total,
           "pipeline", run_id, file_hash, datetime.now().isoformat(timespec="seconds")))
        for a in line_alloc:
            x("UPDATE po_lines SET invoiced_qty = invoiced_qty + ? WHERE po_number=? AND line_no=?",
              (a["qty"], po_number, a["line_no"]))


# ------------------------------------------------------------------ runs
def new_run(filename, source, file_path, file_hash, status="queued"):
    with _lock:
        # run numbers are never reused (not even after a reset), so a stale job can't touch a new run
        last = max((q1("SELECT v FROM meta WHERE k='run_seq'") or {"v": 0})["v"], q1("SELECT MAX(seq) m FROM runs")["m"] or 0)
        seq = last + 1
        x("INSERT OR REPLACE INTO meta (k, v) VALUES ('run_seq', ?)", (seq,))
        rid = f"RUN-{seq:04d}"
        x("INSERT INTO runs (id, seq, created_at, filename, source, file_path, file_hash, status, stages, audit)"
          " VALUES (?,?,?,?,?,?,?,?,?,?)",
          (rid, seq, datetime.now().isoformat(timespec="seconds"), filename, source, file_path, file_hash,
           status, "[]", "[]"))
        return rid


def claim(rid) -> bool:
    """Atomically move a run from queued to running. False if it was already taken or no longer exists."""
    with _lock:
        return x("UPDATE runs SET status='running' WHERE id=? AND status='queued'", (rid,)).rowcount == 1


def update_run(rid, **fields):
    if not fields:
        return
    for k in ("stages", "result", "review", "audit", "overrides"):
        if k in fields and fields[k] is not None and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], default=str)
    cols = ", ".join(f"{k}=?" for k in fields)
    x(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), rid))


def get_run(rid):
    r = q1("SELECT * FROM runs WHERE id=?", (rid,))
    if r:
        for k in ("stages", "result", "review", "audit", "overrides"):
            r[k] = json.loads(r[k]) if r.get(k) else None
    return r


def list_runs():
    return q("SELECT id, created_at, filename, source, status, decision, vendor_name, invoice_number, total, "
             "po_number, summary, extraction_method, duration_ms, review, parent_run FROM runs ORDER BY seq DESC")
