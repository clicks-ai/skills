# Chain patterns — POLL, REPEAT, RETRY inside ONE in-page expression

A UI step is usually a *chain* of calls (set up state → wait → act → fetch the artifact). `run-in-page`
runs the chain as **one async JS expression** — there is no second helper process and no out-of-band loop.
The three control-flow primitives the UI performed (DESIGN §2.4) are therefore expressed **inside that one
JS**: a **POLL** (wait for readiness), a **REPEAT** (follow a continuation signal / paginate), and a
**RETRY** (bounded transient-failure retry). `plan.json.steps` is the abstract source; the snippets below
are the canonical authoring forms a generated chain renders to.

These are the *only* loop shapes `run-in-page` is tuned for. It reads each loop's **own declared bound** out
of the JS and floors the CDP deadline to cover it, so a long-but-bounded readiness loop runs to its
predicate instead of being cut short. Author the bounds the way shown below or the floor won't see them.

> **The one rule these serve:** *No behavior in the capture may be DROPPED.* A UI that waited needs a POLL;
> a UI that paged needs a REPEAT; a UI that survived a transient 5xx may need a RETRY. Dropping any of these
> ships a step that works once and silently truncates / races on the next instance.

> **Zero fixed waits (gate G2).** A bare `setTimeout(s, <num>)` that gates the act with **no predicate** is a
> readiness sleep → FAIL. A `setTimeout` is only legal as the **interval between polls** of a loop that also
> tests a predicate (the loop still exits on the predicate, never on the clock alone).

All examples below are **illustrative** (URLs, field names, status values are placeholders). Nothing here
assumes a protocol, artifact type, or app — swap the predicate's observation for whatever the capture
actually exposes.

---

## POLL — wait for readiness, predicate-driven, bounded

Re-query a readiness `read` until the predicate holds, bounded by a wall-clock timeout. The predicate is
**over any repeatable observation** (DESIGN §2.4 / `ReadinessPredicate`): a status code, a resource going
`404 → 200`, a response header, or a body field — *not only* a body status field.

```js
// POLL: re-read until ready, bounded — NEVER a fixed sleep gating the act
const t0 = Date.now();
let ready = false, last;
do {
  const r = await fetch(`/job/${jobId}`, { credentials: "include" });   // the readiness `read`
  last = r.status === 200 ? (await r.json()).status : null;             // predicate.over = body-field
  if (last === "COMPLETE") { ready = true; break; }                     // predicate.equals
  await new Promise(s => setTimeout(s, 2000));                          // interval_s — between polls, not a readiness wait
} while (Date.now() - t0 < 60000);                                      // timeout_s — the loop's OWN bound
if (!ready) return { ok: false, status: last, reason: "poll timed out" };
```

Predicate variants (pick the one the capture actually reflects — all equally valid):

```js
// over = status-code (the read returns the readiness in its status)
if (r.status === 200) { ready = true; break; }

// over = resource-presence (404 while pending, 200 once it exists)
if (r.status !== 404) { ready = true; break; }

// over = header-value (readiness advertised in a response header)
if (r.headers.get("x-state") === "ready") { ready = true; break; }
```

**Floor cue.** `run-in-page` reads the `Date.now() - t0 < <ms>` bound and the inter-poll
`setTimeout(s, <ms>)` to raise the CDP timeout above the loop's budget — so a 60s poll under a default
`--timeout 30` is NOT prematurely killed. Always give the loop an explicit `Date.now()`-based bound.

**Bail (keep UI), not a hidden infinite loop:** if **no** repeatable HTTP read reflects readiness
(push-only / out-of-band socket), there is nothing to poll → BAIL-3. Never substitute a fixed sleep.

---

## REPEAT — follow a continuation signal, accumulate every page

When a response field is a *continuation signal* (a cursor, `has_more`, or `total > page_size`) fed back
into the **same** call, the UI paged through all of it. Reproduce the loop and **accumulate** — a small
golden captured on page 1 must not become the program's silent truncation point (FP-6).

```js
// REPEAT: cursor pagination — accumulate until the continuation signal is exhausted
const items = []; let cursor = null;
do {
  const r = await fetch(`/list?cursor=${cursor ?? ""}`, { credentials: "include" });
  const page = await r.json();
  items.push(...page.items);                 // accumulate -> `items` is the run-scope key pages append to
  cursor = page.next_cursor;                 // until_predicate: next_cursor == null
} while (cursor);
```

Continuation-signal variants (same shape, different exhaustion test):

```js
// has_more boolean
let offset = 0; do { /* fetch ?offset= */ offset += page.items.length; } while (page.has_more);

// total vs page-size (compute the page count up front)
const total = first.total, size = first.items.length;
for (let p = 1; p * size < total; p++) { /* fetch ?page=p, push items */ }
```

> A REPEAT has no `Date.now()` timeout — its bound is the **data** (`next_cursor == null`). To stay safe
> against a server that never signals exhaustion, cap the iteration count with a generous sentinel
> (`while (cursor && pages++ < 1000)`) and surface `ok:false` if the cap is hit, rather than spinning.

**Proof obligation:** the equivalence gate must run an input that **forces pagination** (`total > page_size`)
on an isolated instance — an unfollowed `next`/`total` is a DROPPED behavior → FAIL (DESIGN §4 G3.4).

---

## RETRY — bounded transient-failure retry, keyed on the call's own status

Wrap a call that may transiently fail (`502/503/429`) in a bounded retry keyed on **its own** retryable
status. This is the dual concern to POLL: POLL waits for *another* resource to become ready; RETRY re-issues
*this* call after a transient failure.

```js
// RETRY: bounded retry on the act call's OWN retryable status
let resp, attempt = 0;
do {
  resp = await fetch(actUrl, { method: "POST", credentials: "include", headers, body });
  if (![502, 503, 429].includes(resp.status)) break;   // on_retryable_status
  await new Promise(s => setTimeout(s, 500 * (attempt + 1)));  // backoff between attempts (optional)
} while (++attempt < 3);                                // max_attempts — bounded
if (resp.status >= 400) return { ok: false, status: resp.status, reason: "retries exhausted" };
```

> **Idempotency.** Retrying a **non-idempotent** mutation (a create/charge/send) with **no idempotency key**
> can double-apply the effect. If the captured request carries no idempotency key, do NOT silently retry the
> mutation — flag it and keep the UI for that call (DESIGN FN-5). A COMPUTED idempotency-key generator
> (a fresh uuid/nonce per logical op, replayed across attempts) makes the retry safe.

**Floor cue.** `run-in-page` reads the `++attempt < <n>` bound and any `setTimeout(s, <ms>)` backoff to
account for the worst-case retry wall-clock when raising the CDP timeout.

---

## Composing the chain (the common end-to-end shape)

A full self-contained chain typically nests these: act (with RETRY) → POLL until the result is ready →
fetch the artifact (possibly with REPEAT to assemble it), then `return` the `run-in-page` object.

```js
(async () => {
  // 1. act — bounded RETRY around the mutation
  let resp, attempt = 0;
  do { resp = await fetch(actUrl, { method: "POST", credentials: "include", body });
       if (![502,503,429].includes(resp.status)) break;
  } while (++attempt < 3);
  const jobId = (await resp.json()).jobId;

  // 2. POLL — predicate-driven, bounded; never a fixed sleep
  const t0 = Date.now(); let url = null;
  do { const r = await fetch(`/job/${jobId}`, { credentials: "include" }); const j = await r.json();
       if (j.status === "COMPLETE") { url = j.download_url; break; }
       await new Promise(s => setTimeout(s, 2000));
  } while (Date.now() - t0 < 60000);
  if (!url) return { ok: false, reason: "poll timed out" };

  // 3. return a self-authenticating URL for run-in-page to fetch to --out (carries no browser cookies)
  return { ok: true, status: 200, contentType: "application/pdf", download: { url } };
})()
```

The returned object is the frozen `run-in-page` interface (CONTRACTS §5.2): `ok` is a **strong** predicate
(status + content-type + a positive shape signal); `download.url` must be **self-authenticating** (e.g. a
pre-signed link) because the helper fetches it with `urllib`, no browser cookies; or return small inline
bytes in `dataBase64`. The helper treats `ok:true` as success only if `--out` (when given) received a
non-empty, type-correct file.
