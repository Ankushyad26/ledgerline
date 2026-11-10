"""Ledgerline — AP invoice agent. FastAPI server: API + static single-page UI."""
import json
import os
import shutil
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, llm, pipeline
from .rules import RULES, duplicate_rules, rulebook

ROOT = db.ROOT
SAMPLES = os.path.join(ROOT, "samples")
STATIC = os.path.join(ROOT, "static")

SAMPLE_META = {
    "01_happy_path_nimbus.pdf": ("Happy path", "Clean digital invoice, exact PO match", "APPROVED", "happy"),
    "02_split_po_part2_orbit.pdf": ("Split PO · part 2", "8 of 25 laptops; 12 already billed earlier", "APPROVED", "edge"),
    "03_split_po_overrun_orbit.pdf": ("Split PO · over-billing", "7 more laptops — only 5 left open (run after #2)", "ON_HOLD", "edge"),
    "04_within_tolerance_shakti.pdf": ("Price within tolerance", "Bracket billed at ₹45.40 vs ₹45 on PO", "APPROVED", "edge"),
    "05_price_variance_shakti.pdf": ("Price over tolerance", "Stretch film billed 8% above PO price", "ON_HOLD", "edge"),
    "06_scanned_no_po_greenleaf.pdf": ("Scanned · no PO number", "Phone-scan; PO must be inferred", "ON_HOLD", "edge"),
    "07_duplicate_resubmission_apex.pdf": ("Disguised duplicate", "INV-0042 resubmitted — INV42 already paid", "REJECTED", "edge"),
    "08_bank_details_changed_nimbus.pdf": ("Bank details changed", "Invoice asks to pay a new bank account", "ON_HOLD", "edge"),
    "09_tax_inclusive_apex.pdf": ("Tax-inclusive pricing", "GST embedded in the rate, different layout", "APPROVED", "edge"),
    "10_missing_fields_shakti.pdf": ("Missing invoice no. & date", "Vendor forgot mandatory fields", "ON_HOLD", "edge"),
    "11_blocked_vendor_vertex.pdf": ("Blocked vendor", "Vendor failed compliance audit", "REJECTED", "edge"),
    "12_not_yet_received_orbit.pdf": ("Billed before delivery", "30 monitors billed, stores received only 18", "ON_HOLD", "edge"),
    "13_bundled_lines_shakti.pdf": ("Bundled lines", "One lump-sum line for a 3-line PO", "APPROVED", "edge"),
}

app = FastAPI(title="Ledgerline AP Agent")


@app.on_event("startup")
def _startup():
    db.conn()


def _start(path, filename, source):
    h = pipeline.file_hash(path)
    rid = db.new_run(filename, source, path, h)
    pipeline.enqueue(rid)   # single worker, strict arrival order
    return {"run_id": rid}


@app.get("/api/samples")
def samples():
    out = []
    for fn in sorted(os.listdir(SAMPLES)):
        if fn.lower().endswith(".pdf"):
            t, d, exp, kind = SAMPLE_META.get(fn, (fn, "", None, "edge"))
            out.append({"file": fn, "title": t, "description": d, "expected": exp, "kind": kind})
    return out


@app.get("/api/samples/{name}")
def sample_file(name: str):
    p = os.path.join(SAMPLES, os.path.basename(name))
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


class SampleRun(BaseModel):
    file: str


@app.post("/api/runs/sample")
def run_sample(body: SampleRun):
    src = os.path.join(SAMPLES, os.path.basename(body.file))
    if not os.path.exists(src):
        raise HTTPException(404, "sample not found")
    dst = os.path.join(db.UPLOADS, f"{uuid.uuid4().hex}_{os.path.basename(body.file)}")
    shutil.copy(src, dst)
    return _start(dst, body.file, "sample library")


async def _save(file: UploadFile):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, f"{file.filename}: upload a PDF or an image (PNG/JPG)")
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, f"{file.filename}: file too large (max 15 MB)")
    dst = os.path.join(db.UPLOADS, f"{uuid.uuid4().hex}_{os.path.basename(file.filename)}")
    with open(dst, "wb") as fh:
        fh.write(data)
    return dst


@app.post("/api/runs/upload")
async def run_upload(file: UploadFile = File(...)):
    dst = await _save(file)
    return _start(dst, file.filename, "upload")


@app.post("/api/runs/batch")
async def run_batch(files: list[UploadFile] = File(...)):
    """Batch intake: every file is queued and processed in the order received."""
    if not files or len(files) > 50:
        raise HTTPException(400, "Send between 1 and 50 files")
    saved = [(await _save(f), f.filename) for f in files]   # validate all before queuing any
    batch = uuid.uuid4().hex[:6].upper()
    ids = [_start(p, fn, f"batch {batch} ({i}/{len(saved)})")["run_id"] for i, (p, fn) in enumerate(saved, 1)]
    return {"batch": batch, "run_ids": ids}


class SampleBatch(BaseModel):
    files: list[str]


@app.post("/api/runs/sample-batch")
def run_sample_batch(body: SampleBatch):
    ids = []
    for fn in body.files:
        src = os.path.join(SAMPLES, os.path.basename(fn))
        if os.path.exists(src):
            dst = os.path.join(db.UPLOADS, f"{uuid.uuid4().hex}_{os.path.basename(fn)}")
            shutil.copy(src, dst)
            ids.append(_start(dst, fn, "sample library (batch)")["run_id"])
    return {"run_ids": ids}


@app.get("/api/runs")
def runs():
    return db.list_runs()


@app.get("/api/runs/{rid}")
def run(rid: str):
    r = db.get_run(rid)
    if not r:
        raise HTTPException(404)
    r.pop("file_path", None)
    r["queue_position"] = pipeline.queue_position(rid) if r["status"] == "queued" else None
    return r


class Correction(BaseModel):
    fields: dict
    line_items: list[dict] | None = None
    note: str | None = None
    reviewer: str | None = "AP reviewer"


@app.post("/api/runs/{rid}/correct")
def do_correct(rid: str, body: Correction):
    try:
        nrid = pipeline.correct(rid, body.fields, body.line_items, body.note, body.reviewer or "AP reviewer")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"run_id": nrid}


@app.post("/api/runs/{rid}/rerun")
def do_rerun(rid: str):
    try:
        return {"run_id": pipeline.rerun(rid)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/editable-fields")
def editable_fields():
    return [{"key": k, "label": lab} for k, lab in pipeline.EDITABLE]


@app.get("/api/runs/{rid}/file")
def run_file(rid: str):
    r = db.get_run(rid)
    if not r:
        raise HTTPException(404)
    ext = os.path.splitext(r["file_path"])[1].lower()
    mt = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
          "webp": "image/webp"}.get(ext[1:], "application/octet-stream")
    return FileResponse(r["file_path"], media_type=mt)


@app.get("/api/runs/{rid}/preview.png")
def run_preview(rid: str):
    r = db.get_run(rid)
    if not r:
        raise HTTPException(404)
    out = r["file_path"] + ".preview.png"
    if not os.path.exists(out):
        ext = os.path.splitext(r["file_path"])[1].lower()
        if ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(r["file_path"]) as pdf:
                pdf.pages[0].to_image(resolution=110).original.save(out)
        else:
            from PIL import Image
            im = Image.open(r["file_path"])
            im.thumbnail((1400, 1800))
            im.save(out)
    return FileResponse(out, media_type="image/png")


class Review(BaseModel):
    action: str
    note: str | None = None
    po_number: str | None = None
    reviewer: str | None = "AP reviewer"


@app.post("/api/runs/{rid}/review")
def do_review(rid: str, body: Review):
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    try:
        r = pipeline.review(rid, body.action, body.note, body.po_number, body.reviewer or "AP reviewer")
    except ValueError as e:
        raise HTTPException(400, str(e))
    r.pop("file_path", None)
    return r


@app.get("/api/stats")
def stats():
    rs = [r for r in db.list_runs() if r["status"] == "done"]
    done = [r for r in rs if r["decision"] in ("APPROVED", "ON_HOLD", "REJECTED")]
    count = lambda d: sum(1 for r in done if r["decision"] == d)  # noqa: E731
    auto = sum(1 for r in done if r["decision"] == "APPROVED" and not r["review"] and not r["parent_run"])
    durs = [r["duration_ms"] for r in done if r["duration_ms"]]
    reasons, risky = {}, []
    risk_codes = {"BANK_MISMATCH", "DUPLICATE", "DUPLICATE_FILE", "NEAR_DUPLICATE", "VENDOR_BLOCKED", "UNKNOWN_VENDOR",
                  "GSTIN_MISMATCH"}
    for r in db.q("SELECT total, result FROM runs WHERE result IS NOT NULL"):
        codes = {c["code"] for c in json.loads(r["result"]).get("checks", []) if c["severity"] in ("hold", "reject")}
        for c in codes:
            reasons[c] = reasons.get(c, 0) + 1
        if codes & risk_codes:
            risky.append(r)
    return {
        "total": len(done), "approved": count("APPROVED"), "on_hold": count("ON_HOLD"),
        "rejected": count("REJECTED"), "auto_approved": auto,
        "straight_through_rate": round(auto / len(done) * 100) if done else 0,
        "avg_seconds": round(sum(durs) / len(durs) / 1000, 3) if durs else 0,
        "value_processed": round(sum(r["total"] or 0 for r in done), 2),
        "value_approved": round(sum(r["total"] or 0 for r in done if r["decision"] == "APPROVED"), 2),
        "value_blocked": round(sum(r["total"] or 0 for r in done if r["decision"] == "REJECTED"), 2),
        "reviewed": sum(1 for r in done if r["review"] or r["parent_run"]),
        "value_risk": round(sum(r["total"] or 0 for r in risky), 2),
        "top_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:6],
    }


@app.get("/api/ledger")
def ledger():
    return db.ledger()


@app.get("/api/vendors")
def vendors():
    return db.vendors()


@app.get("/api/config")
def config():
    return {"rules": RULES, "rulebook": rulebook(), "duplicate_rules": duplicate_rules(), "llm": llm.describe(),
            "buyer": db.buyer()}


@app.post("/api/reset")
def reset():
    db.load_seed(clear_runs=True)
    for fn in os.listdir(db.UPLOADS):
        try:
            os.remove(os.path.join(db.UPLOADS, fn))
        except OSError:
            pass
    return {"ok": True}


@app.exception_handler(Exception)
async def _err(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=500)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
