# reverse-engineer-api — Test Plan & Observation Log

> **INTERNAL — maintainers only.** This is the project's own validation suite and live-run log, not operator
> or agent documentation. Operators use [`operator-playbook.md`](operator-playbook.md); the algorithm and
> wire formats live in [`DESIGN.md`](DESIGN.md) and [`../CONTRACTS.md`](../CONTRACTS.md). Names/sessions/apps
> mentioned below are concrete historical observations, not part of the generic method.

The validation suite for the teaching-mode helper and the steps it generates. Two halves:
**execution** (does a generated step run the API and fall back cleanly?) and **teaching** (does the
helper correctly turn a UI step into an API step?). Update the **Observation log** after every live run.

Status legend: ✅ pass · ❌ fail · 🔧 fixed, needs re-test · ⏳ ready, not yet run · ❓ unknown/blocked · ⬜ not started

---

## How to run a live session (no secrets in this file, ever)

- **Stack:** `make run` in the monorepo → SPA `:5173`, agent_service `:8001`, event_read `:8004`.
- **Mounts on the test Agent:** `reverse-engineer-api` (read-only) + `skill-test-workflows` (editable). A
  **fresh session** clones the latest skill HEAD.
- **Read the event stream:** `GET http://localhost:8004/events?agent_id=<session-id>&order=asc&size=1000`
  (the session id is the last path segment of the SPA session URL).
- **Outputs:** `/agent/user-data/outputs/`.
- **Credentials:** entered live in the session, never written to any file or this log.

---

## Tier 0 — Static / CI (automated)

| ID | Proves | Command | Status |
|----|--------|---------|--------|
| T0.1 | helper logic + gates | `python scripts/test_run_in_page.py` (31 tests) | ✅ 2026-06-21 |
| T0.2 | artifact lints clean & rejects bad ones | `python scripts/lint_skill.py <skill-dir>` | ✅ 2026-06-21 |

## Tier 1 — Runtime image smoke (automated)

| ID | Proves | How | Status |
|----|--------|-----|--------|
| T1.1 | `run-in-page` on PATH, venv imports `websocket`, contract+write+variable-DELETE gates run | `docker run --rm --entrypoint bash agent-desktop:local -lc '…'` | ✅ 2026-06-21 |

## Tier 2 — Execution integration (live WARM Wave session)

A generated step run by the skill-blind leaf model. **Precondition: log in to Wave first** (the step
assumes an authenticated session; login is out of scope).

| ID | Gate | Pass criteria | Status |
|----|------|---------------|--------|
| G1 | Routing | plain task → `execute_step` → inline `download-invoice` invokes `run-in-page` (not the UI) | ✅ run 1, run 2 |
| G2 | Browser ready | `run-in-page` connects to the tab (no "connection refused") | ✅ run 2 (warm + open-app-first + cdp-wait) |
| G3 | **Auth — ANSWERED** | in-page fetch authenticates against `gql.waveapps.com` | ⚠️ run 2/3 — cookie IS sent (200 cross-origin) but **insufficient**: Wave returns `UNAUTHENTICATED` without the Apollo bearer, which is **not JS-re-sourceable** → **bail-to-UI** for this step |
| G4 | Download chain | `pdfUrl` fetched to `--out` is a real PDF | ◑ run 3 — real shape captured (response = `{pdfUrl}` pre-signed S3, **no `didSucceed`**; correct predicate = `!!pdfUrl`), but blocked by the G3 auth wall |
| G5 | Predicate strength | a 200-but-wrong response → exit ≠ 0 → UI | ✅ run 2 — predicate correctly rejected 200 + `didSucceed: null`; no false success |
| G6 | Determinism | open-app → one call → branch → fallback, **no** cookie/db/file digging | ✅ run 2 — clean fallback, zero credential hunting |
| G7 | Clean fallback | forced API failure → `## UI` → task done → honest `## Report` | ✅ run 1, run 2 |
| G8 | Correct tab | with >1 Wave tab open, the same-origin pick targets a valid tab (no wrong-tab, no silent evaporation) | ⏳ |
| G11 | Cold / logged-out | not-logged-in → fails clean (Report `failure`, no credential hunt) | ⏳ |

## Tier 3 — Teaching-mode integration (the product loop — NEVER RUN)

| ID | Gate | Pass criteria | Status |
|----|------|---------------|--------|
| G9 | End-to-end teach | reset `download-invoice.md` to UI-only → run teaching mode → it captures, classifies, writes `## API attempt` into the file, validates, lints CLEAN, emits the `TAUGHT` report | ◑ run 3 — capture/analyze ✅; convergence + surgical-insert write-back ❌ (rabbit-holed in auth probing, no TAUGHT report, stalled mid-turn) |

## Tier 4 — Generalization & economics (later)

| ID | Gate | Pass criteria | Status |
|----|------|---------------|--------|
| G10 | Economics | measured cost + latency, API path vs UI path | ⬜ |
| G12 | Re-capture determinism | a genuine 2nd capture → only cosmetic normalized diff; class/endpoint/auth changes surface | ⬜ |
| G13 | Second client | a different app (e.g. Airtable) taught + run end-to-end | ⬜ |

**"Works across clients" exit bar:** Tier 2 all ✅ on Wave (warm) · G9 ✅ once · G10 measured · G13 green on one more app.

---

## Observation log (newest first)

### Run 3 — 2026-06-21 — session `e5d29120` — Tier 3, TEACHING MODE (G9, first ever)
- **Capture machinery WORKED:** `capture_cdp.py` + `analyze.py` captured the real `InvoiceGeneratePdf` request (4 candidates incl. LinkedIn-ads noise) and the **successful demo response**: `{"invoiceGeneratePdf":{"pdfUrl":"https://s3.amazonaws.com/wave-prod-…Invoice_1_2026-06-19.pdf"}}` — a real pre-signed S3 URL, and **no `didSucceed` field**.
- **Explains Run 2:** the real payload has no `didSucceed`; a **bare in-page `fetch(credentials:"include")` returns 200 with a GraphQL `UNAUTHENTICATED — "Invalid request, authentication expired."` and `data:null`** (confirmed in agent replays at 03:58 + 04:00). So Run 2's `didSucceed:null` was that UNAUTHENTICATED error, not a wrong query. Decoded `invoiceId` = `Business:46900846-…;Invoice:2548644…` (our encoding was right).
- **Refined auth answer (G3):** the `waveapps` cookie IS sent but is **insufficient** — Wave's GraphQL needs an auth token its Apollo client supplies; the only JS-readable token the agent found was `"invalidated…"` (stale). The live bearer is **not reliably re-sourceable in-page** → by our own `hard-cases` rule this is a **bail-to-UI** step.
- **Teaching execution FAILED to converge (G9 ❌):** the agent rabbit-holed ~6 min into manual auth probing (localStorage/__NEXT_DATA__ hunting, JS syntax errors, `--contract 0` mistakes), never recognised "auth not reproducible → bail to UI", **never wrote `download-invoice.md`** (only `README.md` touched), produced **no TAUGHT report**, and **stalled mid-turn** at 04:01.
- **Verdict:** capture/analyze ✅ (the hard part works + delivered the key insight). **Wave download-invoice API = not viable → keep UI** (auth wall — a *correct* design outcome). Teaching-mode *execution* ❌ — must converge to bail-to-UI fast and always write a conclusion, not grind.
- **Next:** (1) tighten the teaching method to fail fast to bail-to-UI + always write the outcome; (2) pick a *different* target whose auth is in-page-reproducible (cookie-only or a readable token) to actually exercise a successful API teach end-to-end.

### Run 2 — 2026-06-21 — session `2fb8b1b4` — Tier 2, WARM session (logged in first)
- **Setup:** logged in to Wave as a first step (warm), then the plain download task.
- **`run-in-page` result:** `{"ok": false, "status": 200, "didSucceed": null, "reason": "expected output file is missing or empty", "class": "write"}` exit 1.
- **Observed:** routing ✅; `run-in-page` **connected and ran** (no connection-refused — race fixed); the cross-origin fetch to `gql.waveapps.com` returned **HTTP 200** with the session cookie; but `data.invoiceGeneratePdf.didSucceed` was **null** (no `pdfUrl`), so the predicate correctly returned `ok:false` → exit 1 → clean UI fallback (no credential digging); UI exported the PDF (31189 bytes); honest Report.
- **Verdict:** G1 ✅ · G2 ✅ · **G3 ✅ — cookie auth WORKS (200 cross-origin; not httpOnly-blocked)** · G5 ✅ (strong predicate caught the bad 200) · G6 ✅ (clean fallback) · G7 ✅. **G4 ❌** — the only gap: the hand-authored GraphQL (operation/`gqlId`/shape) is wrong → `didSucceed: null`. It was never captured from a real session (`validated: no`).
- **Conclusion:** the architecture, auth, race, predicate, and fallback are all proven. The API path fails **only** because we *guessed* the request. **This is exactly what teaching mode fixes** — capture the real `InvoiceGeneratePdf` request instead of hand-writing it.
- **Next:** Run 3 — Tier 3 teaching mode (G9): reset `download-invoice.md` to UI-only, capture the real request, regenerate, re-run → the API path should then succeed.

### Run 1 — 2026-06-21 — session `021062a6` — Tier 2, COLD session
- **Setup:** plain task "download invoice 2548644508954876537", session not pre-logged-in.
- **Observed:** routing worked (ran `run-in-page`); API failed `connection refused` (Chromium launched lazily ~4s *after* the step ran); leaf model then dug cookie DB / IndexedDB / keyrings ~90s trying to hand-rescue auth; UI fallback exported the PDF (31 KB); final report was honest.
- **Verdict:** G1 ✅ · G2 ❌ (browser-launch race) · G6 ❌ (off-script dig) · G7 ✅ (but messy). G3/G4/G5 **blocked** — the fetch never connected, so auth was never tested.
- **Fixes shipped (helper `a310b03`, wave `e8aa7cd`):** `--cdp-wait` + open-app-first (G2); hard no-investigate guards (G6); + review fixes (fail-safe classify, secret-scan, content-aware download, same-origin tab pick).
- **Next:** Run 2 — WARM session (log in first) to finally exercise G2–G5, the auth question.
