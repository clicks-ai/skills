# Teaching prompts — copy, fill the `<slots>`, paste into a session

Three prompts, used in order. Warm-up + Teach go in the **teaching** session; Reuse goes in a **separate,
later** session to prove it works. Keep these generic — the app, protocol, artifact, and auth scheme are
just instances you fill into the slots; nothing here assumes any of them.

---

## 1. Warm-up (always first — log into the app)
```
Log in to <app>. Go to <login url> and log in with <how: the creds below / "Continue with <provider>" / …>:
username <username>, password <password>.
Confirm you reach <the landing page>, then stop.
```

## 2. Teach
```
Use the reverse-engineer-api skill in teaching mode to convert the <skill> "<step>" step into an
API-backed step.
- Target (editable): the <skill> skill, step "<step>".
- Read reverse-engineer-api's SKILL.md and follow its Procedure exactly, including its HARD RULES.
- I'm already logged in.

- Work the CHECKLIST in reverse-engineer-api's SKILL.md, box by box, in order. Paste each box's command
  output before ticking it. The gates decide the verdict — do not judge any value yourself.

- CAPTURE THE WHOLE SEGMENT FROM A CLEAN STATE, with >=2 varied inputs (box 1). Start from a clean instance —
  nothing done in advance. Do NOT do any setup before capture --start: whatever the UI does between the start
  and the final result — open, any setup / select / generate steps, any "wait until it's ready" signal — must
  ALL be inside the capture. Run the entire segment >=2 times with different inputs so the classifier can
  separate constants / inputs / computed values. Keep each UI output as that run's golden.

- You may NOT run teach_insert (box 8) until every gate has passed: box 3 not a bail, box 4
  unexplained==[], box 5 auth reproducible, box 7 MATCH on all held-out instances. KEEP UI on any
  BAIL/FAIL and tell me which gate and why.

- Do NOT git commit — I'll review the diff.
```

## 3. Reuse (a separate, later session — the proof)
A plain, normal request — **no mention of API or teaching**:
```
<the normal task>, e.g. "Download invoice 12345 and save the PDF to /agent/user-data/outputs/."
```

---

## Filled example (labeled — illustrative only)

This is one concrete fill of the slots above for an apply-template-then-download workflow. It is the same
shape for any app; the app/protocol/artifact names here are examples, not requirements.

**Warm-up** (e.g. Metaview)
```
Log in to Metaview. Go to https://my.metaview.app and log in with "Continue with Microsoft":
username <username>, password <from the step file>.
Confirm you reach the conversations list, then stop.
```

**Teach** (e.g. API-ify `open_and_download_summary` — the WHOLE segment: apply the template + download)
```
Use the reverse-engineer-api skill in teaching mode to convert the alphaskill-metaview
"open_and_download_summary" step into an API-backed step.
- Target (editable): the alphaskill-metaview skill, step "open_and_download_summary".
- Read reverse-engineer-api's SKILL.md and follow its Procedure exactly.
- Work the SKILL.md checklist box by box; paste each command's output before ticking.
- CAPTURE THE WHOLE SEGMENT FROM A CLEAN NOTE, >=2 varied notes. Pick notes that do NOT have the template
  applied yet. Do nothing before capture --start. Then per note: capture --start -> open the note, apply the
  template, wait for "Saved", download -> capture --stop. The apply-template mutation MUST be inside the
  capture (the cautionary tale in docs/operator-playbook.md is exactly this being dropped). Keep each
  downloaded file as that run's golden.
- You may NOT run teach_insert until box 7 is a MATCH on a held-out note the chain did NOT set up. KEEP UI
  on any BAIL/FAIL and tell me which gate and why.
- Do NOT git commit — I'll review the diff.
```

**Reuse**
```
Open and download the summary for the Metaview note <id> ("<title>"). Just download the summary.
```
