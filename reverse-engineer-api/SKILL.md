---
name: reverse-engineer-api
description: >-
  Teaching-mode helper: convert one demonstrated UI workflow step into a faster API-backed version ONLY
  when the API output is PROVEN to equal the UI's output. It partitions the step into data-work segments,
  captures each whole segment from a clean start, rebuilds it as one self-contained call-chain with every
  value sourced and every wait/loop/retry reproduced, and proves equivalence on fresh, isolated, boundary
  instances — then EITHER writes an `## API attempt` into the step (UI preserved verbatim as fallback) OR
  keeps the UI with a short written reason. Disciplined: edits only the one target step file. Trigger
  words: reverse engineer api, use the api, apify, make this cheaper, convert this step to api, skip the UI.
---

# Reverse-Engineer-API (teaching-mode helper)

A universal helper like `skill-creator`. In teaching mode it converts ONE step of a target workflow skill
into an API-backed step **only when the API reproduces the UI's result exactly**, and otherwise **keeps the
UI** and says why. Normal sessions never use this skill.

The method is **app-agnostic**. Protocol, artifact type, auth scheme, and the target app are *instances*
plugged into the same machine — nothing here branches on any of them. Every rule that matters is a
**tool-enforced gate**, never prose, because the settled lesson is that *prose does not bind the executing
agent*. Your job is to run the boxes in order and obey their exits. You do not judge a value; the gates do.

---

## The three Iron Laws

These are stated in **delete-and-restart** form: if you catch yourself about to break one, the correct move
is to **stop, delete what you produced, and restart the box from its command** — not to patch around it.

1. **The unit is a SEGMENT, never the whole workflow and never one action.** A segment is a *maximal
   contiguous run of data-work*. If you find yourself API-ifying a fragment that depends on state a human or
   the UI set up out of band — **delete the chain and re-capture the whole segment from a clean start.**
   Mis-sizing this unit is the documented root cause of every prior failure.

2. **No value may be UNEXPLAINED and no behavior may be DROPPED.** Every byte the replay sends must be
   sourced (const / input / derived / produced / computed); every wait, loop, retry, and assembly the UI
   performed must be reproduced. If the classifier reports one unexplained or contested value, or a wait
   with no poll — **delete the plan and re-capture wider; do NOT hand-fill the gap.**

3. **Ship only what is PROVEN; bailing is success.** "A file was produced" is not proof. The output must
   EQUAL the UI's output under a frozen comparator, on instances you did **not** build on, that are mutually
   isolated and span the input boundaries. If proof is missing or fails — **delete the API attempt and keep
   the UI.** A keep-UI outcome is the method working correctly, not a failure.

---

## Inputs (from the human)
- `TARGET_SKILL` — path to the editable skill, e.g. `/agent/skills/editable/<repo>/<skill>`.
- `STEP` — the step to convert; `steps/<STEP>.md` is its mission-style UI baseline.

## Prerequisites
`command -v run-in-page` and `curl -s http://127.0.0.1:9222/json/version` must both succeed. The proof gate
compares artifacts by content; for a non-deterministic binary container a projection/text extractor must be
available, else the gate is INCONCLUSIVE and you keep the UI.

---

## The CHECKLIST — run each box, obey its exit

Each box is **one command** with a **binary outcome**. A box's gate is the exact token its tool prints;
read that token and branch on it. **`teach_insert` (box 8) is reachable ONLY after boxes 0–7 all pass** —
any earlier `KEEP UI` / `FAIL` / `BAIL` exit ends the teach with the UI preserved and `teach_insert` NOT
run. A "→ KEEP UI" exit is the system working.

```
TEACH <skill>/<STEP>

[ ] 0  PARTITION
       python scripts/partition.py --step steps/<STEP>.md --grounded .o11y/run
       GATE: segments.json → segment_ids != []
       segment_ids == []  →  KEEP UI, done (no data-work to API-ify).
       bail != null       →  KEEP UI (reason in segments.json.bail).

[ ] 1  CAPTURE WHOLE  (per segment, clean start, >=2 varied inputs)
       python scripts/capture_cdp.py --out .o11y/run  --start
         … perform the ENTIRE segment through the UI, clean, start to finish …
       python scripts/capture_cdp.py --out .o11y/run  --stop
         … repeat into .o11y/run2 with a DIFFERENT input (varied) …
       GATE: each run dir has cdp/network/ + segment_inputs.json with a bound, DIFFERING input_identity.
       (writes trace, golden, segment_inputs.json per run dir)

[ ] 2  ANALYZE
       python scripts/analyze.py --run .o11y/run --match <url-substr-of-the-segment>
       GATE: every request the segment fired is listed, in order, with its responseExample.

[ ] 3  BAIL-SCAN
       python scripts/detect_replayable.py --run .o11y/run
       GATE: exit 0 = replayable.
       exit 3  →  KEEP UI (signed / HMAC / nonce / CAPTCHA / anti-bot).

[ ] 4  CLASSIFY  (INV-1 + INV-2)
       python scripts/classify_values.py --runs .o11y/run .o11y/run2 \
         --segments segments.json --segment <segment_id> --out plan.json
       GATE: plan.json → gate.G1_self_contained.pass == true
                      AND gate.G2_no_fixed_wait.pass   == true
                      AND verdict == "API-CANDIDATE"
       unexplained/contested/dangling_produced non-empty, OR a wait with no POLL,
       OR a continuation with no REPEAT, OR bail != null  →  KEEP UI (or re-capture wider, box 1).

[ ] 5  AUTH  (INV-4)
       printf '%s' '{"method":"<M>","url":"<URL>","headers":{<non-auth>},"body":<json-string|null>}' > /tmp/req.json
       python scripts/probe_auth.py --match <origin> --request /tmp/req.json --expect-status 200
       GATE: working == true  (case 1 cookie | case 2 readable-token-as-Bearer | signature-recipe | refresh-mint).
       working == false  →  KEEP UI (credential not re-sourceable).

[ ] 6  BUILD
       author command.sh from plan.json  (ISSUE → BIND/COMPUTE → POLL/REPEAT/RETRY → act → return artifact)
       GATE: one `run-in-page --contract 1 …` call; every step in plan.json.steps is rendered;
             zero fixed sleeps used as readiness.

[ ] 7  PROVE  (INV-3)
       python scripts/prove_runner.py --command command.sh \
         --instances <fresh,isolated,boundary> --runs 2 --out verify_receipt.json
       GATE: verify_receipt.json → verdict == "PROVEN"
       verdict == "FAILED" | "UNCOVERED"  →  KEEP UI (a fresh/isolated/boundary/computed-perturbed run diverged).

[ ] 8  WRITE  (only on box-7 PROVEN; on any KEEP UI, do NOT run)
       python scripts/teach_insert.py --step "$TARGET_SKILL/steps/<STEP>.md" \
         --header "<provenance · validated: yes>" --command command.sh
       GATE: edits ONLY the step file (wraps the command, inserts ## API attempt, preserves UI verbatim).

[ ] 9  DISCIPLINE
       git -C "$TARGET_SKILL" diff --name-only
       GATE: output is exactly steps/<STEP>.md (else git checkout -- that file).

RESULT:  api-added (all boxes PASS)  |  kept-ui (any BAIL/FAIL exit)  —  BOTH are correct.
```

---

## Known rationalizations → required action

When the executing agent is tempted to skip a gate, it reaches for one of these. Each is a **trap**; the
required action is the gate, not the shortcut. (These are generic — they apply to every app.)

| The rationalization (a thought you might have) | Why it's wrong | Required action |
|---|---|---|
| "It worked once, so it's fine to ship." | One pass proves nothing — an async race or shared-state coincidence passes once, then exports junk on reuse. | Run box 7 with N≥2 on fresh, isolated, boundary instances. No PROVEN ⇒ keep UI. |
| "I'll just add a `sleep` / fixed `setTimeout` before the act." | A fixed wait is a race: it passes when the server is fast, fails when it's slow. That is dropped behavior (Iron Law 2). | The UI watched a ready signal — POLL *that* signal (box 4 emits the POLL; box 6 renders it). |
| "The thing already exists, so just fetch the output directly." | The setup mutation that created it is part of the segment; skipping it means the chain depends on out-of-band state and ships a one-shot. | Re-capture the WHOLE segment from a clean start (box 1) so the mutation is inside the trace. |
| "This one value is obviously a constant / obviously the input — I'll hardcode it." | You are judging a value; the classifier judges values, with co-variation + entropy evidence across ≥2 runs. | Let box 4 bucket it. An UNEXPLAINED/CONTESTED result ⇒ re-capture wider or keep UI. |
| "I'll open the `.o11y/` capture files / write a `python -c` to inspect the wire." | Hand-grinding the raw wire is the old failure mode; `analyze.py`'s output already surfaces every request + response. | Use box 2's output only. Do not read raw capture files or script the wire. |
| "I'll hand-hunt the cookie / try a few auth headers in a loop." | Manual auth tuning is non-deterministic churn; `probe_auth.py` finds the working auth in one bounded pass. | Run box 5 once. `working:false` ⇒ keep UI. |
| "The golden isn't in any response, but the page clearly shows it — I'll scrape the DOM." | If no response (nor any ordered assembly) contains the output, it is client-rendered and not API-reproducible. | That is BAIL-1 at box 4 (`golden_source.found == false`) ⇒ keep UI. |
| "The first response frame is enough; I'll ignore the rest of the stream / pages." | Streaming and pagination are behavior the UI performed; truncating to the first frame drops it (Iron Law 2). | Box 4 emits a REPEAT/assembled-golden; box 6 must render the full loop. |
| "I'll read the minified frontend source and trust the recipe it implies." | Minified code is low-fidelity — it INFORMS, never OVERRIDES; any recipe must still survive proof. | Use it only to resolve a CONTESTED/UNEXPLAINED value; then it must still pass box 7. |
| "Let me just edit SKILL.md / another step to make this fit." | The teach edits exactly one file by construction; touching anything else is undisciplined drift. | `teach_insert` (box 8) writes only the step file; box 9 enforces it. |

---

## Per-step notes (short)

- **Box 0 — Partition.** Assigns each action a nature (fuzzy/navigate/data-work/comprehend), confirmed
  against the capture, then coalesces maximal data-work runs into segments and declares the typed handoff
  graph. A pure navigate is absorbed into the adjacent segment; a navigate that fired a mutation becomes
  data-work. `segment_ids == []` is a clean "nothing to do here".
- **Box 1 — Capture whole.** Start from a clean instance: logged in, but the segment's own effects **not**
  pre-done. Capture the entire segment, twice, with **different inputs** — the variation is what lets the
  classifier separate constant from input from computed. Keep each run's UI output as that run's golden.
- **Box 2 — Analyze.** Lists every request in order (setup mutations, status polls, the final act) with the
  response examples your predicate fields come from. This is the only window onto the wire you use.
- **Box 3 — Bail-scan.** A signature / HMAC / nonce / CAPTCHA / anti-bot signal means the live page mints
  something you can't replay → keep UI.
- **Box 4 — Classify.** The heart. Locates the golden's source, backward-closes the minimal causal request
  set, buckets every request value, and inserts POLL/REPEAT/RETRY for every wait/continuation/transient. It
  is the mechanical witness for Iron Law 2 (G1 self-contained, G2 no fixed wait). Its verdict is final.
- **Box 5 — Auth.** Auth material is just more values run through the same classifier: static const,
  session token (minted in-chain), per-request signature recipe, or a refresh-mint. One bounded pass.
- **Box 6 — Build.** Transcribe the plan into one `run-in-page` call: setup → poll/repeat/retry → act →
  return the artifact. Never invent a field; never insert a fixed readiness sleep.
- **Box 7 — Prove.** Owns the N≥2 loop and the instance selection (fresh, isolated, boundary, and one that
  perturbs every computed value / forces pagination). PROVEN is the only ship signal.
- **Box 8 — Write.** Mechanical single-file insert; the UI block survives byte-for-byte as the fallback.
  Run it only on PROVEN.
- **Box 9 — Discipline.** The diff must be exactly the one step file. Anything else gets checked out.

## Safety
- Edit only the target step file (the box-9 diff check enforces it).
- The API unit must be a whole, self-contained segment (Iron Law 1) — never split a chain at a
  data-dependency.
- No secrets in the artifact: `run-in-page` re-sources auth live at run time; never write a token, cookie,
  or account id into the step. (Login email/password may stay inline in the UI block.)
- On the live site: no delete/modify/leak beyond the demonstrated read or a human-approved,
  consequence-free write.

## Bundled scripts
- `partition.py` — trace-grounded segment partition + typed handoff graph (box 0).
- `capture_cdp.py` — clean capture of the whole segment, per varied-input run (box 1).
- `analyze.py` (+ `_engine/`, Browserbase MIT, analysis-only) — surface the segment's requests (box 2).
- `detect_replayable.py` — signed/anti-bot bail scan (box 3).
- `classify_values.py` — the heart: subset + classify + control-flow; emits G1/G2 (box 4).
- `probe_auth.py` — deterministic bounded auth search (box 5).
- `run_in_page.py` — `run-in-page`, the on-PATH in-page caller with POLL/REPEAT/RETRY (box 6).
- `prove_runner.py` (over `verify_equivalence.py`) — the proof gate: fresh/isolated/boundary/perturbed
  instances under the frozen comparator (box 7).
- `teach_insert.py` — the mechanical single-file surgical insert (box 8).
- `recombine.py` — the executor that threads typed handoffs across regions (workflow-level INV-1).
- `lint_skill.py` — OPTIONAL CI consistency check; NOT part of this procedure.

## References
- `references/examples.md` — worked illustrations (each labeled *illustrative — the method is app-agnostic*).
- `references/hard-cases.md` — the auth ladder, chains, and the bail conditions in detail.
