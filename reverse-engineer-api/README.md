# reverse-engineer-api (Agent Skill)

A **universal, teaching-mode helper** (a sibling of `skill-creator`). In teaching mode it converts one
step of a *target* workflow skill into an API-backed step by editing that step's **single file** in place
— it keeps the step's Mission/Inputs, inserts a `## API attempt` (a single `run-in-page` call) **above**
the original instructions, and preserves those instructions **verbatim** as `## UI instructions`, under a
one-line provenance header. One file per step, no sidecars: one fixed, reviewable, lintable pattern, the
same for every client. It never stores any customer's API itself.

**→ Operators (onboarding a client / doing this day to day): start with
[`docs/operator-playbook.md`](docs/operator-playbook.md)** — the tick-box checklist + the step-file and
teaching-prompt templates. **→ The full algorithm and the frozen wire formats are in
[`docs/DESIGN.md`](docs/DESIGN.md) and [`CONTRACTS.md`](CONTRACTS.md).** Everything below is the
technical/agent-facing detail.

## What's in here

The pipeline is staged S0→S7; each stage is a script that reads/writes the frozen shapes in `CONTRACTS.md`.

- `SKILL.md` — the teaching-mode method the agent follows to produce the pattern.
- `scripts/partition.py` *(S0)* — splits the workflow into ordered regions, mints stable `segment_id`s, and
  declares the typed handoff graph (`segments.json`). **The unit of API-ification is a SEGMENT** — a maximal
  contiguous run of data-work actions; `segments==0` ⇒ keep UI, done.
- `scripts/capture_cdp.py` *(S1)* — CDP capture of the demonstrated segment from a clean start; emits the
  trace, the golden, and `segment_inputs.json` (the captured handoff bindings) per varied-input run.
- `scripts/_engine/` — Browserbase `browser-to-api` engine, **MIT**, vendored **unmodified**; analysis
  stages only (`load..infer`), **never `emit`** — no openapi/client/report/html files.
- `scripts/analyze.py` *(S2)* — surfaces candidate endpoints (1:N response assembly; pluggable extractors).
- `scripts/detect_replayable.py` *(pre-gate)* — signed/nonce/CAPTCHA/anti-bot bail-to-UI classifier.
- `scripts/classify_values.py` *(S2–S4)* — **the heart**: backward-closes the causal subset, buckets every
  request value (const / input / ambient-input / derived / produced / computed / contested / unexplained),
  emits the ordered POLL/REPEAT/RETRY steps, and runs the G1 (self-contained) + G2 (no-fixed-wait) gates
  into `plan.json`.
- `scripts/check_chain.py` *(S2–S3 helper)* — self-containedness check over `plan.json`: every value is
  sourced and the chain stands alone (no value that only exists because a human set it up).
- `scripts/probe_auth.py` *(S5)* — deterministic bounded auth search (cookie session / readable-token-as-
  bearer / signature-recipe / refresh-mint); the G4 (auth-reproducible) gate.
- `scripts/run_in_page.py` *(S4 runtime)* — source for **`run-in-page`** (contract 1), the generic
  **on-PATH** runtime helper: body-derived read/write gate (refuses a write without `--allow-mutation`),
  success-predicate → exit code, correct-tab targeting, binary-to-file, and POLL/REPEAT/RETRY expressed
  inside the one JS expression. The runtime installs it on PATH; steps call it **by name**.
- `scripts/verify_equivalence.py` — the per-comparison content comparator (MATCH / MISMATCH / INCONCLUSIVE);
  emits the frozen comparison block (`verify_receipt.json §4.1`) and its exit code.
- `scripts/prove_runner.py` *(S6)* — **the G3 (proven) gate**: owns the N≥2 loop and the
  fresh/isolated/boundary/COMPUTED-perturbation instance selection, invokes the **frozen** comparator once
  per instance×run, and reduces to a single verdict (`verify_receipt.json`). A step ships as API *only* on a
  MATCH across held-out, mutually-isolated, boundary-spanning instances — "a file was produced" is not success.
- `scripts/recombine.py` *(S7)* — the executor: runs the ordered regions, validates each typed handoff at
  runtime, threads the shared run-scope, and re-checks self-containedness at the workflow level.
- `scripts/teach_insert.py` *(S8)* — the mechanical single-file surgical insert (the write path).
- `scripts/lint_skill.py` — optional CI consistency check (not part of the teaching procedure).
- `references/hard-cases.md` — read/write, auth ladder, chains, and when to bail.
- `e2e/run_e2e.sh` — offline-ish smoke test of the pipeline.

## The output (in the target skill, never here)
```
<client>/steps/
  <step>.md   # ONE file, mission style: header → Mission/Inputs → ## API attempt → ## UI instructions → Return value
```
Provenance (class, approver, validated) is the one-line header comment; the original UI lives verbatim in `## UI instructions`.
No `.ui.md` / `.capture.json` sidecars. Normal sessions just run `<step>.md` (API, fall back to UI) and
**never modify skills**; only teaching mode commits, human-reviewed.

## Runtime requirements
- **Teaching-time:** Node (engine analysis), a CDP-enabled Chromium, `websocket-client`.
- **Run-time:** `run-in-page` on PATH + a CDP-enabled Chromium. No Node, no `httpx`.
