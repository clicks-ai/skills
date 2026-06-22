#!/usr/bin/env python3
# Tests for capture_cdp.py pure logic: sibling run-dir selection, binary-body recording (no silent drop),
# json-pointer extraction, and segment_inputs.json binding. Runs with plain `python` (no pytest) or under
# pytest. The live CDP/browser path is covered by the integration gate — these use fixtures only.
import base64
import json
import os
import sys
import tempfile

import capture_cdp as c


# ---- resolve_run_dir (varied-input runs into sibling dirs) ----

def test_resolve_no_label_is_identity() -> None:
    assert c.resolve_run_dir("/tmp/x/run", None) == "/tmp/x/run"

def test_resolve_label_first_free_is_label() -> None:
    with tempfile.TemporaryDirectory() as d:
        assert c.resolve_run_dir(d, "run") == os.path.join(d, "run")

def test_resolve_label_skips_recorded_dirs() -> None:
    with tempfile.TemporaryDirectory() as d:
        # `run` already holds a recorded trace -> next free is `run2`
        net = os.path.join(d, "run", "cdp", "network")
        os.makedirs(net)
        open(os.path.join(net, "requests.jsonl"), "w").write("")
        assert c.resolve_run_dir(d, "run") == os.path.join(d, "run2")

def test_resolve_label_skips_two_recorded_dirs() -> None:
    with tempfile.TemporaryDirectory() as d:
        for name in ("run", "run2"):
            net = os.path.join(d, name, "cdp", "network")
            os.makedirs(net)
            open(os.path.join(net, "requests.jsonl"), "w").write("")
        assert c.resolve_run_dir(d, "run") == os.path.join(d, "run3")

def test_resolve_label_in_progress_dir_reused() -> None:
    # a --start created the dir tree but no trace yet -> --stop must reuse the SAME dir, not bump to run2
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "run", "cdp"))  # no requests.jsonl -> still "free"
        assert c.resolve_run_dir(d, "run") == os.path.join(d, "run")


# ---- _content_type ----

def test_content_type_case_insensitive() -> None:
    assert c._content_type({"Content-Type": "application/pdf"}) == "application/pdf"
    assert c._content_type({"content-type": "image/png"}) == "image/png"

def test_content_type_absent_is_none() -> None:
    assert c._content_type({"x-other": "1"}) is None
    assert c._content_type(None) is None


# ---- binary_body_record (CONTRACTS: never silently drop a binary/streamed body) ----

def test_binary_record_keeps_type_size_magic() -> None:
    raw = b"%PDF-1.7\nbinary tail \x00\x01\x02"
    rec = c.binary_body_record("rid1", base64.b64encode(raw).decode(), "application/pdf")
    assert rec["binary"] is True
    assert rec["contentType"] == "application/pdf"
    assert rec["bytes"] == len(raw)
    assert rec["magic"] == "%PDF-1.7"  # first 8 bytes
    assert rec["bodyBase64"] == base64.b64encode(raw).decode()

def test_binary_record_png_magic() -> None:
    raw = b"\x89PNG\r\n\x1a\n" + b"rest"
    rec = c.binary_body_record("r", base64.b64encode(raw).decode(), "image/png")
    assert rec["magic"].startswith("\x89PNG")
    assert rec["bytes"] == len(raw)

def test_binary_record_bad_base64_is_empty_not_crash() -> None:
    rec = c.binary_body_record("r", "!!!not base64!!!", "application/octet-stream")
    assert rec["bytes"] == 0 and rec["magic"] == "" and rec["binary"] is True

def test_binary_record_unknown_content_type_is_none() -> None:
    rec = c.binary_body_record("r", base64.b64encode(b"zzzz").decode(), None)
    assert rec["contentType"] is None and rec["bytes"] == 4


# ---- json-pointer extraction ----

def test_parse_json_ptr_basic() -> None:
    assert c.parse_json_ptr("/a/b/c") == ["a", "b", "c"]
    assert c.parse_json_ptr("") == []
    assert c.parse_json_ptr("/x") == ["x"]

def test_parse_json_ptr_escapes() -> None:
    assert c.parse_json_ptr("/a~1b/c~0d") == ["a/b", "c~d"]

def test_apply_json_ptr_nested() -> None:
    obj = {"data": {"applyTemplate": {"jobId": "job_7f3a"}}}
    assert c.apply_json_ptr(obj, "/data/applyTemplate/jobId") == "job_7f3a"

def test_apply_json_ptr_array_index() -> None:
    obj = {"items": [{"id": "a"}, {"id": "b"}]}
    assert c.apply_json_ptr(obj, "/items/1/id") == "b"

def test_apply_json_ptr_missing_is_none() -> None:
    assert c.apply_json_ptr({"a": 1}, "/b") is None
    assert c.apply_json_ptr({"a": 1}, "/a/b") is None
    assert c.apply_json_ptr({"items": []}, "/items/5") is None


# ---- extract_value (PRIOR_SEGMENT/PRIOR_UI read from the trace) ----

def _paired() -> list[dict]:
    return [
        {"respBody": {"token": "csrf_abc"}, "respHeaders": {"Content-Type": "application/json"}},
        {"respBody": {"data": {"applyTemplate": {"jobId": "job_7f3a"}}},
         "respHeaders": {"X-Request-Id": "rq_99"}},
    ]

def test_extract_json_ptr_first_hit_wins() -> None:
    assert c.extract_value("json-ptr:/token", _paired(), {}) == "csrf_abc"
    assert c.extract_value("json-ptr:/data/applyTemplate/jobId", _paired(), {}) == "job_7f3a"

def test_extract_header_case_insensitive() -> None:
    assert c.extract_value("header:x-request-id", _paired(), {}) == "rq_99"

def test_extract_unresolvable_extractor_is_none() -> None:
    # path-tmpl/whole-payload are resolved later, not at capture -> None (left to bind downstream)
    assert c.extract_value("whole-payload", _paired(), {}) is None
    assert c.extract_value("path-tmpl:jobId", _paired(), {}) is None
    assert c.extract_value("json-ptr:/nope", _paired(), {}) is None


# ---- golden_record ----

def test_golden_record_hashes_existing_file() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.pdf")
        open(p, "wb").write(b"%PDF-1.7 hello")
        rec = c.golden_record(p, "pdf", "r2")
        assert rec is not None
        assert rec["bytes"] == len(b"%PDF-1.7 hello")
        assert len(rec["sha256"]) == 16 and rec["tag"] == "pdf" and rec["produces_ref"] == "r2"

def test_golden_record_missing_path_keeps_nulls() -> None:
    rec = c.golden_record("/no/such/file.pdf", "pdf", "r2")
    assert rec is not None and rec["bytes"] is None and rec["sha256"] is None

def test_golden_record_all_none_is_none() -> None:
    assert c.golden_record(None, None, None) is None


# ---- build_segment_inputs (CONTRACTS §2 shape + binding rules) ----

def _segments() -> dict:
    return {
        "schema": "segments/v1",
        "regions": [
            {"kind": "UiRegion", "id": "u0", "produces": []},
            {"kind": "ApiSegment", "id": "s0",
             "consumes": [
                 {"ref": "r0", "shape": {"type": "string"}, "extractor": "json-ptr:/invoice_id",
                  "origin": "STEP_INPUT"},
                 {"ref": "r1", "shape": {"type": "string"},
                  "extractor": "json-ptr:/data/applyTemplate/jobId", "origin": "PRIOR_SEGMENT"},
             ]},
        ],
    }

def test_build_segment_inputs_shape() -> None:
    si = c.build_segment_inputs(
        ".o11y/run", _segments(), {"r0": "inv_001"},
        {"label": "run1", "ambient": {"tenant_id": "org_123"}}, None, _paired(),
    )
    assert si["schema"] == "segment_inputs/v1"
    assert si["run"] == ".o11y/run"
    assert si["input_identity"]["label"] == "run1"
    assert si["golden"] is None

def test_build_segment_inputs_binds_step_input_from_operator() -> None:
    si = c.build_segment_inputs(".o11y/run", _segments(), {"r0": "inv_001"},
                                {"label": "run1", "ambient": {}}, None, _paired())
    r0 = next(b for b in si["bindings"] if b["ref"] == "r0")
    assert r0["origin"] == "STEP_INPUT" and r0["value"] == "inv_001" and r0["segment_id"] == "s0"

def test_build_segment_inputs_binds_prior_segment_from_trace() -> None:
    si = c.build_segment_inputs(".o11y/run", _segments(), {"r0": "inv_001"},
                                {"label": "run1", "ambient": {}}, None, _paired())
    r1 = next(b for b in si["bindings"] if b["ref"] == "r1")
    assert r1["origin"] == "PRIOR_SEGMENT" and r1["value"] == "job_7f3a"

def test_build_segment_inputs_missing_step_input_is_null() -> None:
    # operator did not supply r0 -> value null (binding still recorded, never dropped)
    si = c.build_segment_inputs(".o11y/run", _segments(), {},
                                {"label": "run1", "ambient": {}}, None, [])
    r0 = next(b for b in si["bindings"] if b["ref"] == "r0")
    assert r0["value"] is None

def test_build_segment_inputs_skips_ui_regions() -> None:
    si = c.build_segment_inputs(".o11y/run", _segments(), {"r0": "x"},
                                {"label": "run1", "ambient": {}}, None, _paired())
    assert all(b["segment_id"] == "s0" for b in si["bindings"])
    assert len(si["bindings"]) == 2


# ---- write_segment_inputs end-to-end (varied runs produce differing input identities) ----

def _ns(**kw: object) -> object:
    class _NS:
        pass
    ns = _NS()
    defaults = {"segments": None, "inputs_json": None, "ambient_json": None,
                "golden": None, "golden_tag": None, "produces_ref": None, "run_label": None}
    for k, v in {**defaults, **kw}.items():
        setattr(ns, k, v)
    return ns

def test_write_segment_inputs_emits_file() -> None:
    with tempfile.TemporaryDirectory() as d:
        seg_path = os.path.join(d, "segments.json")
        json.dump(_segments(), open(seg_path, "w"))
        run = os.path.join(d, "run")
        os.makedirs(run)
        ns = _ns(segments=seg_path, inputs_json=json.dumps({"r0": "inv_001"}),
                 ambient_json=json.dumps({"tenant_id": "org_123"}), run_label="run")
        c.write_segment_inputs(run, ns)  # type: ignore[arg-type]
        si = json.load(open(os.path.join(run, "segment_inputs.json")))
        assert si["input_identity"]["label"] == "run"
        assert si["input_identity"]["ambient"]["tenant_id"] == "org_123"
        r0 = next(b for b in si["bindings"] if b["ref"] == "r0")
        assert r0["value"] == "inv_001"

def test_write_segment_inputs_two_runs_differ() -> None:
    # the classifier needs >=2 differing inputs; two run dirs must bind DIFFERENT input values
    with tempfile.TemporaryDirectory() as d:
        seg_path = os.path.join(d, "segments.json")
        json.dump(_segments(), open(seg_path, "w"))
        vals = {}
        for label, val in (("run", "inv_001"), ("run2", "inv_999")):
            run = os.path.join(d, label)
            os.makedirs(run)
            ns = _ns(segments=seg_path, inputs_json=json.dumps({"r0": val}), run_label=label)
            c.write_segment_inputs(run, ns)  # type: ignore[arg-type]
            si = json.load(open(os.path.join(run, "segment_inputs.json")))
            vals[label] = next(b for b in si["bindings"] if b["ref"] == "r0")["value"]
        assert vals["run"] != vals["run2"]

def test_write_segment_inputs_no_segments_is_noop() -> None:
    with tempfile.TemporaryDirectory() as d:
        run = os.path.join(d, "run")
        os.makedirs(run)
        c.write_segment_inputs(run, _ns())  # type: ignore[arg-type]
        assert not os.path.exists(os.path.join(run, "segment_inputs.json"))

def test_write_segment_inputs_attaches_golden() -> None:
    with tempfile.TemporaryDirectory() as d:
        seg_path = os.path.join(d, "segments.json")
        json.dump(_segments(), open(seg_path, "w"))
        gold = os.path.join(d, "out.pdf")
        open(gold, "wb").write(b"%PDF-1.7 golden")
        run = os.path.join(d, "run")
        os.makedirs(run)
        ns = _ns(segments=seg_path, inputs_json=json.dumps({"r0": "inv_001"}),
                 golden=gold, golden_tag="pdf", produces_ref="r2", run_label="run")
        c.write_segment_inputs(run, ns)  # type: ignore[arg-type]
        si = json.load(open(os.path.join(run, "segment_inputs.json")))
        assert si["golden"]["tag"] == "pdf" and si["golden"]["produces_ref"] == "r2"
        assert si["golden"]["bytes"] == len(b"%PDF-1.7 golden")


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
