#!/usr/bin/env python3
# Unit tests for recombine.py (S7 RECOMBINE / Executor). Pure data orchestration -> NO live browser, NO
# import of the other in-flight stage modules; everything below is a fixture (inline segments.json /
# plan.json shapes per CONTRACTS) or a stub region runner. Runs with plain `python` or under pytest.
#
# The central fixture is a UI -> API -> COMPREHEND workflow:
#   u0  UiRegion(NAVIGATE)  produces r0 (the invoice id surfaced by navigating)         -> PRIOR_UI
#   s0  ApiSegment(API)     consumes r0, produces r1 (a PDF, binary)                     -> PRIOR_SEGMENT
#   u1  UiRegion(COMPREHEND) consumes r1, produces r2 (a summary string the reader emits) -> PRIOR_UI
# It exercises every recombine job: ordering, the shape fail-fast, run-scope threading by ValueRef, and
# the workflow-level INV-1 re-check (r1 is un-sourced inside u1 but declared upstream by s0 -> INPUT).

import sys

import recombine as rc

# ---- fixtures --------------------------------------------------------------------------------------
def _ui_api_comprehend_segments() -> dict:
    # u0 (UI nav) -> s0 (API) -> u1 (COMPREHEND). Mirrors segments.json (CONTRACTS §1) shape.
    return {
        "schema": "segments/v1",
        "step": "steps/summarize-invoice.md",
        "grounded_against": ".o11y/run",
        "regions": [
            {
                "kind": "UiRegion",
                "id": "u0",
                "nature": "NAVIGATE",
                "actions": [{"i": 0, "text": "Open and navigate to the invoice", "nature": "NAVIGATE"}],
                "consumes": [],
                "produces": [
                    {"ref": "r0", "shape": {"type": "string"}, "extractor": "json-ptr:/invoice_id", "origin": "PRIOR_UI"}
                ],
            },
            {
                "kind": "ApiSegment",
                "id": "s0",
                "actions": [{"i": 1, "text": "Export as PDF", "nature": "DATA_WORK", "produces": ["r1"], "consumes": ["r0"]}],
                "consumes": [
                    {"ref": "r0", "shape": {"type": "string"}, "extractor": "json-ptr:/invoice_id", "origin": "PRIOR_UI"}
                ],
                "produces": [
                    {"ref": "r1", "shape": {"type": "binary", "tag": "pdf"}, "extractor": "whole-payload", "origin": "PRIOR_SEGMENT"}
                ],
            },
            {
                "kind": "UiRegion",
                "id": "u1",
                "nature": "COMPREHEND",
                "actions": [{"i": 2, "text": "Read the PDF and summarize", "nature": "COMPREHEND"}],
                "consumes": [
                    {"ref": "r1", "shape": {"type": "binary", "tag": "pdf"}, "extractor": "whole-payload", "origin": "PRIOR_SEGMENT"}
                ],
                "produces": [
                    {"ref": "r2", "shape": {"type": "string"}, "extractor": "whole-payload", "origin": "PRIOR_UI"}
                ],
            },
        ],
        "handoffs": [
            {"ref": "r0", "shape": {"type": "string"}, "extractor": "json-ptr:/invoice_id", "origin": "PRIOR_UI", "from": None, "to": "s0"},
            {"ref": "r1", "shape": {"type": "binary", "tag": "pdf"}, "extractor": "whole-payload", "origin": "PRIOR_SEGMENT", "from": "s0", "to": "u1"},
            {"ref": "r2", "shape": {"type": "string"}, "extractor": "whole-payload", "origin": "PRIOR_UI", "from": "u1", "to": None},
        ],
        "segment_ids": ["s0"],
        "bail": None,
    }


def _api_candidate_plan() -> dict:
    # Minimal plan.json (CONTRACTS §3) for s0 that PASSED its gates -> recombine treats it as API.
    return {
        "schema": "plan/v1",
        "segment_id": "s0",
        "unexplained": [],
        "contested": [],
        "dangling_produced": [],
        "verdict": "API-CANDIDATE",
        "bail": None,
    }


def _runners(record: list[str]) -> dict:
    # Stub region runners — NO live calls. UiRegion fabricates its produces; ApiSegment fabricates a PDF
    # as bytes. Each records the order it ran so we can assert workflow ordering.
    def ui(region: "rc.RegionPlan", supplied: dict) -> dict:
        record.append(region.id)
        out: dict = {}
        for h in region.produces:
            if region.id == "u0":
                out[h.ref] = "inv_001"  # the navigated-to invoice id
            else:
                out[h.ref] = "summary: paid in full"  # the COMPREHEND reader's output string
        return out

    def api(region: "rc.RegionPlan", supplied: dict) -> dict:
        record.append(region.id)
        assert supplied.get("r0") == "inv_001", supplied  # the upstream UI value threaded in
        return {h.ref: b"%PDF-1.7\nfake pdf bytes\n" for h in region.produces}

    return {"UiRegion": ui, "ApiSegment": api}


# ---- shape_ok --------------------------------------------------------------------------------------
def test_shape_ok_string_matches():
    ok, why = rc.shape_ok("inv_001", {"type": "string"})
    assert ok and why == "", why

def test_shape_ok_binary_accepts_bytes():
    ok, _ = rc.shape_ok(b"%PDF-1.7", {"type": "binary", "tag": "pdf"})
    assert ok

def test_shape_ok_binary_accepts_path_string():
    # a runner may hand back a path/str sidecar instead of raw bytes for an opaque artifact
    ok, _ = rc.shape_ok("/tmp/out.pdf", {"type": "binary", "tag": "pdf"})
    assert ok

def test_shape_ok_string_rejects_number():
    ok, why = rc.shape_ok(42, {"type": "string"})
    assert not ok and "expected string" in why

def test_shape_ok_bool_is_not_number():
    # bool must NOT satisfy a number shape (Python bool subclasses int — the classifier must not be fooled)
    ok, _ = rc.shape_ok(True, {"type": "number"})
    assert not ok

def test_shape_ok_undeclared_type_is_free():
    ok, _ = rc.shape_ok({"anything": 1}, {})
    assert ok

def test_shape_ok_array_matches():
    ok, _ = rc.shape_ok([1, 2, 3], {"type": "array"})
    assert ok


# ---- build_plan ------------------------------------------------------------------------------------
def test_build_plan_orders_regions():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    assert [r.id for r in plan.regions] == ["u0", "s0", "u1"]

def test_build_plan_api_segment_with_passing_plan_is_api():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    s0 = next(r for r in plan.regions if r.id == "s0")
    assert s0.verdict == "API"

def test_build_plan_api_segment_without_plan_keeps_ui():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {})  # no plan supplied for s0
    s0 = next(r for r in plan.regions if r.id == "s0")
    assert s0.verdict == "UI"

def test_build_plan_keep_ui_plan_keeps_ui():
    kept = {**_api_candidate_plan(), "verdict": "KEEP-UI"}
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": kept})
    assert next(r for r in plan.regions if r.id == "s0").verdict == "UI"

def test_build_plan_bailed_plan_keeps_ui():
    bailed = {**_api_candidate_plan(), "bail": {"code": "BAIL-2", "reason": "unexplained value"}}
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": bailed})
    assert next(r for r in plan.regions if r.id == "s0").verdict == "UI"

def test_build_plan_ui_region_always_ui():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    assert all(r.verdict == "UI" for r in plan.regions if r.kind == "UiRegion")

def test_build_plan_workflow_edges():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    assert plan.workflow_inputs == ["r0"]
    assert plan.workflow_outputs == ["r2"]


# ---- workflow-level INV-1 (the re-check) -----------------------------------------------------------
def test_inv1_passes_when_all_consumes_sourced_upstream():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    inv1 = rc.check_inv1(plan)
    assert inv1["pass"], inv1

def test_inv1_reclassifies_upstream_produced_as_input():
    # r1 is consumed by u1 but never sourced WITHIN u1; s0 declares it upstream -> INPUT, not UNEXPLAINED.
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    inv1 = rc.check_inv1(plan)
    refs = {(e["region"], e["ref"]) for e in inv1["reclassified_as_input"]}
    assert ("u1", "r1") in refs, inv1["reclassified_as_input"]

def test_inv1_fails_on_truly_unsourced_consume():
    segs = _ui_api_comprehend_segments()
    # u1 now also consumes r9, produced by NO region and not a workflow input -> genuinely UNEXPLAINED.
    segs["regions"][2]["consumes"].append(
        {"ref": "r9", "shape": {"type": "string"}, "extractor": "json-ptr:/x", "origin": "PRIOR_SEGMENT"}
    )
    plan = rc.build_plan(segs, {"s0": _api_candidate_plan()})
    inv1 = rc.check_inv1(plan)
    assert not inv1["pass"]
    assert any(e["ref"] == "r9" for e in inv1["unexplained"]), inv1

def test_inv1_step_input_is_not_unexplained():
    # a STEP_INPUT consume (the workflow's own input) is sourced at the edge, never UNEXPLAINED
    segs = _ui_api_comprehend_segments()
    segs["regions"][0]["consumes"].append(
        {"ref": "rIn", "shape": {"type": "string"}, "extractor": "json-ptr:/seed", "origin": "STEP_INPUT"}
    )
    plan = rc.build_plan(segs, {"s0": _api_candidate_plan()})
    assert rc.check_inv1(plan)["pass"]


# ---- execute (pure orchestration; stub runners) ----------------------------------------------------
def test_execute_runs_regions_in_order():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    record: list[str] = []
    rc.execute(plan, _runners(record))
    assert record == ["u0", "s0", "u1"]

def test_execute_threads_scope_by_valueref():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    out = rc.execute(plan, _runners([]))
    # r0 from u0 fed into s0 (asserted inside the api runner); final scope carries r0,r1,r2
    assert out["scope"]["r0"] == "inv_001"
    assert out["scope"]["r1"].startswith(b"%PDF-")
    assert out["scope"]["r2"] == "summary: paid in full"

def test_execute_returns_only_workflow_outputs():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    out = rc.execute(plan, _runners([]))
    assert set(out["outputs"]) == {"r2"}  # to==null leaf only, not the intermediate r0/r1

def test_execute_fail_fast_on_wrong_shape():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})

    def bad_api(region: "rc.RegionPlan", supplied: dict) -> dict:
        return {h.ref: 12345 for h in region.produces}  # int where a binary PDF was declared

    runners = {**_runners([]), "ApiSegment": bad_api}
    try:
        rc.execute(plan, runners)
    except rc.ShapeViolation as e:
        assert "r1" in str(e) and "wrong shape" in str(e)
    else:
        raise AssertionError("expected ShapeViolation on a wrong-shape handoff")

def test_execute_fail_fast_on_missing_produce():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})

    def empty_api(region: "rc.RegionPlan", supplied: dict) -> dict:
        return {}  # declared r1 but produced nothing

    try:
        rc.execute(plan, {**_runners([]), "ApiSegment": empty_api})
    except rc.ShapeViolation as e:
        assert "did not produce" in str(e)
    else:
        raise AssertionError("expected ShapeViolation when a declared produce is missing")

def test_execute_raises_on_unbound_consume():
    segs = _ui_api_comprehend_segments()
    segs["regions"][2]["consumes"].append(
        {"ref": "r9", "shape": {"type": "string"}, "extractor": "json-ptr:/x", "origin": "PRIOR_SEGMENT"}
    )
    plan = rc.build_plan(segs, {"s0": _api_candidate_plan()})
    try:
        rc.execute(plan, _runners([]))
    except rc.UnboundConsume:
        pass
    else:
        raise AssertionError("expected UnboundConsume when INV-1 fails at the workflow level")

def test_execute_missing_runner_raises():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    try:
        rc.execute(plan, {"UiRegion": _runners([])["UiRegion"]})  # no ApiSegment runner
    except KeyError as e:
        assert "ApiSegment" in str(e)
    else:
        raise AssertionError("expected KeyError for an unregistered region kind")


# ---- serialization ---------------------------------------------------------------------------------
def test_plan_to_json_roundtrips_ids_and_verdicts():
    plan = rc.build_plan(_ui_api_comprehend_segments(), {"s0": _api_candidate_plan()})
    plan.inv1 = rc.check_inv1(plan)
    out = rc.plan_to_json(plan)
    assert out["schema"] == "recombine/v1"
    assert [r["id"] for r in out["regions"]] == ["u0", "s0", "u1"]
    assert {r["id"]: r["verdict"] for r in out["regions"]}["s0"] == "API"
    assert out["workflow_outputs"] == ["r2"]
    assert out["inv1"]["pass"] is True


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'} ({len(tests)} tests)")
    sys.exit(1 if failed else 0)
