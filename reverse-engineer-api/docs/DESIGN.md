# Reverse-Engineer UI → API — Definitive Design

App-agnostic. Protocol (REST/GraphQL/RPC/multipart/binary), artifact (JSON/PDF/CSV/image/archive/stream), auth scheme (cookie/Bearer/HMAC/refresh), and target app are *instances* plugged into the same machine. Nothing in the algorithm branches on any of them. Every critical rule is a **tool-enforced gate**, never prose — because the settled lesson is that *prose does not bind the executing agent*.

---

## 1. Problem and spec

A **UI workflow** `W` is an ordered list of actions that turns an **input** into an **output**. This skill synthesizes a program that reproduces that output for *future* inputs via **direct API calls**, OR proves it cannot and keeps the UI.

The **unit of API-ification is a SEGMENT** of `W` — a maximal contiguous run of data-work actions. Never the whole workflow; never a single action. Misidentifying this unit is the documented root cause of every prior failure. A workflow yields **0, 1, or many** segments; each is API-ified-or-kept independently, then recombined.

### The one rule

> **Replay the capture faithfully, and let the content proof be the judge.**

Transcribe every call the UI made; **parameterize** what the classifier can source (const / input / derived / produced / computed) and **replay verbatim** what it cannot; reproduce every wait / loop / retry the UI performed (or retry the act when the wait was out-of-band). The **binding** gates are three: an HTTP response actually carries the artifact (else **BAIL-1**, client-rendered), auth is reproducible (else **BAIL-4**), and the replay reproduces the golden **byte-equal on fresh / isolated / boundary instances** (**PROVE / G3** — else keep UI). Value-completeness and readiness-completeness are now **advisory signals** that feed those gates, *not* up-front keep-UI bails: the settled lesson is that "explain every value first" is a weak proxy that false-bails on real-world captures (constants, framework boilerplate, telemetry), while "does it actually reproduce the bytes?" is the real test. **Bailing (keep UI) is a correct, expected, frequent outcome — not a failure.**

---

## 2. Data model (core structures, app-agnostic)

Six records. Nothing else is needed.

```
Nature = FUZZY | NAVIGATE | DATA_WORK | COMPREHEND

Action  = { i, text, nature,
            produces:[ValueRef], consumes:[ValueRef] }   # one line of W

ValueRef = { id, shape, extractor }   # a typed handle flowing BETWEEN regions
                                      # shape = what a consumer may assume; extractor = how to read it
```

### 2.1 Workflow, region, segment

```
Workflow W = [Action]                          # ordered, i strictly increasing

Region = UiRegion   { actions, produces:[HandoffSpec] }          # FUZZY/NAVIGATE/COMPREHEND
       | ApiSegment { id, actions,                               # maximal DATA_WORK run
                      consumes:[HandoffSpec], produces:[HandoffSpec],
                      inputs:{ref -> value}, golden:Artifact, trace:Trace }

HandoffSpec = { ref:ValueRef, shape, extractor, origin }
              origin ∈ STEP_INPUT | PRIOR_UI | PRIOR_SEGMENT
```

**Boundary law.** A segment is a *maximal* contiguous run of `DATA_WORK`. Its boundaries are `FUZZY`/`COMPREHEND` actions. A **pure `NAVIGATE`** (no mutation, no irreproducible effect) is *absorbed into the adjacent segment* so it never splits a causal chain (resolves hole CON-4a). A `NAVIGATE` that fires a mutation is reclassified `DATA_WORK` (resolves EC-A2).

### 2.2 Trace (the wire, generalized)

```
Trace T = [Exchange]                           # ordered by capture time

Exchange = { seq, request:Request,
             responses:[Response],             # 1:N — a request may yield a stream of frames
             t_sent, t_recv, is_mutation }

Request  = { method, locator,                  # URL/path/op-name — protocol-neutral
             carriers:[ValueSite] }            # every value, addressed by a content-type extractor
Response = { status, headers, body, readiness:ReadinessSignal|None }

ValueSite = { extractor, path, bytes }         # extractor ∈ JSON-ptr|form-key|header|path-tmpl|
                                               #            multipart-part|registered-binary-decoder|WHOLE-PAYLOAD
```

**1:N responses** (streaming/long-poll/chunked) are first-class: a request maps to an *ordered list* of response messages; pairing never truncates to the first frame (resolves GEN-1, GEN-9). **Opaque/binary bodies** are addressed at `WHOLE-PAYLOAD` granularity when no structured extractor applies — un-introspectable never auto-means UNEXPLAINED (resolves GEN-2).

### 2.3 The five value buckets (+ the residual)

Every value in every **request** of `R` gets exactly one bucket. Response values are *sources*, not classified.

| Bucket | Test | Handling |
|---|---|---|
| **CONST** | identical (up to canonical form) across runs that **vary input AND ambient identity** | hardcode |
| **INPUT** | equals a segment input, confirmed by co-variation across ≥2 differing inputs | parameterize |
| **DERIVED** | equals a **unique, high-entropy** value in an earlier in-R response, confirmed by co-variation | thread (DAG edge) |
| **PRODUCED** | a DERIVED whose source request `is_mutation` | the mutation **must be in R** |
| **COMPUTED** | client-produced by a reproducible **recipe** (transform of input/state, OR a fresh generator — UUID/nonce/timestamp/HMAC) | run the recipe; **proof-obligated** |
| **UNEXPLAINED** | none of the above | **replayed VERBATIM** (advisory, reported) — PROVE judges; a genuinely-irreproducible value (signature/captcha) is caught earlier by the bail-scan, a wrong/hardcoded one by PROVE on a fresh instance |

`COMPUTED` is widened to include values minted from **entropy** (fresh idempotency key, nonce), not only transforms of captured state (resolves FN-1). It is recognised by the value's **shape** — a random **UUID v4** sourced from no response is client-minted regardless of the carrier name (`operationId` reads the same as `idempotencyKey`) — not only by a generator hint in the field name. It carries a `recipe` and is a first-class proof obligation, never a silent flag (resolves FP-5, CLASS-5).

### 2.4 Call-DAG and replay program

```
CallDAG = { nodes:[CallNode], edges:[DataEdge], waits:[WaitEdge], loops:[LoopEdge] }

ValueSlot = CONST(bytes) | INPUT(ref) | DERIVED(src_node, src_path)
          | COMPUTED(recipe)            # recipe = pure fn over earlier slots/inputs/generators

ReadinessPredicate = over ANY repeatable observation:
                     status-code | resource-presence(404→200) | header-value | body-field
                     (NOT only a body status field)            # resolves GEN-4

ReplayProgram = { segment_id, signature:(inputs, output), auth:AuthCarrier,
                  steps:[Step], comparator:Comparator }

Step = ISSUE(node) | BIND(ref,src,path) | COMPUTE(ref,recipe)
     | POLL(read, predicate)            # async readiness — never sleep(k)
     | REPEAT(node, until_predicate, accumulate)   # cursor/pagination loop
     | RETRY(node, on_retryable_status, bounded)   # transient-failure robustness
     | ASSERT(predicate)
```

`REPEAT` (the dual of POLL) and `RETRY` are control-flow primitives, not afterthoughts — pagination and transient-retry are *behavior the UI performed* and dropping them violates the one rule (resolves FN-3, FN-5, FP-6).

### 2.5 Artifact and comparator

```
Artifact   = opaque bytes + type-tag
Comparator = BYTE_EQ | CANONICAL_JSON_EQ | NORMALIZED(field_mask) | EXTRACTED(projection)
             | ASSEMBLED(reduce over a SET of responses)   # streaming/pagination goldens
```

`content_equal(a,b) := golden.comparator(a,b)`. The comparator is operator-declared **after partition** (keyed to real segment ids), frozen, and itself gated (§4, G3-mask). For nondeterministic binary containers (image/zip/pdf), a projection extractor is *required*; falling through to BYTE_EQ on a known-nondeterministic type is forbidden (resolves GEN-8).

---

## 3. Algorithm — stage by stage

```
S0 PARTITION   (once)         W                        → [Region], segment ids, handoff graph
S1 CAPTURE     (per segment)  segment, clean start     → Trace T, golden G, segment_inputs.json
   per segment, in order:
S2 SUBSET                     T, G                      → minimal causal R ⊆ T
S3 CLASSIFY                   R(full-trace), inputs     → every value bucketed; unexplained[]
S4 CONTROL-FLOW               classified R              → ordered steps + POLL/REPEAT/RETRY
S5 AUTH                       R                         → AuthCarrier + re-source recipe
S6 GATE                       ReplayProgram             → API-ADD | KEEP-UI
S7 RECOMBINE   (once)         [decision]                → executable Plan with typed handoffs
```

**S0 PARTITION** — assign `nature` (verb prior, **confirmed against capture**: an action is `DATA_WORK` if a mutation fired during it; needs a POLL if a status read repeated ≥2× during it — text keywords are a prior, never the decision, resolving GEN-6). Coalesce maximal `DATA_WORK` runs into `ApiSegment`s with stable ids. Absorb pure `NAVIGATE`s. Record every handoff as a typed `HandoffSpec` (including `PRIOR_SEGMENT`, so API→API hand-offs exist — resolves CON-1, CON-4). **Contract:** no `DATA_WORK` action sits outside a segment; every cross-region value is a declared, typed ref. **Runs before capture** so comparators/goldens key to real ids (resolves CON-6).

**S1 CAPTURE** — from a **clean start relative to the segment's declared inputs** (preconditions established, segment's own effects absent — resolves GEN-10), drive the whole segment, record `T` (full request/responses + headers + timestamps) and `G`. Emit `segment_inputs.json` binding each handoff ref to the **concrete value it took during capture** (resolves CON-3). Capture **≥2 runs** with *varied inputs* (needed to separate CONST/INPUT/COMPUTED). **Contract:** `T` covers the whole segment from clean state; `G` is what a fresh run truly produces; inputs are bound. *(The Metaview "setup-in-PREP" failure is forbidden here: an applied-template mutation must be inside `T`.)*

**S2 SUBSET** — locate G's source by **IDENTITY**, not by content-type label: the response whose body *is*, *contains* (incl. **base64-encoded inside JSON**), or *assembles-into* G — matched by the artifact's **sha256 / type-magic**, so a binary smuggled in a JSON field (content-type `application/json`) is found, not wrongly declared client-rendered. The sha match is tag-free, so it works even when the capture recorded no artifact type. If G's bytes appear in no response nor any ordered assembly → **BAIL-1** (client-rendered). Seed `R`, then **backward-close transitively over request dependencies** (not just G): pull in every call producing a non-trivial value that any in-R request needs (keeps the CSRF-token-fetch GET — resolves FN-2), **but only within the golden's own registrable-domain** — third-party analytics/ads/RUM beacons share trace-ids with the app and would otherwise bridge into R as hundreds of UNEXPLAINED values (real-capture denoise). A call is noise only if no required field of any in-R call derives from it. **Contract:** R is causally closed and on-site.

**S3 CLASSIFY** — *runs against the full trace with strict capture-time ordering, then drives R* (resolves CLASS-7's circularity: classify → close, not close → classify). Apply §2.3 buckets. Key sub-rules, each resolving a hole:
- Compute **all** matching buckets; a value matching both INPUT and PRODUCED is **CONTESTED**, not first-match-resolved (resolves CLASS-1, FP-1).
- DERIVED/INPUT require **uniqueness + high entropy + co-variation** across the ≥2 varied runs; low-cardinality coincidences (small ints, `default`, booleans) cannot establish an edge (resolves FP-1, CLASS-3/4/8).
- CONST requires stability across runs that **vary ambient identity**; an otherwise-CONST value that also appears in auth/session context (org/tenant id) is reclassified **AMBIENT-INPUT**, threaded from auth, not hardcoded (resolves CLASS-2).
- Array/collection-sourced DERIVED is valid only if the selection is unambiguous (unique value, stable non-positional key); positional indices alone are rejected → `DERIVED-BY-SELECTION`, recover the predicate or contest (resolves CLASS-4).
- **Contract (advisory):** unexplained / CONTESTED / dangling-PRODUCED are **reported, not fatal** — each is replayed verbatim and surfaced so the operator can spot a mis-identified per-instance input; **PROVE (G3) is the arbiter**. Only **BAIL-1** (golden in no response) is a classify-level keep-UI.

**S4 CONTROL-FLOW** — topo-sort R by DataEdges. Insert **POLL** for async gaps (predicate over any repeatable observation), **REPEAT** where a response field is a continuation signal (cursor/has_more/total) fed back into the same call, **RETRY** around mutating/act calls keyed on their own retryable-status. When a repeated read has **no pinnable readiness signal** (the wait was out-of-band — e.g. a websocket/SSE channel the UI listened on), do NOT fabricate a poll: fall back to **RETRY the terminal act until it yields the artifact**, bounded. **Zero fixed sleeps.** **Contract:** runnable; every wait/loop/retry is predicate-driven or act-retried.

**S5 AUTH** — auth material is **ordinary Values run through the same classifier**: static → CONST/secret-ref; session token → PRODUCED (mint call in R); per-request signature → COMPUTED(signing recipe); refresh flow → PRODUCED mint (resolves GEN-5). **Contract:** carrier re-sourceable at run time.

**S6 GATE** — §4. **S7 RECOMBINE** — §3.1.

### 3.1 Recombine (the missing executor)

```
Plan = ordered [Region]
Executor: for each region in order →
  run it (UiRegion = agent; ApiSegment = ReplayProgram),
  validate each `produces` against its HandoffSpec.shape (fail fast),
  bind into a shared run-scope keyed by ValueRef,
  supply the next region's `consumes` from that scope.
```

INV-1 self-containedness is checked **at the workflow level** here, not only per-segment: a value un-sourced in segment `S2` but declared `produces` by upstream `S1`/UI-region is INPUT, not UNEXPLAINED (resolves CON-1, CON-2, CON-4, CON-5). The API→COMPREHEND seam is symmetric: a segment publishes its golden in the comparator-pinned shape the downstream reader expects (resolves CON-5).

---

## 4. The gates (four invariants as mechanical checks)

Each gate is `input → {PASS | FAIL→keep-UI | BAIL→keep-UI}`. **No agent prose overrides a gate.**

**G1 — VALUES BUCKETED** *(end of S3, re-checked at S7) — ADVISORY, not a keep-UI gate*
Input: `replay_plan.json` + run-scope. Reports: which values are `∈ {CONST, INPUT, AMBIENT-INPUT, DERIVED, PRODUCED, COMPUTED}` and which are UNEXPLAINED / CONTESTED. A miss is **replayed verbatim and reported** (so the operator can spot a mis-identified per-instance input and declare it), **not** a keep-UI bail — a genuinely-irreproducible value (signature/captcha) is caught earlier by the bail-scan, and a wrong/hardcoded one by PROVE (G3) on a fresh instance. *(Demoted from a fatal gate: "explain every value up front" false-bailed on real captures full of constants / framework boilerplate / telemetry.)*

**G2 — READINESS** *(end of S4) — ADVISORY, not a keep-UI gate*
Input: steps + `command.sh`. Reports: zero `sleep`/`setTimeout(<num>)` used as readiness; every async gap (UI waited / status read ≥2× / 404→200) has a `POLL`; every continuation signal has a `REPEAT`. An async gap with **no pollable HTTP observation** is **not** a keep-UI bail — the act is **retried until the artifact appears** (out-of-band/websocket waits aren't pollable), and PROVE (G3) catches a race. A fixed sleep that gates the act remains forbidden in the authored `command.sh`.

**G3 — INV-3 PROVEN** *(S6)* — the empirical backstop, hardened against same-instance *similarity*:
Input: `command.sh`, held-out instances, fresh goldens. Checks, all required:
1. Run **N≥2** times; any missing output → FAIL.
2. `content_equal` holds each run, with the **frozen** comparator (no weakening — resolves FP-3).
3. **Held-out is not the build instance** AND **proof instances are mutually isolated** (different tenant/account, interleaved or with an adversarial state perturbation between them) — kills the shared-state false-pass (resolves FP-4).
4. **Domain coverage:** proof instances must span declared input **boundaries** (min/max, empty/large, each category) and include at least one input that **forces pagination** (total > page-size) and one that **perturbs every COMPUTED value** — kills the unsampled-band, truncation, and stale-recipe false-passes (resolves FP-2, FP-5, FP-6, CLASS-5).
5. **Mask validity:** every comparator-masked field must be constant across the varied-input runs; a masked field that *varies with input* is illegal to mask → FAIL (resolves FP-3).
**FAIL/uncovered → keep UI.** *(This is the gate that rejects the 45 KB canary and the masked-headline-number bug.)*

**G4 — INV-4 AUTH-REPRODUCIBLE** *(end of S5)*
Input: an R request + live context. Check: dry-run the carrier recipe in a fresh context (cookie-session, value-as-Bearer, **signature-recipe, or refresh-mint** — resolves GEN-5); a guarded call returns non-401/403. **FAIL** (unreadable/device-bound) → **BAIL-4**.

**Optional bounded code cross-check** *(trigger = an UNEXPLAINED or CONTESTED value, or an unsampled branch G3 flags)*: the frontend code is the **analytical** ground truth (intended branches/transforms). Its only jobs: resolve *missed-call vs COMPUTED recipe*, and reveal input-conditioned branches so G3 samples them. **Minified = low-fidelity → it INFORMS, never OVERRIDES**; any recipe it proposes must still survive G3. Bounded effort budget; an unresolved value is simply **replayed verbatim** and left for PROVE (G3) to judge.

---

## 5. Bail taxonomy (every keep-UI condition + detection)

| Code | Condition | Detected by | Invariant |
|---|---|---|---|
| **BAIL-1** | G's bytes appear in no response nor any ordered assembly → client-rendered | S2 G-source check (by sha256/type-magic identity) | INV-1 |
| ~~BAIL-2~~ → **advisory** | Value UNEXPLAINED / CONTESTED | G1 reports it; **replayed verbatim**, PROVE judges | INV-1 |
| ~~BAIL-3~~ → **advisory** | Readiness out-of-band (push-only, no repeatable HTTP read) | G2 reports it; **act is retried**, PROVE judges | INV-2 |
| **BAIL-4** | Credential unreadable/un-re-sourceable (device/origin-bound, non-extractable key) | G4 | INV-4 |
| **BAIL-5** | `content_equal` fails on a fresh/isolated/boundary instance | G3 | INV-3 |

A bail is the method correctly proving "keep the UI here." The **keep-UI** exits are **BAIL-1, BAIL-4, BAIL-5**; BAIL-2 and BAIL-3 were **demoted to advisory** (reported, never an exit) because the question "does the replay reproduce the bytes?" (PROVE) subsumes them without the false alarms. A `Plan` may be all-UI, all-API, or mixed.

---

## 6. Holes and resolutions (every critical/major hole, folded or accepted)

**False-positive (accepted-into-design):**
- **FP-1 coincidental DERIVED** → DERIVED requires uniqueness + high entropy + co-variation; collisions → CONTESTED (§3 S3).
- **FP-2 unsampled input-band branch** → G3.4 boundary coverage; code cross-check enumerates branches (§4).
- **FP-3 mask swallows the answer** → G3.5 mask-validity + frozen comparator (§4).
- **FP-4 shared-state proof instances** → G3.3 mutual isolation (§4).
- **FP-5 unverifiable COMPUTED recipe** → COMPUTED is proof-obligated; G3.4 perturbs it; unperturbable COMPUTED = UNEXPLAINED-equivalent → bail (§2.3, §4).
- **FP-6 pagination truncation on small golden** → REPEAT primitive + G3.4 forces a paginating input; advertised total/next unfollowed → INV-1 dropped-behavior FAIL (§2.4, §4).

**False-negative (folded):**
- **FN-1 fresh nonce/idempotency key** → COMPUTED widened to entropy-minted generators (§2.3).
- **FN-2 CSRF token dropped by subset** → transitive request-dependency closure (§3 S2).
- **FN-3 pagination loop** → REPEAT primitive (§2.4).
- **FN-4 cross-segment id is UNEXPLAINED** → typed `PRIOR_SEGMENT` handoff + workflow-level INV-1 (§2.1, §3.1).
- **FN-5 transient-retry not modeled** → RETRY primitive; non-idempotent + no key → flag, don't silently retry (§2.4).

**Classification (folded):** CLASS-1 contested-not-first-match; CLASS-2 ambient-input tenant guard; CLASS-3/4 transform-vs-raw + selection predicate; CLASS-5 perturb-every-COMPUTED; CLASS-6 client-minted-opaque positive test (high-entropy + load-bearing + unmatched ⇒ COMPUTED nonce, else stays UNEXPLAINED); CLASS-7 classify-full-trace-then-close; CLASS-8 low-cardinality weak-evidence.

**Genericness (folded):** GEN-1 assembled golden; GEN-2 pluggable extractors + WHOLE-PAYLOAD; GEN-3 canonical-form request matching; GEN-4 readiness over any observation; GEN-5 auth-as-classified-value; GEN-6 trace-grounded partition; GEN-8 per-type comparator (no byte-eq fallthrough); GEN-9 1:N exchanges; GEN-10 clean-start-relative-to-segment-inputs.

**Contracts (folded):** CON-1 explicit RECOMBINE/Executor; CON-2 typed UI↔API handoff (shape + extractor, validated at runtime); CON-3 capture emits `segment_inputs.json`; CON-4 pure-NAVIGATE absorbed + PRIOR_SEGMENT bucket; CON-5 symmetric API→COMPREHEND output handoff; CON-6 partition-before-capture so comparators key to real ids.

**Accepted limits (documented, not eliminable):**
- **G3 residual** (a third instance outside sampled boundaries could still diverge): empirical proof is inherently sampled. Mitigation = boundary + isolation + COMPUTED-perturbation + pagination coverage; for high-stakes segments widen held-out sampling. The verdict states "proven over the *declared* input domain," and a shipped program **restricts its declared domain to the sampled bands** when code-enumeration is unavailable.
- **Output-equivalence, not process-equivalence:** PROVE (G3) checks the replay reproduces the golden **bytes**, not that it makes the **same calls** the UI did. So the build step can reinvent the workflow (a different wait, an extra call) and still pass as long as the output matches — e.g. the Metaview chain regenerated the summary **twice** (a self-invented "force a detectable operation" workaround for its fabricated poll) where the UI generates **once**, which then made reuse slow and timeout-prone. Mitigation: prefer faithful transcription of the captured call sequence and **do not author logic for states you did not capture**. A process-faithfulness check (does the API chain issue the operations the trace did?) is a candidate future gate on top of output-PROVE.

---

## 7. Operator view (non-CS tick-box; no internals edited)

Each box is a command with a binary outcome. A "→ KEEP UI" exit is the system working.

```
TEACH <skill>/<STEP>
[ ] 0 Partition     partition.py --step steps/<STEP>.md     → segments==0? KEEP UI, done.
[ ] 1 Capture WHOLE capture_cdp.py --start … do the ENTIRE segment, clean, ≥2 varied inputs … --stop
                    (writes trace, golden, segment_inputs.json)
[ ] 2 Analyze       analyze.py --run .o11y/run --match <url-bit>
[ ] 3 Bail-scan     detect_replayable.py --run .o11y/run    → exit 3? KEEP UI (signed/anti-bot).
[ ] 4 Classify      classify_values.py --run .o11y/run --plan plan.json --inputs segment_inputs.json
      → verdict == "API-CANDIDATE" (only BAIL-1 = golden client-rendered stops here). UNEXPLAINED values are advisory/replayed-verbatim; if a CHANGING id is reported UNEXPLAINED, declare it as the per-instance input and re-capture. Auth (box 5) + PROVE (box 7) are the real gates.
[ ] 5 Auth (INV-4)  probe_auth.py --request req.json        → case 3 / working:false? KEEP UI.
[ ] 6 Build         author command.sh from plan.json (setup → POLL/REPEAT → act → return artifact)
[ ] 7 PROVE (INV-3) prove_runner.py --command command.sh --instances <fresh,isolated,boundary> --runs 2
      → MATCH on all (fresh, isolated, boundary, COMPUTED-perturbed)? else KEEP UI.
[ ] 8 Write         teach_insert.py --step steps/<STEP>.md --header "<provenance · validated:yes>" --command command.sh
      (only on box-7 MATCH; KEEP UI ⇒ do NOT run)
[ ] 9 Discipline    git diff --name-only → MUST be only steps/<STEP>.md (else git checkout it)
RESULT: api-added (all PASS) | kept-ui (any BAIL/FAIL) — both correct.
```

Operator never judges a value; the gates decide. Only human inputs are the **clean ≥2-run capture** and the **frozen per-segment comparator**.

---

## 8. Build map (generic scripts/gates; exist vs new)

Paths under `scripts/` (repo-relative).

| Stage | Script / gate | Status | Delta |
|---|---|---|---|
| S0 | `partition.py` | **NEW** | trace-grounded nature classifier; emits segments + typed handoff graph; absorbs pure NAVIGATE |
| S1 | `capture_cdp.py` | exists | **EXTEND**: emit `segment_inputs.json`; enforce ≥2 varied-input clean runs |
| S2 | `analyze.py` (+`_engine/`) | exists | **EXTEND**: 1:N response assembly; pluggable extractors (multipart/header/path/binary/WHOLE-PAYLOAD) |
| S2 | (subset) in `classify_values.py` | **NEW** | transitive request-dependency closure; assembled-golden source detection |
| S3 | `classify_values.py` | **NEW** | **heart**: 5 buckets + AMBIENT-INPUT/CONTESTED; full-trace-then-close; co-variation + entropy guards; INV-1/INV-2 gate |
| pre | `detect_replayable.py` | exists | keep (signing/nonce/anti-bot bail-scan) |
| S5 | `probe_auth.py` | exists | **EXTEND**: signature-recipe + refresh-mint cases (not just cookie/Bearer) |
| S4/6 | `run_in_page` build | exists | **EXTEND**: REPEAT + RETRY primitives alongside POLL |
| S6 | `verify_equivalence.py` | exists | **EXTEND**: per-type comparator (assembled/projection/binary); mask-validity check |
| S6 | `prove_runner.py` | **NEW** | owns N≥2 loop + fresh/isolated/boundary/COMPUTED-perturbation instance selection |
| S7 | `recombine.py` | **NEW** | Executor: ordered regions, typed handoff validation, run-scope threading, workflow-level INV-1 |
| S8 | `teach_insert.py` | exists | keep (mechanical single-file write, UI verbatim) |

The structural additions (`partition`, `classify_values`, `prove_runner`, `recombine`) are exactly the four still-prose disciplines made into gates: **segment as the unit**, **no UNEXPLAINED value**, **proven on fresh+isolated+boundary instances**, and **a recombine contract that threads the pieces**. The two confirmed Metaview failures map one-to-one: incomplete-capture-scope → INV-1 (a missing template-apply mutation surfaces as a dangling PRODUCED); transport-only validation → INV-3 (content equality on covered instances, never "a file exists").

### Relevant paths
- Existing/extend: `scripts/{capture_cdp,analyze,detect_replayable,probe_auth,run_in_page,verify_equivalence,teach_insert}.py` (+ `_engine/`)
- New: `scripts/{partition,classify_values,prove_runner,recombine}.py`
- Prose to gate-ify: `SKILL.md`
