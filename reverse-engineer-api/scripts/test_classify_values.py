#!/usr/bin/env python3
# Unit tests for classify_values: the S2 subset + S3 classifier + INV-1/INV-2 gate. Runs under plain
# `python` (no pytest). Each test synthesizes >=2 varied-input run dirs (paired.jsonl + segment_inputs.json)
# on disk and drives the pure logic — no live browser, no other in-flight modules imported.
import json
import os
import shutil
import sys
import tempfile

import classify_values as c

# ---- fixture builders ------------------------------------------------------------------------------


def _write_run(root: str, name: str, rows: list[dict], inputs: dict) -> str:
    run_dir = os.path.join(root, name)
    inter = os.path.join(run_dir, "api-spec", "intermediate")
    os.makedirs(inter, exist_ok=True)
    with open(os.path.join(inter, "paired.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(run_dir, "segment_inputs.json"), "w") as f:
        json.dump(inputs, f)
    return run_dir


def _row(rid: str, method: str, path: str, *, op: str | None = None, req: object = None,
         resp: object = None, rctype: str = "application/json", req_headers: dict | None = None) -> dict:
    body = req
    if op is not None:
        body = {"operationName": op, **(req if isinstance(req, dict) else {})}
    return {
        "requestId": rid, "method": method, "url": "https://api.x.com" + path, "origin": "https://api.x.com",
        "path": path, "query": {}, "status": 200, "contentType": rctype,
        "reqHeaders": req_headers or {"content-type": "application/json"},
        "reqBody": body, "respHeaders": {"content-type": rctype}, "respBody": resp, "ts": 1,
    }


def _inputs(label: str, bindings: list[dict], *, golden_tag: str = "pdf", ambient: dict | None = None) -> dict:
    return {
        "schema": "segment_inputs/v1", "run": label,
        "input_identity": {"label": label, "ambient": ambient or {}},
        "bindings": bindings,
        "golden": {"path": None, "tag": golden_tag, "bytes": 1000, "sha256": "x", "produces_ref": "rOut"},
    }


def _binding(ref: str, value: object, origin: str = "STEP_INPUT") -> dict:
    return {"ref": ref, "segment_id": "s0", "origin": origin, "value": value,
            "shape": {"type": "string"}, "extractor": "json-ptr:/x"}


def _value_for(plan: dict, carrier_suffix: str) -> dict | None:
    for v in plan["values"]:
        if v["carrier"].endswith(carrier_suffix):
            return v
    return None


def _build(rows1: list[dict], inputs1: dict, rows2: list[dict], inputs2: dict) -> dict:
    root = tempfile.mkdtemp()
    try:
        r1 = _write_run(root, "run", rows1, inputs1)
        r2 = _write_run(root, "run2", rows2, inputs2)
        return c.build_plan([c.Run(r1), c.Run(r2)], "s0", None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---- pure-helper tests -----------------------------------------------------------------------------


def test_high_entropy_rejects_low_cardinality():
    assert c.is_high_entropy("pdf") is False
    assert c.is_high_entropy("default") is False
    assert c.is_high_entropy(3) is False
    assert c.is_high_entropy(True) is False
    assert c.is_high_entropy("aaaaaaaa") is False  # long but no entropy


def test_high_entropy_accepts_unique_handle():
    assert c.is_high_entropy("job_7f3ad9e21b4c") is True
    assert c.is_high_entropy("a1b2c3d4e5f6a7b8") is True


def test_is_mutation_graphql_query_is_not_mutation():
    row = _row("1", "POST", "/graphql", req={"query": "query Me { me { id } }"})
    assert c.is_mutation(row) is False


def test_is_mutation_graphql_mutation_is_mutation():
    row = _row("1", "POST", "/graphql", req={"query": "mutation Apply { applyTemplate { jobId } }"})
    assert c.is_mutation(row) is True


def test_is_mutation_get_is_read():
    assert c.is_mutation(_row("1", "GET", "/job/abc")) is False


def test_is_mutation_lowercase_method_is_write():
    # regression: some capture tools emit a lowercase method; "post" must still be a mutation.
    assert c.is_mutation(_row("1", "post", "/jobs", req={"name": "x"})) is True


def test_is_mutation_query_named_mutation_is_not_write():
    # regression: a read whose op name merely CONTAINS "mutation" must not be flagged a write (anchored).
    row = _row("1", "POST", "/graphql", req={"query": "query GetMutationStatus { status }"})
    assert c.is_mutation(row) is False


def test_request_carriers_flattens_body_query_headers():
    row = _row("1", "POST", "/x", req={"variables": {"id": "abc"}}, req_headers={"x-csrf-token": "tok"})
    cs = dict(c.request_carriers(row))
    assert cs["json-ptr:/variables/id"] == "abc"
    assert cs["header:x-csrf-token"] == "tok"


# ---- bucket tests (each via a full 2-run plan) -----------------------------------------------------


def _input_run(label: str, inv_id: str) -> tuple[list[dict], dict]:
    # ONE call: a GET whose path-equivalent body carries the segment input + a CONST format; produces the
    # pdf golden directly. INPUT co-varies with the supplied input across runs.
    rows = [
        _row("e1", "POST", "/export", req={"variables": {"invoiceId": inv_id, "format": "pdf"}},
             resp={"ok": True}, rctype="application/pdf"),
    ]
    inputs = _inputs(label, [_binding("rInv", inv_id)])
    return rows, inputs


def test_input_and_const_buckets():
    rows1, in1 = _input_run("run1", "inv_aaaa1111bbbb")
    rows2, in2 = _input_run("run2", "inv_cccc2222dddd")
    plan = _build(rows1, in1, rows2, in2)
    inp = _value_for(plan, "/invoiceId")
    const = _value_for(plan, "/format")
    assert inp is not None and inp["bucket"] == "INPUT", inp
    assert inp["source"]["kind"] == "step_input"
    assert const is not None and const["bucket"] == "CONST", const
    assert const["value"] == "pdf"
    assert plan["gate"]["G1_self_contained"]["pass"] is True
    assert plan["verdict"] == "API-CANDIDATE"


def test_derived_bucket_threads_from_earlier_response():
    # n0 GET /csrf -> token; n1 POST sends it as header + carries the input; n1's pdf is the golden.
    def runset(label: str, inv: str, tok: str) -> tuple[list[dict], dict]:
        rows = [
            _row("c0", "GET", "/csrf", resp={"token": tok}),
            _row("c1", "POST", "/export", req={"variables": {"invoiceId": inv}},
                 req_headers={"content-type": "application/json", "x-csrf-token": tok},
                 resp={"ok": True}, rctype="application/pdf"),
        ]
        return rows, _inputs(label, [_binding("rInv", inv)])
    rows1, in1 = runset("run1", "inv_aaaa1111bbbb", "csrf_111aaa222bbb")
    rows2, in2 = runset("run2", "inv_cccc2222dddd", "csrf_333ccc444ddd")
    plan = _build(rows1, in1, rows2, in2)
    der = _value_for(plan, "x-csrf-token")
    assert der is not None and der["bucket"] == "DERIVED", der
    assert der["source"]["src_node"] == "n0"
    # the csrf GET must be pulled into R by transitive closure (DESIGN FN-2)
    assert any(n["request"]["locator"].endswith("/csrf") for n in plan["subset_R"])
    assert plan["verdict"] == "API-CANDIDATE"


def test_produced_bucket_from_mutation():
    # n0 mutation -> jobId; n1 GET /job/{jobId} consumes it (PRODUCED, src is a mutation in R) and yields pdf.
    def runset(label: str, inv: str, job: str) -> tuple[list[dict], dict]:
        rows = [
            _row("m0", "POST", "/graphql", op="ApplyTemplate",
                 req={"variables": {"invoiceId": inv}},
                 resp={"data": {"applyTemplate": {"jobId": job}}}),
            _row("m1", "GET", "/job", req={"jobId": job}, resp={"ok": True}, rctype="application/pdf"),
        ]
        return rows, _inputs(label, [_binding("rInv", inv)])
    rows1, in1 = runset("run1", "inv_aaaa1111bbbb", "job_777fff888eee")
    rows2, in2 = runset("run2", "inv_cccc2222dddd", "job_999aaa000bbb")
    plan = _build(rows1, in1, rows2, in2)
    prod = _value_for(plan, "/jobId")
    assert prod is not None and prod["bucket"] == "PRODUCED", prod
    assert prod["evidence"]["src_is_mutation"] is True
    assert prod["source"]["src_node"] == "n0"
    assert plan["dangling_produced"] == []
    assert plan["verdict"] == "API-CANDIDATE"


def test_path_threaded_jobid_is_produced() -> None:
    # A 2-call REST chain where the jobId is threaded ONLY through the URL path: POST /jobs mints a
    # jobId in its response, then GET /jobs/<jobId> consumes it (no body/query carrier — path-tmpl only)
    # and yields the pdf golden. The path segment must be surfaced as a `path-tmpl:` carrier and bucketed
    # PRODUCED (DERIVED whose source is a mutation in R). Without path-segment extraction it would never
    # be classified and the chain would silently under-detect.
    def runset(label: str, inv: str, job: str) -> tuple[list[dict[str, object]], dict[str, object]]:
        rows = [
            _row("j0", "POST", "/jobs", req={"variables": {"invoiceId": inv}}, resp={"jobId": job}),
            _row("j1", "GET", "/jobs/" + job, resp={"ok": True}, rctype="application/pdf"),
        ]
        return rows, _inputs(label, [_binding("rInv", inv)])
    rows1, in1 = runset("run1", "inv_aaaa1111bbbb", "job_777fff888eee")
    rows2, in2 = runset("run2", "inv_cccc2222dddd", "job_999aaa000bbb")
    plan = _build(rows1, in1, rows2, in2)
    prod = _value_for(plan, "path-tmpl:jobs")
    assert prod is not None and prod["bucket"] == "PRODUCED", prod
    assert prod["carrier"] == "path-tmpl:jobs", prod["carrier"]
    assert prod["evidence"]["src_is_mutation"] is True
    assert prod["source"]["src_node"] == "n0"
    # the producing POST /jobs mutation must be pulled into R by the path-carrier closure
    assert any(n["request"]["locator"].endswith("/jobs") and n["is_mutation"] for n in plan["subset_R"])
    assert plan["dangling_produced"] == []
    assert plan["gate"]["G1_self_contained"]["pass"] is True
    assert plan["verdict"] == "API-CANDIDATE"


def test_path_threaded_jobid_dangling_when_mutation_missing() -> None:
    # THE PATH-THREADED MISS: the same chain with the producing POST /jobs absent from the trace. The path
    # jobId now matches no input, no earlier in-R response, no nonce hint -> UNEXPLAINED (the dangling-
    # PRODUCED fingerprint of an incomplete capture) -> G1 FAIL -> KEEP-UI. This is exactly the under-
    # detection the path-tmpl extractor closes: the value is now SEEN and correctly flagged, not dropped.
    def runset(label: str, inv: str, job: str) -> tuple[list[dict[str, object]], dict[str, object]]:
        rows = [
            _row("j1", "GET", "/jobs/" + job, resp={"ok": True}, rctype="application/pdf"),
        ]
        return rows, _inputs(label, [_binding("rInv", inv)])
    rows1, in1 = runset("run1", "inv_aaaa1111bbbb", "job_777fff888eee")
    rows2, in2 = runset("run2", "inv_cccc2222dddd", "job_999aaa000bbb")
    plan = _build(rows1, in1, rows2, in2)
    miss = _value_for(plan, "path-tmpl:jobs")
    assert miss is not None and miss["bucket"] == "UNEXPLAINED", miss
    assert any(u["carrier"] == "path-tmpl:jobs" for u in plan["unexplained"]), plan["unexplained"]
    assert plan["gate"]["G1_self_contained"]["pass"] is False
    assert plan["verdict"] == "KEEP-UI"
    assert plan["bail"]["code"] == "BAIL-2"


def test_computed_generator_bucket():
    # an idempotency key: high-entropy, differs every run, matches no input/response, name hints a nonce.
    def runset(label: str, inv: str, key: str) -> tuple[list[dict], dict]:
        rows = [
            _row("g0", "POST", "/export",
                 req={"variables": {"invoiceId": inv, "idempotencyKey": key}},
                 resp={"ok": True}, rctype="application/pdf"),
        ]
        return rows, _inputs(label, [_binding("rInv", inv)])
    rows1, in1 = runset("run1", "inv_aaaa1111bbbb", "k_11112222333344445555")
    rows2, in2 = runset("run2", "inv_cccc2222dddd", "k_66667777888899990000")
    plan = _build(rows1, in1, rows2, in2)
    comp = _value_for(plan, "/idempotencyKey")
    assert comp is not None and comp["bucket"] == "COMPUTED", comp
    assert comp["recipe"]["kind"] == "generator"
    assert comp["evidence"]["differs_across_runs"] is True
    assert plan["verdict"] == "API-CANDIDATE"


def test_ambient_input_bucket():
    # a tenant id that is stable across runs but ALSO lives in the run's ambient identity -> AMBIENT-INPUT,
    # threaded from auth, not hardcoded (DESIGN CLASS-2).
    def runset(label: str, inv: str) -> tuple[list[dict], dict]:
        rows = [
            _row("a0", "POST", "/export",
                 req={"variables": {"invoiceId": inv, "tenantId": "org_55aa66bb77cc"}},
                 resp={"ok": True}, rctype="application/pdf"),
        ]
        return rows, _inputs(label, [_binding("rInv", inv)], ambient={"tenant_id": "org_55aa66bb77cc"})
    rows1, in1 = runset("run1", "inv_aaaa1111bbbb")
    rows2, in2 = runset("run2", "inv_cccc2222dddd")
    plan = _build(rows1, in1, rows2, in2)
    amb = _value_for(plan, "/tenantId")
    assert amb is not None and amb["bucket"] == "AMBIENT-INPUT", amb
    assert amb["source"]["kind"] == "ambient"
    assert plan["verdict"] == "API-CANDIDATE"


# ---- the two MISS fixtures (the whole point) -------------------------------------------------------


def test_unexplained_high_entropy_unmatched_value_fails_gate():
    # a high-entropy field that equals NOTHING (no input, no response, no nonce-name hint) -> UNEXPLAINED
    # -> G1 FAIL -> nonzero. (The fingerprint of a missed call.)
    def runset(label: str, inv: str, mystery: str) -> tuple[list[dict], dict]:
        rows = [
            _row("u0", "POST", "/export",
                 req={"variables": {"invoiceId": inv, "signature": mystery}},
                 resp={"ok": True}, rctype="application/pdf"),
        ]
        return rows, _inputs(label, [_binding("rInv", inv)])
    rows1, in1 = runset("run1", "inv_aaaa1111bbbb", "sig_zzz111yyy222xxx")
    rows2, in2 = runset("run2", "inv_cccc2222dddd", "sig_qqq333www444eee")
    plan = _build(rows1, in1, rows2, in2)
    un = _value_for(plan, "/signature")
    assert un is not None and un["bucket"] == "UNEXPLAINED", un
    assert plan["unexplained"], "expected a non-empty unexplained list"
    assert plan["gate"]["G1_self_contained"]["pass"] is False
    assert plan["verdict"] == "KEEP-UI"
    assert plan["bail"]["code"] == "BAIL-2"


def test_dangling_produced_when_mutation_missing_from_capture():
    # THE METAVIEW MISS: the export consumes a jobId that ONLY a (missed) apply-template mutation could
    # mint. The producing mutation is NOT in the capture; the jobId equals a value in an EARLIER read's
    # response that is itself a mutation-less echo -> we synthesize the dangling case by making the jobId's
    # only in-trace source a mutation call that the closure did NOT keep in R.
    #
    # Construct: n0 = a GET (read) that the closure pulls in only because it echoes jobId; the REAL source
    # is a POST mutation that comes AFTER the read in capture order, so it is never an EARLIER source ->
    # the jobId resolves to a PRODUCED whose src_node falls outside R == dangling.
    #
    # Simpler faithful construction: jobId appears in the request but its sole earlier producer is a
    # mutation response we DROP from the run by not recording it — leaving the value UNEXPLAINED is the
    # generic miss; to exercise the *dangling-PRODUCED* path specifically we craft a PRODUCED value whose
    # source mutation is present for classification but excluded from R closure.
    job = "job_deadbeef12345678"
    inv1, inv2 = "inv_aaaa1111bbbb", "inv_cccc2222dddd"
    # n0: GET /status echoes jobId (read) -> closure keeps it as the source; n1: GET /job/{jobId} -> pdf.
    # We force PRODUCED-with-missing-mutation by post-processing the plan: relabel the source as a mutation
    # node that is not in R. Build the natural plan first, then assert the generic-miss path, AND separately
    # exercise collect_misses with a hand-built dangling value to prove the dangling branch.
    rows1 = [
        _row("d0", "GET", "/status", resp={"jobId": job}),
        _row("d1", "GET", "/job", req={"jobId": job}, resp={"ok": True}, rctype="application/pdf"),
    ]
    rows2 = [
        _row("d0", "GET", "/status", resp={"jobId": "job_feedface87654321"}),
        _row("d1", "GET", "/job", req={"jobId": "job_feedface87654321"}, resp={"ok": True}, rctype="application/pdf"),
    ]
    in1 = _inputs("run1", [_binding("rInv", inv1)])
    in2 = _inputs("run2", [_binding("rInv", inv2)])
    plan = _build(rows1, in1, rows2, in2)
    # the jobId is DERIVED from a non-mutation read here (well-formed), so it is NOT dangling:
    job_v = _value_for(plan, "/jobId")
    assert job_v is not None and job_v["bucket"] == "DERIVED", job_v
    assert plan["dangling_produced"] == []

    # now the dangling branch directly: a PRODUCED value whose src_node is outside R.
    fake_values = [{
        "node": "n1", "carrier": "json-ptr:/jobId", "bucket": "PRODUCED",
        "all_matching_buckets": ["PRODUCED"],
        "source": {"kind": "response", "src_node": "n9", "src_path": "json-ptr:/data/applyTemplate/jobId"},
        "evidence": {"src_is_mutation": True},
    }]
    node_of_seq = {0: "n0", 1: "n1"}
    un, con, dang = c.collect_misses(fake_values, [0, 1], node_of_seq)
    assert dang and dang[0]["src_node"] == "n9", dang
    assert un == [] and con == []


def test_contested_value_matches_two_buckets():
    # A value that is BOTH a segment input AND appears as an earlier in-R mutation's response field ->
    # matches INPUT and PRODUCED -> CONTESTED, never first-match-resolved (DESIGN CLASS-1).
    shared1 = "tok_aaaa1111bbbb22"
    shared2 = "tok_cccc3333dddd44"

    def runset(label: str, shared: str) -> tuple[list[dict], dict]:
        rows = [
            # a mutation whose response surfaces the SAME value the operator also supplied as the input
            _row("x0", "POST", "/graphql", op="Mint", req={"variables": {}},
                 resp={"data": {"mint": {"id": shared}}}),
            _row("x1", "POST", "/export", req={"variables": {"sharedId": shared}},
                 resp={"ok": True}, rctype="application/pdf"),
        ]
        # the operator ALSO declares `shared` as a step input value -> both INPUT and PRODUCED match
        return rows, _inputs(label, [_binding("rShared", shared)])
    rows1, in1 = runset("run1", shared1)
    rows2, in2 = runset("run2", shared2)
    plan = _build(rows1, in1, rows2, in2)
    con = _value_for(plan, "/sharedId")
    assert con is not None and con["bucket"] == "CONTESTED", con
    assert set(con["all_matching_buckets"]) >= {"INPUT", "PRODUCED"}, con["all_matching_buckets"]
    assert plan["contested"], "expected a non-empty contested list"
    assert plan["gate"]["G1_self_contained"]["pass"] is False
    assert plan["verdict"] == "KEEP-UI"
    assert plan["bail"]["code"] == "BAIL-2"


def test_bail1_when_golden_in_no_response():
    # the golden is a pdf but no response is a pdf -> client-rendered -> BAIL-1.
    def runset(label: str, inv: str) -> tuple[list[dict], dict]:
        rows = [_row("z0", "POST", "/export", req={"variables": {"invoiceId": inv}}, resp={"ok": True})]
        return rows, _inputs(label, [_binding("rInv", inv)], golden_tag="pdf")
    rows1, in1 = runset("run1", "inv_aaaa1111bbbb")
    rows2, in2 = runset("run2", "inv_cccc2222dddd")
    plan = _build(rows1, in1, rows2, in2)
    assert plan["golden_source"]["found"] is False
    assert plan["bail"]["code"] == "BAIL-1"
    assert plan["verdict"] == "KEEP-UI"


# ---- control flow / gate G2 ------------------------------------------------------------------------


def test_poll_inserted_for_repeated_status_read():
    # a status read fired twice with a body /status field -> a POLL is synthesized (no fixed sleep).
    def runset(label: str, inv: str, job: str) -> tuple[list[dict], dict]:
        rows = [
            _row("p0", "POST", "/graphql", op="Apply", req={"variables": {"invoiceId": inv}},
                 resp={"data": {"apply": {"jobId": job}}}),
            _row("p1", "GET", "/job", req={"jobId": job}, resp={"status": "RUNNING"}),
            _row("p2", "GET", "/job", req={"jobId": job}, resp={"status": "COMPLETE"}),
            _row("p3", "GET", "/download", req={"jobId": job}, resp={"ok": True}, rctype="application/pdf"),
        ]
        return rows, _inputs(label, [_binding("rInv", inv)])
    rows1, in1 = runset("run1", "inv_aaaa1111bbbb", "job_777fff888eee")
    rows2, in2 = runset("run2", "inv_cccc2222dddd", "job_999aaa000bbb")
    plan = _build(rows1, in1, rows2, in2)
    assert plan["control_flow"]["polls"], "expected a POLL for the repeated status read"
    poll = plan["control_flow"]["polls"][0]
    assert poll["predicate"]["over"] == "body-field"
    assert poll["predicate"]["path"] == "json-ptr:/status"
    # regression: the predicate must wait for the TERMINAL status (COMPLETE), not the first (RUNNING) — else
    # the poll exits immediately and fetches the artifact prematurely (the Metaview async bug).
    assert poll["predicate"]["equals"] == "COMPLETE", poll["predicate"]["equals"]
    assert any(s["op"] == "POLL" for s in plan["steps"])
    assert plan["gate"]["G2_no_fixed_wait"]["pass"] is True


def test_low_entropy_input_is_classified_not_unexplained():
    # regression: a SHORT id (42 -> 97) that co-varies with the declared input is INPUT, not UNEXPLAINED
    # (the INPUT bucket must not be entropy-gated — co-variation with a KNOWN input is the evidence).
    def runset(label: str, inv: str) -> tuple[list[dict], dict]:
        rows = [_row("e0", "POST", "/export", req={"invoiceId": inv}, resp={"ok": True}, rctype="application/pdf")]
        return rows, _inputs(label, [_binding("rInv", inv)])
    rows1, in1 = runset("run1", "42")
    rows2, in2 = runset("run2", "97")
    plan = _build(rows1, in1, rows2, in2)
    v = _value_for(plan, "/invoiceId")
    assert v is not None and v["bucket"] == "INPUT", v


def test_golden_source_single_not_assembled_for_common_ctype():
    # regression: a common content-type (json) matching MANY responses must NOT become 'assembled' — pick
    # the terminal single source, or R over-selects into a false PROVEN.
    root = tempfile.mkdtemp()
    try:
        rows = [_row("a", "GET", "/a", resp={"x": 1}), _row("b", "GET", "/b", resp={"y": 2}),
                _row("z", "POST", "/export", resp={"z": 3})]
        rd = _write_run(root, "run", rows, _inputs("run", [], golden_tag="json"))
        gs = c.find_golden_source(c.Run(rd))
        assert gs["mode"] == "single", gs["mode"]
        assert gs["exchange_seqs"] == [2], gs["exchange_seqs"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_golden_source_finds_base64_pdf_in_json():
    # the live-shakedown false negative: a binary artifact delivered base64 INSIDE application/json must be
    # found by its decoded magic (not declared client-rendered), with the decode recipe in the extractor.
    import base64 as b64
    pdf_b64 = b64.b64encode(b"%PDF-1.7\n" + b"x" * 200).decode()
    root = tempfile.mkdtemp()
    try:
        rows = [
            _row("a", "POST", "/apply", resp={"data": {"applyTemplate": {"jobId": "job_7f3ad9e21b4c"}}}),
            _row("z", "POST", "/export", resp={"data": {"exportArtifact": {"file": pdf_b64, "filename": "x.pdf"}}}),
        ]
        rd = _write_run(root, "run", rows, _inputs("run", [], golden_tag="pdf"))
        gs = c.find_golden_source(c.Run(rd))
        assert gs["found"] is True, gs
        assert gs["exchange_seqs"] == [1], gs["exchange_seqs"]
        assert gs["extractor"] == "json-ptr:/data/exportArtifact/file|base64", gs["extractor"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_golden_source_finds_raw_typed_artifact():
    # the raw-typed-download envelope still works (regression): content-type IS the artifact's type.
    root = tempfile.mkdtemp()
    try:
        rows = [_row("z", "POST", "/export", resp={"ok": True}, rctype="application/pdf")]
        rd = _write_run(root, "run", rows, _inputs("run", [], golden_tag="pdf"))
        gs = c.find_golden_source(c.Run(rd))
        assert gs["found"] and gs["extractor"] == "whole-payload" and gs["exchange_seqs"] == [0], gs
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_golden_source_base64_generalizes_across_types():
    # not pdf-specific: a base64 ZIP inside JSON is found by its own magic.
    import base64 as b64
    zip_b64 = b64.b64encode(b"PK\x03\x04" + b"y" * 200).decode()
    root = tempfile.mkdtemp()
    try:
        rows = [_row("z", "POST", "/export", resp={"result": {"archive": zip_b64}})]
        rd = _write_run(root, "run", rows, _inputs("run", [], golden_tag="zip"))
        gs = c.find_golden_source(c.Run(rd))
        assert gs["found"] and gs["extractor"] == "json-ptr:/result/archive|base64", gs
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_golden_source_bail1_when_truly_client_rendered():
    # no response carries the artifact (raw OR base64) -> genuinely client-rendered -> BAIL-1 (correct keep-UI).
    root = tempfile.mkdtemp()
    try:
        rows = [_row("a", "GET", "/data", resp={"rows": [1, 2, 3], "note": "drawn in the browser"})]
        rd = _write_run(root, "run", rows, _inputs("run", [], golden_tag="pdf"))
        gs = c.find_golden_source(c.Run(rd))
        assert gs["found"] is False, gs
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_repeat_inserted_for_pagination_signal():
    def runset(label: str, inv: str) -> tuple[list[dict], dict]:
        rows = [
            _row("r0", "GET", "/list", req={"invoiceId": inv}, resp={"items": [1], "next_cursor": "abc"}),
            _row("r1", "POST", "/export", req={"variables": {"invoiceId": inv}}, resp={"ok": True},
                 rctype="application/pdf"),
        ]
        return rows, _inputs(label, [_binding("rInv", inv)])
    rows1, in1 = runset("run1", "inv_aaaa1111bbbb")
    rows2, in2 = runset("run2", "inv_cccc2222dddd")
    plan = _build(rows1, in1, rows2, in2)
    assert plan["control_flow"]["repeats"], "expected a REPEAT for the cursor signal"


def test_main_returns_nonzero_on_miss() -> None:
    # end-to-end: main() writes plan.json and returns nonzero when a value is UNEXPLAINED.
    root = tempfile.mkdtemp()
    try:
        rows1 = [_row("u0", "POST", "/export", req={"variables": {"invoiceId": "inv_aaaa1111bbbb",
                 "signature": "sig_zzz111yyy222xxx"}}, resp={"ok": True}, rctype="application/pdf")]
        rows2 = [_row("u0", "POST", "/export", req={"variables": {"invoiceId": "inv_cccc2222dddd",
                 "signature": "sig_qqq333www444eee"}}, resp={"ok": True}, rctype="application/pdf")]
        r1 = _write_run(root, "run", rows1, _inputs("run1", [_binding("rInv", "inv_aaaa1111bbbb")]))
        r2 = _write_run(root, "run2", rows2, _inputs("run2", [_binding("rInv", "inv_cccc2222dddd")]))
        plan_path = os.path.join(root, "plan.json")
        rc = c.main(["--runs", r1, r2, "--segment-id", "s0", "--plan", plan_path])
        assert rc != 0, "a MISS must exit nonzero"
        with open(plan_path) as f:
            plan = json.load(f)
        assert plan["verdict"] == "KEEP-UI"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_main_returns_zero_on_clean_plan() -> None:
    root = tempfile.mkdtemp()
    try:
        rows1 = [_row("e1", "POST", "/export", req={"variables": {"invoiceId": "inv_aaaa1111bbbb",
                 "format": "pdf"}}, resp={"ok": True}, rctype="application/pdf")]
        rows2 = [_row("e1", "POST", "/export", req={"variables": {"invoiceId": "inv_cccc2222dddd",
                 "format": "pdf"}}, resp={"ok": True}, rctype="application/pdf")]
        r1 = _write_run(root, "run", rows1, _inputs("run1", [_binding("rInv", "inv_aaaa1111bbbb")]))
        r2 = _write_run(root, "run2", rows2, _inputs("run2", [_binding("rInv", "inv_cccc2222dddd")]))
        plan_path = os.path.join(root, "plan.json")
        rc = c.main(["--runs", r1, r2, "--segment-id", "s0", "--plan", plan_path])
        assert rc == 0, "a clean self-contained plan must exit 0"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_main_rejects_non_varied_runs() -> None:
    # two runs with the SAME input_identity.label must be refused (can't separate CONST/INPUT/COMPUTED).
    root = tempfile.mkdtemp()
    try:
        rows = [_row("e1", "POST", "/export", req={"variables": {"invoiceId": "inv_x"}}, resp={"ok": True},
                rctype="application/pdf")]
        r1 = _write_run(root, "run", rows, _inputs("same", [_binding("rInv", "inv_x")]))
        r2 = _write_run(root, "run2", rows, _inputs("same", [_binding("rInv", "inv_x")]))
        threw = False
        try:
            c.main(["--runs", r1, r2, "--plan", os.path.join(root, "p.json")])
        except SystemExit:
            threw = True
        assert threw, "identical labels must be rejected"
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
