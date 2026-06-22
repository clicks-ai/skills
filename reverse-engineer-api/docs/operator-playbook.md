# Operator guide — turn one UI step into an API step

**Who this is for:** whoever onboards clients and runs this day to day. **No coding needed.**

**What it does:** you show the app doing one step once; a teaching agent watches, finds the real API behind
it, and rewrites that step to call the API directly — **but only if it can PROVE the API gives the exact same
result.** If it can't, it leaves the clicking in place. Later runs are faster and cheaper.

**The key idea — you never run anything technical.** You drive the agent with two pasted prompts and check
the result. The agent does all the hard work itself — capturing the action, working out the API, and proving
it matches. *(Curious what it does inside? It's in [`../SKILL.md`](../SKILL.md) and [`DESIGN.md`](DESIGN.md).
You don't need it to use this.)*

---

## Your whole job — 6 steps

### 0 · Is this step worth it? (10-second check)
- ✅ **Good fit:** a **download / export / fetch / generate** — one clear data result.
- ❌ **Leave as UI:** send / pay / delete, captcha or anti-bot pages, or "look at the screen and decide" steps.
*(The agent also refuses these on its own — this filter just saves you a session.)*

### 1 · Set up
- The step must already exist as a normal **UI step** in the client's skill. (Brand-new workflow? Write the UI
  step first from [`templates/step.md`](templates/step.md) — teaching improves an existing step, it doesn't
  invent the workflow.)
- Spawn an agent and mount **two** skills:
  - **`reverse-engineer-api`** — read-only (the teacher)
  - the **client's skill** — editable (the workflow you're improving)

### 2 · Warm up (log in)
Paste the **warm-up prompt** from [`templates/teach-prompt.md`](templates/teach-prompt.md) (fill in the app +
login). Confirm it reaches the app, then stop.

### 3 · Teach
Paste the **teach prompt** from the same file (fill in which step). Then **wait** — a few minutes. The agent
captures the action a couple of times, works out the API, and proves it matches. You don't touch any commands.

### 4 · Read the result — both outcomes are correct
The agent ends with one of two results — **both correct**, neither is an error:
- **`api-added`** ✅ — it proved the API matches the UI and wrote the API step.
- **`kept-ui`** — it couldn't prove a match (or the step can't be API'd), so it left the clicks and tells you
  **which check stopped it and why.** This is **NOT a failure** — the step still works exactly as before; you
  just didn't gain the speed-up this time. (kept-ui is common and fine — see *Expectations*.)

### 5 · Review + commit (1 minute)
`git diff` in the client repo and check:
- exactly **one** step file changed;
- an `## API attempt` was added **above** the original clicks (now `## UI instructions`), kept **word-for-word**;
- the header says **`validated: yes`**.

Then commit on a **slash-free branch** (e.g. `api-download-summary` — **not** `feat/...`; a `/` breaks the mount).

### 6 · Test it for real (the payoff)
New session → mount the client repo on your **branch** → log in → give the **plain task** (no mention of API),
ideally on a **different record** than you taught on. It should do the step **via the API, no clicking** — and
**open the produced file and check the contents are right**, not just that a file appeared.

---

## Expectations (so you manage your own)
- **Teaching takes a few minutes — once. Reuse is seconds — forever.**
- **`kept-ui` is a frequent, expected, correct outcome.** Some steps genuinely can't be reproduced (the login
  can't be reused, the page is signed/anti-bot, or the API output simply didn't match the UI's). You lose
  nothing — the step keeps working by clicking.
- **`validated: yes` means the agent *proved* the API output equals the UI's** — not "a file appeared." That
  proof is the entire point.
- It edits **only the one step file** and never changes your clicking instructions.

## The cautionary tale (the one bug this whole thing exists to prevent)
> An early version captured only the final **download** click and skipped a **setup step** that ran just before
> it. The API "succeeded" — it produced a file — but it was the **wrong file** (the real output was several
> times bigger). Nobody opened it, so "it worked" hid the bug.
>
> The fix is baked in now: the agent captures the **whole** action (setup included) and **proves the output
> equals the UI's** on fresh data before it ships. That is why `validated: yes` is content-proof, not "a file
> exists" — and why you still **open the file** in step 6.

## If something goes wrong (the 3 real gotchas)
| Symptom | Fix |
|---|---|
| Session **errors the moment it starts** | Branch name has a `/`. Use a **slash-free** branch (`api-download`, not `feat/api-download`). |
| **Reuse clicks instead of using the API** | Wrong branch mounted, or you didn't log in first. Open the step — it must show `## API attempt` at the top. |
| **"It worked" but the file is wrong** | Shouldn't happen now (the proof gate prevents it) — but always open the file. This is the exact bug the cautionary tale describes. |

## Want the internals? (optional — not needed to use this)
The agent follows a strict checklist: capture the **whole** action → prove the API output equals the UI's on
**fresh, isolated** data → ship it (`api-added`) or keep the clicks (`kept-ui`). The full procedure and the
checks behind it are in [`../SKILL.md`](../SKILL.md) and [`DESIGN.md`](DESIGN.md). **You never run it — the
agent does.**
