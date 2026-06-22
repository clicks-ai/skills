# Worked examples

These are **illustrations of the same app-agnostic machine**, not special cases. Each one is labeled
*illustrative — the method is app-agnostic*. Nothing in the algorithm (`SKILL.md` boxes 0–9) branches on the
app, protocol, artifact type, or auth scheme below; they are merely the *instances* each example plugged in.
Read these to see what a real segment looks like as it moves through the checklist — never to copy an
endpoint, a field name, or an assumption into a new teach.

---

## Example A — single mutation → pre-signed download (cookie session)

*Illustrative — the method is app-agnostic.* Concrete instance: a Wave invoice "Export as PDF" step.

- **Box 0 Partition** — one data-work segment: the export. The "navigate to the invoice" action is a pure
  navigate, absorbed into the segment; `segment_ids == ["s0"]`.
- **Box 1 Capture** — clean start (logged in, invoice not yet exported), captured twice with two different
  invoice ids. Each run's golden is the downloaded PDF.
- **Box 2 Analyze** — the segment fired one GraphQL mutation (`InvoiceGeneratePdf`) returning a `pdfUrl`,
  then a GET to a pre-signed object-store URL that is the PDF.
- **Box 4 Classify** — `variables.id` buckets **INPUT** (co-varies with the invoice id across both runs);
  `pdfUrl` is **PRODUCED** by the mutation (a DERIVED whose source is a mutation, in R); the object-store
  GET produces the golden (`golden_source.found == true`, `mode: single`). G1 ✓, G2 ✓ (no wait).
- **Box 5 Auth** — same-origin cookie session: `credentials:"include"`, no header (case 1). `working:true`.
- **Box 6 Build** — one `run-in-page` call: issue the mutation → read `pdfUrl` → return `download:{url}`
  so the helper fetches the pre-signed URL to `--out` itself.
- **Box 7 Prove** — re-run on a second, fresh invoice; produce a fresh UI golden on it; the PDFs match
  under the frozen text-projection comparator on N≥2 runs. `verdict: PROVEN`.
- **Why it's a WRITE that's still eligible:** the mutation only renders a PDF (consequence-free), so it is
  API-ified with `--allow-mutation` and a recorded approver.

---

## Example B — apply-template → poll "Saved" → export (the missed-setup trap)

*Illustrative — the method is app-agnostic.* Concrete instance: a Metaview note "apply template, then
download summary" step.

- **The trap this example exists to show:** an earlier attempt carved the *apply-template* mutation into
  "setup done by hand before the capture," captured only the *export*, and shipped a chain that exported
  whatever happened to be on the note. On a fresh note it exported junk.
- **Box 0 Partition** — the apply + the export are **one** maximal data-work segment; the apply mutation is
  inside it, not prep. Iron Law 1.
- **Box 1 Capture** — clean start = template **not yet applied**. The capture therefore contains the
  apply-template mutation. Two runs with two different notes.
- **Box 2/4 Classify** — apply-template is a mutation producing a server-side job; the UI then waits for a
  "Saved" status before exporting. That wait is a **POLL** over the job-status read (G2 would FAIL on a
  fixed sleep). The export's job reference is **PRODUCED** by the apply mutation, in R. If the apply
  mutation were missing, its product would surface as a **dangling PRODUCED** → G1 FAIL → re-capture wider.
- **Box 7 Prove** — proven on a fresh, isolated note, not the build note; "a file exists" is rejected — the
  exported summary must match the UI's under the frozen comparator.
- **Lesson:** incomplete-capture-scope surfaces mechanically as a dangling PRODUCED (G1); transport-only
  "a file was produced" validation is rejected by content equality on fresh instances (G3).

---

## Example C — REST create → job-poll → paginated fetch (Bearer + pagination)

*Illustrative — the method is app-agnostic.* Concrete instance: a generic REST report export — `POST
/reports` returns a `job_id`; `GET /jobs/{job_id}` flips `status: QUEUED → COMPLETE`; `GET
/reports/{job_id}/rows?cursor=…` pages until `next_cursor == null`; the assembled rows are the artifact.

- **Box 0 Partition** — one data-work segment (create → poll → paged fetch).
- **Box 1 Capture** — twice, with one nominal input and one **large** input that forces multiple pages.
- **Box 4 Classify** —
  - `job_id` in the poll/fetch URLs is **PRODUCED** by `POST /reports` (mutation in R).
  - the report parameters in the POST body are **INPUT** (co-vary across runs).
  - an `Idempotency-Key` header is **COMPUTED** (`generator: uuid_v4`) — it differs across runs and is
    proof-obligated.
  - the wait on `GET /jobs/{job_id}` is a **POLL** with predicate `body-field /status == COMPLETE`.
  - the cursor loop on `…/rows` is a **REPEAT** with `until_predicate: next_cursor == null`, accumulating
    pages; the golden is therefore `mode: assembled` and the comparator is `ASSEMBLED`.
- **Box 5 Auth** — a Bearer whose value sits in a readable store, re-sourced live (case 2). `working:true`.
- **Box 6 Build** — one `run-in-page` call rendering ISSUE(create) → COMPUTE(idem key) → POLL(job) →
  REPEAT(rows) → assemble → return the artifact. Zero fixed sleeps; the poll exits only on the predicate.
- **Box 7 Prove** — the **large** instance forces pagination (so truncation can't pass), and the
  idempotency key is perturbed every run; proven N≥2 on mutually-isolated tenants under the frozen
  ASSEMBLED comparator. `verdict: PROVEN`.
- **Lesson:** pagination and the idempotency key are *behavior the UI performed* — REPEAT + COMPUTED model
  them; the proof boundary that forces pagination + perturbs every computed value is what makes the pass
  trustworthy rather than coincidental.

---

## When any of these would have KEPT the UI instead (all correct outcomes)

- **BAIL-1** — the artifact appears in no response nor any ordered assembly (it was client-rendered from
  data the page already held) → box 4 `golden_source.found == false`.
- **BAIL-3** — the readiness signal is purely out-of-band (a push the UI got over a socket) with no
  repeatable HTTP read that reflects it → box 4 G2 has no pollable observation.
- **BAIL-4** — the credential is device/origin-bound or in an unreadable store → box 5 `working:false`.
- **BAIL-5** — the output diverges on a fresh, isolated, or boundary instance → box 7 `verdict: FAILED`.

A `Plan` may end up all-UI, all-API, or mixed. Keeping the UI where the method can't prove equivalence is
the method working, not a miss.
