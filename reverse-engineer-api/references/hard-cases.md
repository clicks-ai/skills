# Hard cases — read/write, auth, chains, and when to bail

Default: **API attempt with UI fallback**. When in doubt, keep the UI step. A correct UI run beats an
API step that 200s in dev and 403s in prod.

## The five cases — how a web app exposes its API, and what to do

Detect the case from the captured request's origin + `observedAuthHeaders`, then pick the mechanism:

| # | Case | Tell | Mechanism |
|---|------|------|-----------|
| 1 | Same-origin + cookie session (common) | request origin == page origin; no bearer header | in-page `fetch`, `credentials:"include"`, no auth header |
| 2 | JS-readable token | a bearer/token header whose value sits in a readable cookie / localStorage / JS global | in-page `fetch`, re-source the token live (auth rung 2/3) — e.g. Wave's `waveapps` cookie value sent as the Bearer |
| 3 | In-memory / httpOnly bearer | a bearer whose value is in **no** readable store | keep UI (or case 4) — can't reproduce in-page |
| 4 | Official public API + provisioned token | the app documents a REST/GraphQL API; a long-lived token is configured | call it with `curl` + the token (no browser, no CORS) — most robust |
| 5 | Signed / HMAC / nonce / CAPTCHA / anti-bot | `detect_replayable.py` flags it | keep UI |

Cases 1, 2, 4 → write an `## API attempt`. Cases 3, 5 → keep UI with a one-line structured reason. The
auth ladder below is how you climb case 1 → 2; the probe budget is **one** escalation, then stop.

## Read vs write (the gate `run-in-page` enforces)
`run-in-page` derives **read|write** from the fetch body itself — GraphQL `mutation` keyword, or REST
`POST/PUT/PATCH/DELETE` — and **refuses a write (or anything it can't classify) unless `--allow-mutation`
is passed**. Never trust a caller-supplied label; the classifier is the source of truth, and `lint_skill.py`
flags a step whose header `class` echo disagrees with the body.

- **READ** (GET/HEAD, GraphQL `query`): safe to author + validate freely. No `--allow-mutation`.
- **WRITE** with a **consequence-free** way to validate (e.g. a PDF/render mutation, or a sandbox/test
  account): eligible — author with `--allow-mutation`, a recorded `approved:` in the header, validate once, set `validated:`.
- **WRITE with no safe validation target** (send/void invoice, refund, delete): **UI-only.** Do not API-ify it.

## Bail — keep the UI step
- **Signed / HMAC bodies** (`x-sig`, `signature`, `_hmac`, `checksum`): minted by in-page JS; a stale one is rejected.
- **Per-request nonces**: single-use; the captured one won't replay.
- **CAPTCHA / Turnstile / reCAPTCHA**: only the live page mints these.
- **Active anti-bot** (`403`/`429` with Cloudflare/DataDome/Akamai/PerimeterX markers in the trace).

`detect_replayable.py` flags these at teaching time.

## Auth — climb a ladder, default to reading nothing
Try the **first** rung that returns 2xx + a correct result; note the rung in the provenance header (a
**recipe** string, never a value):
1. **`credentials:"include"`, no auth header** — the common cookie-session case; nothing to add.
2. **+ a re-sourced non-httpOnly token** — add a header **only if rung 1 gets 401/403**, reading the token
   live, e.g. `Authorization: "Bearer " + (document.cookie… or localStorage.getItem("token"))`. Never hardcode it.
3. **+ a token from app JS state** (`window.__APOLLO_STATE__`, etc.) when it's not in a readable cookie/storage.

**httpOnly / cross-origin caveat:** if the token lives in an httpOnly cookie, JS can't read it — and a
cross-origin call (`page-origin → api-origin`) may not attach it. If no rung re-sources auth in-page, **bail to UI.**

## Chains — handle self-contained, bail cross-step
- **Handle (inline into the one JS):** every value a later call needs is produced **inside the captured
  trace** and reproduced **inside the same in-page expression** — including a **bounded poll-with-timeout**
  (e.g. `mutation → poll status → pre-signed-URL GET`). The Wave example (`InvoiceGeneratePdf → pdfUrl → S3`)
  qualifies: `run-in-page` returns `download:{url}` and fetches the pre-signed URL to `--out` itself.
- **Bail (UI):** a needed value came from an **uncaptured prior UI step**, an httpOnly cookie/redirect the
  fetch can't read, **unbounded** polling, or the body shape changes between runs (template won't generalize).

## Worked example
See `skill-test-workflows/wave/steps/download-invoice.md` for the canonical shape — one file (provenance
header → Mission/Inputs → `## API attempt` → `## UI instructions` → Return value): a WRITE (GraphQL
mutation, consequence-free render) with a self-contained `mutation → pre-signed S3` chain.
