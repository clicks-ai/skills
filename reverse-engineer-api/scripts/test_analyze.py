#!/usr/bin/env python3
# Unit tests for analyze.py's structural analysis (S2 inputs). Runs with plain `python` (no pytest), no
# live browser, no engine — every fixture is synthetic paired.jsonl-shaped rows. The engine-driven
# candidate path is exercised only via the on-disk fixture run + --no-engine (no `node` needed).
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from typing import Any

import analyze as a


# ---- synthetic wire rows (paired.jsonl shape; CONTRACTS §0.1) ----
def _oc(rows: list[dict[str, Any]], golden_mime: str | None) -> dict[str, Any]:
    # ordered_calls returns dict[str, object] (heterogeneous); narrow to Any so the test can index freely.
    out: dict[str, Any] = a.ordered_calls(rows, golden_mime)
    return out


def _row(**kw: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "requestId": kw.get("requestId", "1.0"),
        "method": "GET", "url": "https://api.example.com/x", "origin": "https://api.example.com",
        "path": "/x", "query": {}, "status": 200, "type": "Fetch", "contentType": "application/json",
        "reqHeaders": {}, "reqBody": None, "respHeaders": {}, "respBody": None, "ts": 0,
    }
    base.update(kw)
    return base


# ---- locator_of / operation_of (GraphQL-multiplex only as an example) ----
def test_operation_of_reads_graphql_op() -> None:
    assert a.operation_of(_row(reqBody={"operationName": "ApplyTemplate"})) == "ApplyTemplate"
    assert a.operation_of(_row(reqBody={"foo": 1})) is None
    assert a.operation_of(_row(reqBody="raw")) is None

def test_locator_distinguishes_ops_on_one_url() -> None:
    r1 = _row(method="POST", path="/graphql", reqBody={"operationName": "A"})
    r2 = _row(method="POST", path="/graphql", reqBody={"operationName": "B"})
    assert a.locator_of(r1) != a.locator_of(r2)
    # plain REST has no operation, so locator is method+path
    assert a.locator_of(_row(method="GET", path="/job/7")) == "GET https://api.example.com/job/7"


# ---- is_mutation (protocol-agnostic; GraphQL/REST only as examples) ----
def test_is_mutation_graphql_mutation() -> None:
    assert a.is_mutation(_row(method="POST", reqBody={"query": "mutation M{ m{ ok } }"})) is True

def test_is_mutation_graphql_query_over_post_is_read() -> None:
    assert a.is_mutation(_row(method="POST", reqBody={"query": "query Q{ me{ id } }"})) is False

def test_is_mutation_get_is_read() -> None:
    assert a.is_mutation(_row(method="GET")) is False

def test_is_mutation_delete_and_plain_post_are_writes() -> None:
    assert a.is_mutation(_row(method="DELETE")) is True
    assert a.is_mutation(_row(method="POST", reqBody="name=x")) is True


# ---- pluggable extractors: json-ptr / header / path-tmpl / multipart / form / WHOLE-PAYLOAD ----
def test_value_sites_json_ptr_for_nested_body() -> None:
    sites = a.request_value_sites(_row(method="POST", reqBody={"variables": {"id": "tpl_88"}}))
    ptrs = {s["extractor"] for s in sites}
    assert "json-ptr:/variables/id" in ptrs

def test_value_sites_header_excludes_auto_and_auth() -> None:
    row = _row(reqHeaders={"x-csrf-token": "abc", "cookie": "sess=1", "authorization": "Bearer z"})
    extractors = {s["extractor"] for s in a.request_value_sites(row)}
    assert "header:x-csrf-token" in extractors
    assert "header:cookie" not in extractors and "header:authorization" not in extractors

def test_value_sites_path_tmpl_for_high_entropy_segment() -> None:
    sites = a.request_value_sites(_row(method="GET", path="/job/7f3a9c2b/pdf"))
    paths = [s["path"] for s in sites if s["extractor"] == "path-tmpl"]
    assert "7f3a9c2b" in paths and "job" not in paths and "pdf" not in paths

def test_value_sites_multipart_part_per_part() -> None:
    body = '--B\r\nContent-Disposition: form-data; name="file"; filename="a.bin"\r\n\r\n...\r\n--B\r\nContent-Disposition: form-data; name="kind"\r\n\r\nx\r\n--B--'
    row = _row(method="POST", reqBody=body, reqHeaders={"content-type": "multipart/form-data; boundary=B"})
    extractors = {s["extractor"] for s in a.request_value_sites(row)}
    assert "multipart-part:file" in extractors and "multipart-part:kind" in extractors

def test_value_sites_form_urlencoded() -> None:
    row = _row(method="POST", reqBody="a=1&b=2",
               reqHeaders={"content-type": "application/x-www-form-urlencoded"})
    extractors = {s["extractor"] for s in a.request_value_sites(row)}
    assert "form-key:a" in extractors and "form-key:b" in extractors

def test_value_sites_opaque_body_is_whole_payload_not_unexplained() -> None:
    # an un-introspectable binary/proto body is a real value site at WHOLE-PAYLOAD granularity.
    row = _row(method="POST", reqBody="\x08\x96\x01\x12\x07opaque",
               reqHeaders={"content-type": "application/octet-stream"})
    extractors = [s["extractor"] for s in a.request_value_sites(row)]
    assert "whole-payload" in extractors


# ---- 1:N response handling (never truncate to the first frame) ----
def test_one_to_n_groups_repeated_locator_in_order() -> None:
    rows = [
        _row(requestId="1", method="GET", path="/stream", status=200, contentType="text/event-stream"),
        _row(requestId="2", method="GET", path="/stream", status=200, contentType="text/event-stream"),
        _row(requestId="3", method="GET", path="/stream", status=200, contentType="text/event-stream"),
    ]
    out = _oc(rows, None)
    stream = next(r for r in out["responses"] if "/stream" in r["locator"])
    assert stream["is_one_to_n"] is True and stream["frame_count"] == 3
    assert stream["exchange_seqs"] == [0, 1, 2]  # ordered, all frames kept

def test_ordered_calls_preserves_capture_order() -> None:
    rows = [_row(requestId="a", path="/p1"), _row(requestId="b", path="/p2"), _row(requestId="c", path="/p3")]
    out = _oc(rows, None)
    assert [c["seq"] for c in out["ordered_calls"]] == [0, 1, 2]
    assert [c["exchange_ref"] for c in out["ordered_calls"]] == ["a", "b", "c"]


# ---- async/poll signal: repeated status read AND 202->200 transition ----
def test_poll_signal_on_repeated_status_read() -> None:
    rows = [
        _row(requestId="1", method="GET", path="/job/77", respBody={"status": "RUNNING"}),
        _row(requestId="2", method="GET", path="/job/77", respBody={"status": "RUNNING"}),
        _row(requestId="3", method="GET", path="/job/77", respBody={"status": "COMPLETE"}),
    ]
    out = _oc(rows, None)
    assert len(out["polls"]) == 1
    poll = out["polls"][0]
    assert poll["readyField"] == "json-ptr:/status"
    assert poll["readyValue"] == "COMPLETE" and poll["readyValueRecognized"] is True
    assert poll["evidence"]["repeated_read"] is True and poll["evidence"]["read_count"] == 3

def test_poll_signal_on_202_to_200_transition() -> None:
    rows = [
        _row(requestId="1", method="GET", path="/export", status=202, respBody=None),
        _row(requestId="2", method="GET", path="/export", status=200, respBody=None),
    ]
    out = _oc(rows, None)
    assert len(out["polls"]) == 1
    poll = out["polls"][0]
    assert poll["readyField"] == "status-code" and poll["readyValue"] == 200
    assert poll["evidence"]["status_transition"] is True

def test_no_poll_signal_for_single_read() -> None:
    rows = [_row(requestId="1", method="GET", path="/once", status=200, respBody={"status": "ok"})]
    assert _oc(rows, None)["polls"] == []

def test_mutation_then_single_read_is_not_a_poll() -> None:
    # one act + one read on different locators -> no repeated read, no transition -> no poll.
    rows = [
        _row(requestId="1", method="POST", path="/graphql", reqBody={"query": "mutation M{ x }"}),
        _row(requestId="2", method="GET", path="/result", status=200, respBody={"done": True}),
    ]
    assert _oc(rows, None)["polls"] == []


# ---- artifactOrigin: golden MIME present vs client-rendered hint ----
def test_artifact_origin_present_when_mime_in_a_response() -> None:
    rows = [
        _row(requestId="1", method="GET", path="/meta", contentType="application/json"),
        _row(requestId="2", method="GET", path="/file", contentType="application/pdf"),
    ]
    ao = _oc(rows, "application/pdf")["artifactOrigin"]
    assert ao["appears_in_a_response"] is True and ao["hint"] == "server-rendered"

def test_artifact_origin_client_rendered_when_mime_absent() -> None:
    rows = [_row(requestId="1", method="GET", path="/data", contentType="application/json")]
    ao = _oc(rows, "application/pdf")["artifactOrigin"]
    assert ao["appears_in_a_response"] is False
    assert "client-rendered" in ao["hint"] and "BAIL-1" in ao["hint"]

def test_artifact_origin_unknown_when_no_golden_mime() -> None:
    rows = [_row(requestId="1", contentType="application/json")]
    ao = _oc(rows, None)["artifactOrigin"]
    assert ao["appears_in_a_response"] is None and ao["hint"] is None


# ---- end-to-end main() over an on-disk fixture run, engine skipped (--no-engine) ----
def _write_fixture_run(d: str) -> str:
    run = os.path.join(d, "run")
    interm = os.path.join(run, "api-spec", "intermediate")
    os.makedirs(interm)
    # minimal endpoints.with-schemas.jsonl so the candidate path produces output
    ep = {
        "method": "POST", "origin": "https://api.example.com", "path": "/graphql [ApplyTemplate]",
        "operationName": "ApplyTemplate", "pathParams": [], "queryParams": [], "statusCodes": [200],
        "pathHash": "deadbeef", "requestContentType": "application/json",
        "responseContentTypes": {"200": "application/json"},
        "requestExample": {"operationName": "ApplyTemplate", "variables": {"id": "tpl_88"}},
        "responseExample": {"data": {"applyTemplate": {"jobId": "job_7f3a"}}},
        "observedAuthHeaders": ["authorization"], "sampleCount": 1, "responseBodyKnown": True,
    }
    with open(os.path.join(interm, "endpoints.with-schemas.jsonl"), "w") as f:
        f.write(json.dumps(ep) + "\n")
    paired = [
        _row(requestId="1", method="POST", path="/graphql",
             reqBody={"operationName": "ApplyTemplate", "variables": {"id": "tpl_88"}},
             respBody={"data": {"applyTemplate": {"jobId": "job_7f3a"}}}),
        _row(requestId="2", method="GET", path="/job/job_7f3a", respBody={"status": "RUNNING"}),
        _row(requestId="3", method="GET", path="/job/job_7f3a", respBody={"status": "COMPLETE"}),
        _row(requestId="4", method="GET", path="/job/job_7f3a/pdf", contentType="application/pdf"),
    ]
    with open(os.path.join(interm, "paired.jsonl"), "w") as f:
        for r in paired:
            f.write(json.dumps(r) + "\n")
    return run


def _main(argv: list[str]) -> dict[str, Any]:
    buf = io.StringIO()
    argv_backup = sys.argv
    sys.argv = ["analyze.py", *argv]
    try:
        with redirect_stdout(buf):
            a.main()
    finally:
        sys.argv = argv_backup
    result: dict[str, Any] = json.loads(buf.getvalue())
    return result

def test_main_emits_candidates_and_structure() -> None:
    with tempfile.TemporaryDirectory() as d:
        run = _write_fixture_run(d)
        out = _main(["--run", run, "--no-engine", "--golden-mime", "application/pdf", "--golden-tag", "pdf"])
        # existing candidate output preserved
        assert out["candidate_count"] == 1
        assert out["candidates"][0]["operationName"] == "ApplyTemplate"
        assert out["candidates"][0]["responseExample"]["data"]["applyTemplate"]["jobId"] == "job_7f3a"
        # new structural analysis present
        s = out["structure"]
        assert [c["exchange_ref"] for c in s["ordered_calls"]] == ["1", "2", "3", "4"]
        assert s["ordered_calls"][0]["is_mutation"] is True
        assert len(s["polls"]) == 1 and s["polls"][0]["readyValue"] == "COMPLETE"
        assert s["artifactOrigin"]["appears_in_a_response"] is True
        assert s["golden_tag"] == "pdf"


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
