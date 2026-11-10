"""End-to-end: every demo invoice must produce its designed decision.  Run:  python -m pytest -q"""
import os
import shutil
import time
import uuid

os.environ["STAGE_DELAY"] = "0"
os.environ["LLM_PROVIDER"] = "none"   # deterministic offline readers for CI
os.environ.setdefault("TODAY", "2026-11-10")
os.environ["DATA_DIR"] = "/tmp/ledgerline-test"

from app import db, pipeline  # noqa: E402

EXPECTED = [  # order matters: split-PO overrun depends on part 2 being posted first
    ("01_happy_path_nimbus.pdf", "APPROVED", "THREE_WAY_OK"),
    ("02_split_po_part2_orbit.pdf", "APPROVED", None),
    ("03_split_po_overrun_orbit.pdf", "ON_HOLD", "QTY_EXCEEDS_OPEN"),
    ("04_within_tolerance_shakti.pdf", "APPROVED", "PRICE_WITHIN_TOL"),
    ("05_price_variance_shakti.pdf", "ON_HOLD", "PRICE_VARIANCE"),
    ("06_scanned_no_po_greenleaf.pdf", "ON_HOLD", "PO_INFERRED"),
    ("07_duplicate_resubmission_apex.pdf", "REJECTED", "DUPLICATE"),
    ("08_bank_details_changed_nimbus.pdf", "ON_HOLD", "BANK_MISMATCH"),
    ("09_tax_inclusive_apex.pdf", "APPROVED", "TERMS_SHORTER"),
    ("10_missing_fields_shakti.pdf", "ON_HOLD", "MISSING_FIELDS"),
    ("11_blocked_vendor_vertex.pdf", "REJECTED", "VENDOR_BLOCKED"),
    ("12_not_yet_received_orbit.pdf", "ON_HOLD", "GOODS_NOT_RECEIVED"),
    ("13_bundled_lines_shakti.pdf", "APPROVED", "BUNDLED"),
]
SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")


def _new(fn):
    dst = os.path.join(db.UPLOADS, uuid.uuid4().hex + "_" + fn)
    shutil.copy(os.path.join(SAMPLES, fn), dst)
    return db.new_run(fn, "test", dst, pipeline.file_hash(dst))


def _run(fn):
    rid = _new(fn)
    pipeline.run_pipeline(rid)
    return db.get_run(rid)


def _wait(rid, timeout=30):
    t = time.time()
    while db.get_run(rid)["status"] != "done":
        assert time.time() - t < timeout, f"{rid} did not finish"
        time.sleep(0.1)
    return db.get_run(rid)


def test_all_scenarios():
    db.conn()
    db.load_seed(clear_runs=True)
    for fn, decision, code in EXPECTED:
        r = _run(fn)
        assert r["status"] == "done", fn
        assert r["decision"] == decision, f"{fn}: {r['decision']} - {r['summary']}"
        if code:
            assert code in {c["code"] for c in r["result"]["checks"]}, fn
        assert r["result"]["fields"].get("field_confidence"), fn


def test_resubmitting_an_approved_invoice_is_rejected():
    db.load_seed(clear_runs=True)
    assert _run("01_happy_path_nimbus.pdf")["decision"] == "APPROVED"
    assert _run("01_happy_path_nimbus.pdf")["decision"] == "REJECTED"


def test_human_review_confirms_inferred_po_and_updates_ledger():
    db.load_seed(clear_runs=True)
    r = _run("06_scanned_no_po_greenleaf.pdf")
    assert r["decision"] == "ON_HOLD" and r["result"]["po_inferred"]
    r = pipeline.review(r["id"], "approve", "confirmed with facilities", "PO-1005")
    assert r["decision"] == "APPROVED"
    assert db.po("PO-1005")["remaining_value"] == 0


def test_reviewer_correction_reruns_all_checks():
    db.load_seed(clear_runs=True)
    r = _run("10_missing_fields_shakti.pdf")
    nrid = pipeline.correct(r["id"], {"invoice_number": "SIC/1201", "invoice_date": "2026-11-08"}, None,
                            "vendor confirmed by phone")
    n = _wait(nrid)
    assert n["decision"] == "APPROVED" and n["parent_run"] == r["id"]
    old = db.get_run(r["id"])
    assert old["decision"] == "SUPERSEDED" and len(old["review"]["changes"]) == 2


def test_batch_queue_processes_in_order():
    """Two invoices on the same PO queued together must not both pass against the same open balance."""
    db.load_seed(clear_runs=True)
    a, b = _new("02_split_po_part2_orbit.pdf"), _new("03_split_po_overrun_orbit.pdf")
    pipeline.enqueue(a)
    pipeline.enqueue(b)
    assert _wait(a)["decision"] == "APPROVED"
    assert _wait(b)["decision"] == "ON_HOLD"


def test_payment_due_uses_agreed_terms_not_invoice_terms():
    db.load_seed(clear_runs=True)
    r = _run("09_tax_inclusive_apex.pdf")          # invoice says "Due on receipt", agreed Net 30
    assert r["result"]["due_date"] == "2026-12-02"


def test_thresholds_come_from_policy_dataset():
    import json
    from app.rules import RULES, duplicate_rules
    pol = json.load(open(os.path.join(os.path.dirname(__file__), "..", "data", "seed", "policy.json")))
    assert RULES["price_tolerance_pct"] == pol["price_tolerance_pct"]
    assert len(duplicate_rules()) == 3


def test_every_decision_has_a_step_by_step_trace():
    db.load_seed(clear_runs=True)
    r = _run("05_price_variance_shakti.pdf")
    assert len(r["result"]["trace"]) == 8 and r["summary"].startswith("Flagged for review")


def test_ai_path_with_retry_and_backup_model(monkeypatch):
    """AI reader path end-to-end (mocked HTTP): busy -> retry -> backup model -> correct decision."""
    import httpx
    from app import llm
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr("time.sleep", lambda s: None)
    good = {"vendor_name": "Orbit Systems India Pvt Ltd", "vendor_gstin": "29AACCO5678K1ZR", "invoice_number": "ORB/26-27/0433",
            "invoice_date": "2026-11-09", "po_reference": "PO-1012", "line_items": [{"description": "Dell 27-inch 4K monitor P2723QE",
            "quantity": 30, "unit_price": 18000, "amount": 540000}], "subtotal": 540000,
            "tax_lines": [{"type": "IGST", "rate": 18, "amount": 97200}], "tax_total": 97200, "total": 637200,
            "amounts_include_tax": False, "payment_terms": "Net 45 days", "notes": [], "confidence": {"total": 0.99}}
    calls = []

    def fake_call(p, model, b, mime, prompt):
        calls.append(model)
        if model == llm.MODELS["gemini"]:
            req = httpx.Request("POST", "https://x")
            raise httpx.HTTPStatusError("busy", request=req, response=httpx.Response(503, request=req, text="busy"))
        return good
    monkeypatch.setattr(llm, "_call", fake_call)
    db.load_seed(clear_runs=True)
    r = _run("12_not_yet_received_orbit.pdf")
    assert r["extraction_method"].startswith("AI vision")
    assert r["decision"] == "ON_HOLD" and "GOODS_NOT_RECEIVED" in {c["code"] for c in r["result"]["checks"]}
    assert calls[-1] != llm.MODELS["gemini"]          # succeeded on a backup model after retries
