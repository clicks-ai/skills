# CONTRACTS — frozen inter-stage wire formats

App-agnostic. Protocol (REST/GraphQL/RPC/multipart/binary), artifact (JSON/PDF/CSV/image/archive/stream),
auth scheme (cookie/Bearer/HMAC/refresh) and target app are *instances* — never assumed by any shape here.
Anything app-specific in an example below is labelled `e.g.` and is illustrative only.

These shapes are **FROZEN**. Every script reads/writes exactly these keys. New scripts (`partition.py`,
`classify_values.py`, `prove_runner.py`, `recombine.py`) build against these; existing scripts
(`capture_cdp.py`, `analyze.py`, `detect_replayable.py`, `probe_auth.py`, `run_in_page.py`,
`verify_equivalence.py`, `teach_insert.py`) keep emitting what they already emit — the additions below
**extend, never rename or drop** an existing key.

Conventions used throughout:
- `ValueRef.id` — a stable string handle for a value flowing between regions, format `r<seq>` (`"r0"`,`"r1"`,…).
- `segment_id` — a stable string, format `s<seq>` (`"s0"`,`"s1"`,…), minted by `partition.py`, reused everywhere.
- `seq` — capture-time order index of an `Exchange` within one run, 0-based, strictly increasing.
- `extractor` — one of: `json-ptr` | `form-key` | `header` | `path-tmpl` | `multipart-part` | `binary-decoder` | `whole-payload`.
- `origin` (handoff) — `STEP_INPUT` | `PRIOR_UI` | `PRIOR_SEGMENT`.
- `bucket` — `CONST` | `INPUT` | `AMBIENT-INPUT` | `DERIVED` | `PRODUCED` | `COMPUTED` | `UNEXPLAINED` | `CONTESTED`.
- All JSON is UTF-8; absent-and-`null` mean the same thing; consumers must tolerate unknown extra keys
  (response-model permissive: a producer may add fields).

---

## 0. The `.o11y/run/` directory layout

`capture_cdp.py` records the raw CDP wire under `<run>/cdp/network/`; the vendored engine (run by
`analyze.py`) derives `<run>/api-spec/`; the new stage scripts add `<run>/*.json` analysis artifacts.
Multiple **varied-input runs** are captured into **sibling run dirs** (`run`, `run2`, `run3`, …), each a
self-contained copy of this layout. Classification reads the whole set.

```
.o11y/
  run/                                 # run #1 (the build instance, capture-time)
    cdp/
      capture.pid                      # present only while a --start recorder is live
      capture.log
      network/
        requests.jsonl                 # one CDP Network.requestWillBeSent event per line (capture_cdp.py)
        responses.jsonl                # one CDP Network.responseReceived event per line
        bodies/
          <requestId>/request.json     # { "id": "<requestId>", "body": "<raw postData string>" }
          <requestId>/response.json    # { "id": "<requestId>", "body": "<raw response body string>" }
    api-spec/                          # engine output (analyze.py; only the analysis stages, never `emit`)
      intermediate/
        paired.jsonl                   # §0.1 — request/response pairs (load.mjs)
        filtered.jsonl                 # engine internal
        endpoints.jsonl                # engine internal (rows stripped)
        endpoint-samples.jsonl         # engine internal (rows sidecar)
        endpoints.with-schemas.jsonl   # §0.2 — inferred endpoints (infer.mjs) — analyze.py reads this
        redaction-stats.json
      samples/
        <method>__<pathHash>.json      # §0.3 — one redacted concrete example per endpoint
    segment_inputs.json                # §2 — handoff ref -> concrete captured value (capture_cdp.py, per run)
  run2/  …                             # run #2 — SAME layout, DIFFERENT input (varied), clean start
  run3/  …                             # optional further varied runs

# analysis artifacts (written once, keyed across the run set; live beside the primary run dir or in cwd)
segments.json                          # §1 — partition output (partition.py)
plan.json                              # §3 — classify + control-flow output (classify_values.py)
verify_receipt.json                    # §4 — prove output (prove_runner.py)
```

**Run-set contract.** A run dir is *self-describing*: its `cdp/network/` + `api-spec/` + `segment_inputs.json`
are everything a consumer needs for that one capture. `partition.py` runs **once** over the run set (it needs
no per-run wire — it classifies the workflow `W`). `classify_values.py` consumes **≥2** run dirs (named
explicitly, `--runs run run2 …`) because CONST/INPUT/COMPUTED separation requires varied inputs.

### 0.1 `paired.jsonl` (one JSON object per line — `load.mjs`; FROZEN, existing)

```json
{
  "requestId": "1000123.45",
  "method": "POST",
  "url": "https://api.example.com/graphql?opname=ApplyTemplate",
  "origin": "https://api.example.com",
  "path": "/graphql",
  "query": { "opname": "ApplyTemplate" },
  "status": 200,
  "type": "Fetch",
  "contentType": "application/json",
  "reqHeaders": { "content-type": "application/json", "x-csrf-token": "…" },
  "reqBody": { "operationName": "ApplyTemplate", "variables": { "id": "tpl_88" } },
  "respHeaders": { "content-type": "application/json" },
  "respBody": { "data": { "applyTemplate": { "jobId": "job_7f3a" } } },
  "ts": 1718900000123
}
```
`reqBody`/`respBody` are the parsed JSON when the body parsed, else the raw string, else `null`. `ts` is
epoch-ms or `null`. This is the **wire of record** for `partition.py`, `classify_values.py`, and
`detect_replayable.py` — they read `paired.jsonl`, never the raw CDP files.

### 0.2 `endpoints.with-schemas.jsonl` (one per line — `infer.mjs`; FROZEN, existing)

```json
{
  "endpointKey": "POST https://api.example.com/graphql [ApplyTemplate]",
  "origin": "https://api.example.com",
  "method": "POST",
  "path": "/graphql [ApplyTemplate]",
  "operationName": "ApplyTemplate",
  "discriminatorField": "operationName",
  "parentPath": "/graphql",
  "pathParams": [],
  "queryParams": [{ "name": "opname", "in": "query", "required": true, "schema": { "type": "string" } }],
  "statusCodes": [200],
  "pathHash": "a1b2c3d4e5",
  "requestBodyKnown": true,
  "responseBodyKnown": true,
  "requestContentType": "application/json",
  "responseContentTypes": { "200": "application/json" },
  "requestExample": { "operationName": "ApplyTemplate", "variables": { "id": "tpl_88" } },
  "responseExample": { "data": { "applyTemplate": { "jobId": "job_7f3a" } } },
  "observedAuthHeaders": ["authorization"],
  "sampleCount": 1,
  "normalizationFlags": ["single-sample"]
}
```

### 0.3 `samples/<method>__<pathHash>.json` (`infer.mjs`; FROZEN, existing)

```json
{
  "endpoint": "POST https://api.example.com/graphql [ApplyTemplate]",
  "request":  { "status": 200, "headers": { "content-type": "application/json", "x-csrf-token": "‹REDACTED›" }, "body": { "operationName": "ApplyTemplate", "variables": { "id": "tpl_88" } } },
  "response": { "status": 200, "headers": { "content-type": "application/json" }, "body": { "data": { "applyTemplate": { "jobId": "job_7f3a" } } } }
}
```

---

## 1. `segments.json` — partition output (`partition.py`, S0; NEW)

Runs **once** over `W` (and confirmed against a capture's `paired.jsonl` for nature). Partitions the
workflow into ordered `Region`s, mints stable `segment_id`s, and declares the **typed handoff graph** —
every value crossing a region boundary as a `HandoffSpec`. **Frozen contract:** no `DATA_WORK` action sits
outside a segment; every cross-region value is a declared, typed ref; pure-`NAVIGATE` actions are absorbed.

```json
{
  "schema": "segments/v1",
  "step": "steps/download-invoice.md",
  "grounded_against": ".o11y/run",
  "regions": [
    {
      "kind": "UiRegion",
      "id": "u0",
      "nature": "NAVIGATE",
      "actions": [
        { "i": 0, "text": "Open Chrome and log in", "nature": "FUZZY" },
        { "i": 1, "text": "Navigate to the invoice list", "nature": "NAVIGATE" }
      ],
      "produces": []
    },
    {
      "kind": "ApiSegment",
      "id": "s0",
      "actions": [
        { "i": 2, "text": "Apply the template", "nature": "DATA_WORK",
          "produces": ["r1"], "consumes": ["r0"] },
        { "i": 3, "text": "Wait for Saved, then Export as PDF", "nature": "DATA_WORK",
          "produces": ["r2"], "consumes": ["r1"], "observed": { "mutation_fired": true, "status_read_repeats": 3 } }
      ],
      "consumes": [
        { "ref": "r0", "shape": { "type": "string" }, "extractor": "json-ptr:/invoice_id", "origin": "STEP_INPUT" }
      ],
      "produces": [
        { "ref": "r2", "shape": { "type": "binary", "tag": "pdf" }, "extractor": "whole-payload", "origin": "PRIOR_SEGMENT" }
      ]
    }
  ],
  "handoffs": [
    { "ref": "r0", "shape": { "type": "string" },                 "extractor": "json-ptr:/invoice_id", "origin": "STEP_INPUT",     "from": null, "to": "s0" },
    { "ref": "r1", "shape": { "type": "string", "entropy": "high" }, "extractor": "json-ptr:/data/applyTemplate/jobId", "origin": "PRIOR_SEGMENT", "from": "s0", "to": "s0" },
    { "ref": "r2", "shape": { "type": "binary", "tag": "pdf" },    "extractor": "whole-payload",        "origin": "PRIOR_SEGMENT", "from": "s0", "to": null }
  ],
  "segment_ids": ["s0"],
  "bail": null
}
```

- `regions` — ordered; `kind ∈ {UiRegion, ApiSegment}`. `i` is the action index in `W`, strictly increasing
  across the whole list. A `UiRegion` carries `FUZZY`/`NAVIGATE`/`COMPREHEND` actions; an `ApiSegment` carries
  a *maximal contiguous run* of `DATA_WORK`.
- `nature ∈ {FUZZY, NAVIGATE, DATA_WORK, COMPREHEND}`. A pure `NAVIGATE` is **absorbed** into an adjacent
  `ApiSegment` (no region of its own); a `NAVIGATE` that fired a mutation is reclassified `DATA_WORK`.
- `action.observed` (optional, present only when grounded against capture): `{ "mutation_fired": bool,
  "status_read_repeats": int }` — the trace evidence that confirmed `DATA_WORK` / a needed `POLL`. Absence =
  verb-prior only.
- `handoffs[]` — every typed value crossing a boundary. `from`/`to` are region ids (`null` = workflow
  edge: `STEP_INPUT` enters at `from:null`; a final output leaves at `to:null`). `origin` per §2.1 of DESIGN.
  `shape` is what a consumer may assume; `entropy: "high"|"low"` is set when known (drives DERIVED eligibility).
- `segment_ids` — the minted ids, in order. **`segment_ids == [] ⇒ KEEP UI, done`** (workflow has no
  data-work to API-ify).
- `bail` — `null`, or `{ "code": "BAIL-1".."BAIL-5", "reason": "<one line>" }` if partition already proves
  keep-UI (rare at S0; usually later).

---

## 2. `segment_inputs.json` — captured handoff bindings (`capture_cdp.py`, S1; NEW, per run dir)

Written **per run dir** alongside `cdp/`. Binds each handoff `ref` declared in `segments.json` to the
**concrete value it actually took during that capture**, so a consumer never has to re-derive it. **Frozen
contract:** every `STEP_INPUT`/`PRIOR_UI` consume of every segment is bound; the binding records the run's
input identity so classification can confirm co-variation across runs.

```json
{
  "schema": "segment_inputs/v1",
  "run": ".o11y/run",
  "input_identity": { "label": "run1", "ambient": { "tenant_id": "org_123" } },
  "bindings": [
    { "ref": "r0", "segment_id": "s0", "origin": "STEP_INPUT",
      "value": "inv_001", "shape": { "type": "string" }, "extractor": "json-ptr:/invoice_id" },
    { "ref": "r1", "segment_id": "s0", "origin": "PRIOR_SEGMENT",
      "value": "job_7f3a", "shape": { "type": "string" }, "extractor": "json-ptr:/data/applyTemplate/jobId" }
  ],
  "golden": { "path": "/tmp/golden.pdf", "tag": "pdf", "bytes": 107431, "sha256": "9f2c…", "produces_ref": "r2" }
}
```

- `input_identity` — the run's varied input + ambient identity (`label` is the run's nickname, `ambient`
  holds tenant/session context). Two run dirs with the **same** `input_identity.label` are illegal for the
  classifier (it needs ≥2 *differing* inputs).
- `bindings[].value` — the concrete captured value (JSON scalar/array/object), or `null` if the segment did
  not consume it. `STEP_INPUT` values come from the operator-supplied inputs; `PRIOR_SEGMENT`/`PRIOR_UI`
  values are read out of the trace via `extractor`.
- `golden` — the artifact the UI produced *this run* (ground truth for the comparator). `tag` is the type
  tag; `produces_ref` ties it to the segment's terminal `produces` handoff. `path` may be `null` pre-capture.

---

## 3. `plan.json` — classify + subset + control-flow (`classify_values.py`, S2–S4; NEW)

The heart. Built per segment, across the run set. Locates the golden's source, **backward-closes the causal
subset R**, buckets **every value in every request of R**, and emits the ordered runnable steps with
POLL/REPEAT/RETRY. **Frozen contract:** `unexplained == [] AND contested == []` and every async gap has a
POLL and every continuation has a REPEAT — else the gate fails and the verdict is KEEP-UI.

```json
{
  "schema": "plan/v1",
  "segment_id": "s0",
  "runs": [".o11y/run", ".o11y/run2"],
  "signature": {
    "inputs":  [{ "ref": "r0", "shape": { "type": "string" } }],
    "output":  { "ref": "r2", "shape": { "type": "binary", "tag": "pdf" } }
  },
  "golden_source": {
    "found": true,
    "mode": "single",
    "exchange_seqs": [4],
    "extractor": "whole-payload",
    "comparator_hint": "binary-projection"
  },
  "subset_R": [
    { "node": "n0", "seq": 0, "exchange_ref": "1000.1",
      "request": { "method": "GET", "locator": "https://api.example.com/csrf", "operation": null },
      "is_mutation": false, "in_R_reason": "produces DERIVED x-csrf-token needed by n1" },
    { "node": "n1", "seq": 2, "exchange_ref": "1000.3",
      "request": { "method": "POST", "locator": "https://api.example.com/graphql", "operation": "ApplyTemplate" },
      "is_mutation": true,  "in_R_reason": "produces PRODUCED jobId; mutation in R" },
    { "node": "n2", "seq": 3, "exchange_ref": "1000.4",
      "request": { "method": "GET", "locator": "https://api.example.com/job/{jobId}", "operation": null },
      "is_mutation": false, "in_R_reason": "readiness poll; source of the export" },
    { "node": "n3", "seq": 4, "exchange_ref": "1000.5",
      "request": { "method": "GET", "locator": "https://api.example.com/job/{jobId}/pdf", "operation": null },
      "is_mutation": false, "in_R_reason": "produces the golden (r2)" }
  ],
  "values": [
    {
      "node": "n1", "carrier": "json-ptr:/variables/id", "bucket": "INPUT",
      "ref": "r0",
      "source": { "kind": "step_input", "ref": "r0" },
      "binds_as": "INPUT(r0)",
      "evidence": { "co_varies_with_input": true, "unique": true, "entropy": "high", "runs_confirmed": 2 },
      "all_matching_buckets": ["INPUT"]
    },
    {
      "node": "n1", "carrier": "header:x-csrf-token", "bucket": "DERIVED",
      "source": { "kind": "response", "src_node": "n0", "src_path": "json-ptr:/token" },
      "binds_as": "DERIVED(n0, json-ptr:/token)",
      "evidence": { "co_varies_with_input": false, "unique": true, "entropy": "high", "runs_confirmed": 2 },
      "all_matching_buckets": ["DERIVED"]
    },
    {
      "node": "n2", "carrier": "path-tmpl:jobId", "bucket": "PRODUCED",
      "source": { "kind": "response", "src_node": "n1", "src_path": "json-ptr:/data/applyTemplate/jobId" },
      "binds_as": "DERIVED(n1, json-ptr:/data/applyTemplate/jobId)",
      "evidence": { "co_varies_with_input": true, "unique": true, "entropy": "high", "runs_confirmed": 2, "src_is_mutation": true },
      "all_matching_buckets": ["PRODUCED"]
    },
    {
      "node": "n1", "carrier": "json-ptr:/variables/idempotencyKey", "bucket": "COMPUTED",
      "recipe": { "kind": "generator", "fn": "uuid_v4", "args": [] },
      "binds_as": "COMPUTED(uuid_v4)",
      "evidence": { "co_varies_with_input": false, "unique": true, "entropy": "high", "differs_across_runs": true },
      "all_matching_buckets": ["COMPUTED"]
    },
    {
      "node": "n1", "carrier": "json-ptr:/variables/format", "bucket": "CONST",
      "value": "pdf",
      "binds_as": "CONST(\"pdf\")",
      "evidence": { "stable_across_runs": true, "stable_across_ambient": true },
      "all_matching_buckets": ["CONST"]
    }
  ],
  "steps": [
    { "op": "ISSUE",   "node": "n0" },
    { "op": "BIND",    "ref": "r_csrf", "src": "n0", "path": "json-ptr:/token" },
    { "op": "COMPUTE", "ref": "r_idem", "recipe": { "kind": "generator", "fn": "uuid_v4", "args": [] } },
    { "op": "ISSUE",   "node": "n1" },
    { "op": "POLL",    "read": "n2", "predicate": { "over": "body-field", "path": "json-ptr:/status", "equals": "COMPLETE", "timeout_s": 60, "interval_s": 2 } },
    { "op": "ISSUE",   "node": "n3" },
    { "op": "ASSERT",  "predicate": { "over": "status-code", "equals": 200 } }
  ],
  "control_flow": {
    "polls":   [{ "read": "n2", "predicate": { "over": "body-field", "path": "json-ptr:/status", "equals": "COMPLETE" } }],
    "repeats": [],
    "retries": []
  },
  "unexplained": [],
  "contested": [],
  "dangling_produced": [],
  "gate": {
    "G1_self_contained": { "pass": true,  "reason": "all values bucketed; no unexplained/contested/dangling" },
    "G2_no_fixed_wait":  { "pass": true,  "reason": "readiness via POLL on n2; zero fixed sleeps" }
  },
  "verdict": "API-CANDIDATE",
  "bail": null
}
```

Field rules (each ties to a DESIGN gate/hole):

- `golden_source` — S2 result. `found:false ⇒ BAIL-1` (client-rendered; `bail` set). `mode ∈ {single,
  assembled}`; `assembled` lists every contributing `exchange_seqs` (1:N streaming / pagination golden) and
  `comparator_hint` becomes `assembled`. `comparator_hint ∈ {byte-eq, canonical-json, normalized,
  extracted, assembled, binary-projection}` — a *hint*; the operator-frozen comparator lands in
  `verify_receipt.json` (§4).
- `subset_R[]` — the causally-closed minimal request set, in capture order. `node` ids are `n<seq-rank>`,
  reused by `values[]` and `steps[]`. `exchange_ref` is the `requestId` from `paired.jsonl`. `in_R_reason`
  is the closure justification (a call is in R only if some required field of an in-R call derives from it).
- `values[]` — one entry per **value in every request of R** (request values only; response values are
  *sources*). Exactly one `bucket` is chosen, but `all_matching_buckets` lists **every** bucket it matched:
  - length > 1 (e.g. `["INPUT","PRODUCED"]`) ⇒ the value is **CONTESTED** and also appears in `contested[]`
    (never first-match-resolved).
  - `bucket` ∈ §-conventions. `source` shape varies by kind:
    `{kind:"step_input", ref}` | `{kind:"response", src_node, src_path}` | `{kind:"const", }` |
    `{kind:"ambient", path}` | `{kind:"generator"/"transform" → in `recipe`}`.
  - `DERIVED`/`INPUT` require `evidence.unique && entropy=="high" && co_varies_with_input` (the latter for
    INPUT) across `runs_confirmed >= 2`; low-cardinality coincidences are rejected (stay UNEXPLAINED or
    CONTESTED).
  - `PRODUCED` = a DERIVED whose `evidence.src_is_mutation == true`; its `src_node` MUST be in `subset_R`.
  - `AMBIENT-INPUT` = an otherwise-CONST value that also appears in auth/session context; `source.kind ==
    "ambient"`, threaded from auth, not hardcoded.
  - `COMPUTED` carries `recipe` (`{kind:"generator"|"transform", fn, args}`); a `generator` (uuid/nonce/
    timestamp/hmac) sets `evidence.differs_across_runs`; a `transform` sets `args` as refs/paths it reads.
    Proof-obligated (§4 perturbs it).
  - `UNEXPLAINED` = matched no bucket; also appears in `unexplained[]`.
  - `DERIVED-BY-SELECTION` (array-sourced, no stable non-positional key) is recorded with
    `source.kind=="response"` plus `"selection": {"by":"positional","rejected":true}` and is treated as
    UNEXPLAINED until a predicate is recovered.
- `steps[]` — the ordered runnable program. `op ∈ {ISSUE, BIND, COMPUTE, POLL, REPEAT, RETRY, ASSERT}`,
  mirroring DESIGN §2.4 exactly:
  - `ISSUE {node}` — fire an R call.
  - `BIND {ref, src, path}` — thread a DERIVED/PRODUCED value into run-scope.
  - `COMPUTE {ref, recipe}` — run a COMPUTED recipe.
  - `POLL {read, predicate}` — async readiness; **never** a fixed sleep.
  - `REPEAT {node, until_predicate, accumulate}` — cursor/pagination loop; `accumulate` names the
    run-scope key the page results append to.
  - `RETRY {node, on_retryable_status: [int], max_attempts}` — bounded transient-failure retry.
  - `ASSERT {predicate}` — invariant check.
  - `predicate` is **over any repeatable observation**: `{ "over": "status-code"|"resource-presence"|
    "header-value"|"body-field", "path"?: "<extractor>", "equals"?: <v>, "present"?: bool, "timeout_s"?:
    int, "interval_s"?: int }`.
- `control_flow` — a denormalized index of the POLL/REPEAT/RETRY steps (so G2 and `prove_runner` read them
  without re-walking `steps[]`).
- `unexplained` / `contested` / `dangling_produced` — the three MISS lists. Each entry is
  `{ "node", "carrier", "reason" }` (+ `"all_matching_buckets"` for contested, `"src_node"` for dangling).
  **Any non-empty list ⇒ G1 FAIL.**
- `gate` — the two mechanical gates checked here. `G1_self_contained` (end of S3) and `G2_no_fixed_wait`
  (end of S4), each `{ "pass": bool, "reason": str }`.
- `verdict` — `"API-CANDIDATE"` (G1∧G2 pass → proceed to auth + prove) | `"KEEP-UI"` (any gate FAIL/BAIL).
- `bail` — `null` or `{ "code", "reason" }` (BAIL-1/2/3 surface here; BAIL-4 at auth, BAIL-5 at prove).

---

## 4. `verify_receipt.json` — the empirical proof (`prove_runner.py` + `verify_equivalence.py`, S6; NEW wrapper over existing)

`prove_runner.py` owns the N≥2 loop and the fresh/isolated/boundary/COMPUTED-perturbation **instance
selection**; it invokes the frozen comparator (`verify_equivalence.py` emits the per-comparison verdict
block — §4.1, FROZEN existing) once per instance×run and reduces to a single verdict. **Frozen contract:**
`verdict == "PROVEN"` only if every instance×run is a MATCH under the **frozen** comparator on
mutually-isolated, boundary-spanning instances; anything else ⇒ KEEP UI.

```json
{
  "schema": "verify_receipt/v1",
  "segment_id": "s0",
  "verdict": "PROVEN",
  "comparator": {
    "kind": "NORMALIZED",
    "frozen": true,
    "tag": "pdf",
    "field_mask": ["/metadata/CreationDate", "/metadata/ModDate"],
    "projection": null,
    "threshold": 0.9,
    "mask_valid": true
  },
  "api_instance": "run-in-page --contract 1 ... (command.sh)",
  "golden_instance": "UI export on each held-out instance",
  "runs": [
    {
      "instance": { "id": "inv_777", "role": "fresh",    "tenant": "org_777", "isolated_from": ["org_888"], "boundary": "nominal" },
      "n": 2,
      "results": [
        { "run": 1, "api": "/tmp/api_out.1.pdf", "golden": "/tmp/golden_fresh.1.pdf", "match": true,
          "comparison": { "verdict": "MATCH", "method": "pdf-text-jaccard", "overlap": 0.98, "threshold": 0.9 } },
        { "run": 2, "api": "/tmp/api_out.2.pdf", "golden": "/tmp/golden_fresh.2.pdf", "match": true,
          "comparison": { "verdict": "MATCH", "method": "pdf-text-jaccard", "overlap": 0.97, "threshold": 0.9 } }
      ]
    },
    {
      "instance": { "id": "inv_888", "role": "boundary", "tenant": "org_888", "isolated_from": ["org_777"], "boundary": "large-paginating" },
      "n": 2,
      "forces": { "pagination": true, "perturbs_computed": ["json-ptr:/variables/idempotencyKey"] },
      "results": [
        { "run": 1, "api": "/tmp/api_out.b1.pdf", "golden": "/tmp/golden_b1.pdf", "match": true,
          "comparison": { "verdict": "MATCH", "method": "pdf-text-jaccard", "overlap": 0.95, "threshold": 0.9 } },
        { "run": 2, "api": "/tmp/api_out.b2.pdf", "golden": "/tmp/golden_b2.pdf", "match": true,
          "comparison": { "verdict": "MATCH", "method": "pdf-text-jaccard", "overlap": 0.96, "threshold": 0.9 } }
      ]
    }
  ],
  "coverage": {
    "instances": 2,
    "min_runs_each": 2,
    "fresh_not_build_instance": true,
    "mutually_isolated": true,
    "boundaries_spanned": ["nominal", "large-paginating"],
    "forces_pagination": true,
    "perturbs_every_computed": true,
    "mask_fields_constant_across_runs": true
  },
  "bail": null
}
```

- `verdict` — `"PROVEN"` (G3 fully satisfied → ship API) | `"FAILED"` (any MATCH missing → `bail` BAIL-5,
  KEEP UI) | `"UNCOVERED"` (a coverage obligation unmet → KEEP UI, no false ship).
- `comparator` — the **operator-declared, frozen** comparator keyed to this segment id.
  `kind ∈ {BYTE_EQ, CANONICAL_JSON_EQ, NORMALIZED, EXTRACTED, ASSEMBLED}` (DESIGN §2.5). `frozen:true` is
  required to ship. `field_mask` lists masked extractor paths; `projection` is the extractor for EXTRACTED;
  `threshold` feeds the text/jaccard path of `verify_equivalence.py`. `mask_valid` — every masked field is
  **constant across the varied-input runs** (G3.5); a masked field that varies with input ⇒ `false ⇒ FAILED`.
  For a known-nondeterministic binary container (image/zip/pdf) `projection` is **required** —
  falling through to BYTE_EQ is forbidden.
- `runs[]` — one entry per proof **instance**; each carries `n` (≥2) and the per-run `results`. Each result
  embeds the **raw comparator block** from §4.1. `instance.role ∈ {fresh, isolated, boundary}`; `boundary`
  names the band sampled (`nominal` | `min` | `max` | `empty` | `large-paginating` | `<category>`).
  `forces` records pagination-forcing and which COMPUTED values were perturbed.
- `coverage` — the G3.3/G3.4/G3.5 checklist, all booleans. **All must hold** for `PROVEN`:
  `fresh_not_build_instance`, `mutually_isolated`, `forces_pagination` (if any REPEAT exists),
  `perturbs_every_computed` (if any COMPUTED exists), `mask_fields_constant_across_runs`. `boundaries_spanned`
  must include ≥2 declared bands.
- `bail` — `null` or `{ "code": "BAIL-5", "reason": "<which instance/run diverged, with the comparator block>" }`.

### 4.1 `verify_equivalence.py` comparison block (FROZEN, existing — embedded per result)

`verify_equivalence.py --api … --golden …` prints exactly this object and sets its exit code
(`0=MATCH`, `1=MISMATCH`, `3=INCONCLUSIVE`). `prove_runner.py` captures it verbatim into each
`runs[].results[].comparison`.

```json
{
  "api":    { "path": "/tmp/api_out.pdf",    "bytes": 107201, "sha256": "9f2c…", "magic": "%PDF-" },
  "golden": { "path": "/tmp/golden_fresh.pdf","bytes": 107431, "sha256": "a1b4…", "magic": "%PDF-" },
  "sizeRatio": 0.998,
  "verdict": "MATCH",
  "method": "pdf-text-jaccard",
  "overlap": 0.98,
  "threshold": 0.9,
  "reason": "text token overlap 0.98 >= 0.9"
}
```
`method ∈ {sha256, pdf-text-jaccard, bytes}` (existing); `verdict ∈ {MATCH, MISMATCH, INCONCLUSIVE}`.
A consumer must tolerate the extra `overlap`/`threshold` keys appearing only on the jaccard path.

---

## 5. `run-in-page` command / return interface (`run_in_page.py`, S4 runtime; FROZEN existing + POLL/REPEAT/RETRY)

`run-in-page` is resolved **by name on PATH** (never a path into the skill). One `ReplayProgram` step calls
it once; `--contract 1` is the wire version. The **command line** and the **JS return object** are the two
halves of the frozen interface.

### 5.1 Command line (FROZEN, existing)

```
run-in-page --contract 1 [--allow-mutation] [--match <url-substr>] [--out <path>]
            [--vars-json '<json-object>'] [--port 9222] [--timeout 30] [--cdp-wait 15]
            (--js '<expr>' | JS on stdin)
```

- `--contract` — integer; must equal the helper's `CONTRACT_VERSION` (`1`) or exit `4`.
- `--allow-mutation` — required for any fetch the classifier deems `write`/`unknown` (fail-safe).
- `--match` — substring selecting the already-authenticated tab (correct-tab targeting; same-origin
  duplicates pick deterministically, cross-origin is ambiguous → fail loud).
- `--out` — write binary output here (from `download.url` or `dataBase64`).
- `--vars-json` — a JSON **object**; each `{{key}}` in the JS is replaced by the **JSON-encoded** value
  (author the JS WITHOUT wrapping the placeholder in quotes).
- The JS is a single async expression returning the object in §5.2.

### 5.2 JS return object (FROZEN, existing)

```json
{
  "ok": true,
  "status": 200,
  "contentType": "application/pdf",
  "download": { "url": "https://signed.example.com/tmp/abc.pdf?sig=…" },
  "dataBase64": null
}
```

- `ok` — **strong** predicate (status + content-type + a positive shape signal). The helper treats `ok:true`
  as success **only if** `--out` (when given) received a non-empty, type-correct file.
- `download.url` — a **self-authenticating** URL the helper fetches with `urllib` (carries no browser
  cookies — e.g. a pre-signed S3 link). An HTML/login page or wrong magic ⇒ treated as failure.
- `dataBase64` — small inline bytes alternative to `download.url`.
- Any extra fields are echoed into the report verbatim (minus `download`/`dataBase64`).

### 5.3 POLL / REPEAT / RETRY — expressed **inside one JS expression** (the EXTEND)

These are control-flow the UI performed (DESIGN §2.4); the chain reproduces them **inside the single async
JS** the step runs — there is no second helper process and **no fixed `sleep`/`setTimeout(<num>)` used as
readiness**. The shapes below are the canonical authoring forms a generated chain emits; `plan.json.steps`
(§3) is the abstract source these render from.

- **POLL** — re-query a readiness `read` until the predicate holds, bounded by a timeout:
  ```js
  // POLL: status-read until COMPLETE, predicate-driven, bounded — NEVER a fixed sleep
  const t0 = Date.now();
  let status;
  do {
    const r = await fetch(`/job/${jobId}`, { credentials: "include" });
    status = (await r.json()).status;                 // predicate.over = body-field, path = /status
    if (status === "COMPLETE") break;                 // predicate.equals
    await new Promise(s => setTimeout(s, 2000));       // INTER-POLL backoff (interval_s), not a readiness wait
  } while (Date.now() - t0 < 60000);                  // timeout_s
  if (status !== "COMPLETE") return { ok: false, status, reason: "poll timed out" };
  ```
  The backoff `setTimeout` is the **interval between polls**, not a substitute for the readiness check — the
  loop still exits only on the predicate. G2's regex permits `setTimeout` *inside a polling loop that also
  tests a predicate*; a bare `setTimeout` gating the act with no predicate is a FAIL.

- **REPEAT** — follow a continuation signal (cursor / `has_more` / `total > page`) feeding the same call,
  accumulating pages:
  ```js
  // REPEAT: cursor pagination — accumulate until the continuation signal is exhausted
  const items = []; let cursor = null;
  do {
    const r = await fetch(`/list?cursor=${cursor ?? ""}`, { credentials: "include" });
    const page = await r.json();
    items.push(...page.items);                         // accumulate
    cursor = page.next_cursor;                         // until_predicate: next_cursor == null
  } while (cursor);
  ```

- **RETRY** — bounded retry around a call keyed on its **own** retryable status:
  ```js
  // RETRY: bounded transient-failure retry on the act call (idempotency key supplied if non-idempotent)
  let resp, attempt = 0;
  do {
    resp = await fetch(actUrl, { method: "POST", credentials: "include", headers, body });
    if (![502, 503, 429].includes(resp.status)) break; // on_retryable_status
  } while (++attempt < 3);                              // max_attempts (bounded)
  ```

Exit codes (the step branches on them — `0 ⇒ done`, anything else ⇒ UI fallback): **FROZEN, existing**

| Code | Constant | Meaning |
|---|---|---|
| 0 | `OK` | success: `ok:true` and (if `--out`) a non-empty, type-correct file |
| 1 | `FAIL` | ran but `ok:false`, bad output object, or missing/empty `--out` file |
| 2 | `THREW` | JS threw / tab not found / CDP unreachable |
| 3 | `REFUSED_WRITE` | a write/unknown fetch without `--allow-mutation` |
| 4 | `BAD_CONTRACT` | `--contract` ≠ helper version |
| 5 | `USAGE` | no JS, or a `{{var}}` had no `--vars-json` value |

`classify(js)` → `read | write | unknown` is **fail-safe**: anything not provably a read is `unknown` (gated
as a write). A GraphQL `mutation` anywhere, a `DELETE`/`PUT`/`PATCH` literal, a non-literal `method:`, a
plain non-GraphQL `POST`, or a persisted op with no inline text all force `write`/`unknown`.

---

## 6. Cross-stage invariants (what every shape jointly guarantees)

1. **One id space.** `segment_id` (`segments.json`) → `plan.json.segment_id` → `verify_receipt.json.segment_id`
   are the same string. `ValueRef.id` (`segments.json.handoffs`) → `segment_inputs.json.bindings[].ref` →
   `plan.json` `source.ref` / step `ref` are the same string. `node` ids (`plan.json.subset_R`) are local to
   one plan and referenced by its own `values[]`/`steps[]`.
2. **No UNEXPLAINED, no DROPPED.** `plan.json.unexplained == [] && contested == [] && dangling_produced == []`
   AND every async gap has a POLL and every continuation a REPEAT — the `gate.G1`/`gate.G2` booleans are the
   mechanical witnesses. A failing witness forces `verdict:"KEEP-UI"`.
3. **Proof, not production.** `verify_receipt.json.verdict == "PROVEN"` requires every instance×run MATCH
   under the **frozen** comparator on **mutually-isolated, boundary-spanning** instances that **force
   pagination** (if any REPEAT) and **perturb every COMPUTED** — `coverage` is the checklist.
4. **Permissive consumers.** Every reader tolerates unknown extra keys; absent ≡ `null`; a producer may add
   fields without breaking a frozen consumer.
5. **Bail is success.** Any `bail` object (`BAIL-1..5`) or a `KEEP-UI`/`FAILED`/`UNCOVERED` verdict is the
   method correctly proving "keep the UI here," not a tool failure.
