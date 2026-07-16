---
name: skill-creation
description: Guide for creating effective skills. Use when a user wants to create a new skill (or update an existing one) that extends your capabilities with specialized knowledge, workflows, or tool integrations. Covers both simple single-file skills and multi-step agentic workflows that split into an orchestrator (SKILL.md) and executors (step.md files).
---

# Skill Creator

A skill packages specialized knowledge, a workflow, or a tool integration so it can be loaded on demand. This guide covers two things: the **anatomy** of any skill (structure + progressive disclosure), and the **orchestrator/executor methodology** for skills that run as multi-step agentic workflows.

## How to use this file

**Read this entire file before starting, every time.**

- **Anatomy** and **Progressive disclosure** apply to every skill.
- **Steps**, **Workflow methodology**, and the **Schemas**, apply only once the skill turns out to be (or grows into) a multi-step workflow that delegates work to subagents. Still, read the entire file upfront.
- **Skill creation process** covers the interactive flow of teaching a skill.

## Anatomy of a Skill

Every skill consists of a required SKILL.md file and optional bundled resources:

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter metadata (required)
│   │   ├── name: (required)
│   │   └── description: (required)
│   └── Markdown instructions (required)
└── Bundled Resources (optional)
    ├── scripts/          - Executable code (Python/Bash/etc.)
    ├── references/       - Documentation intended to be loaded into context as needed
    ├── steps/            - Instructions for delegated workflow steps (one file per step)
    └── assets/           - Files used in output (templates, icons, fonts, etc.)
```

### SKILL.md (required)

Every SKILL.md consists of:

- **Frontmatter** (YAML): Contains `name` and `description` fields. It is very important to be clear and comprehensive in describing what the skill is, and when it should be used.
- **Body** (Markdown): Instructions and guidance for using the skill. Only loaded AFTER the skill triggers (if at all).

### Bundled Resources (optional)

#### Scripts (`scripts/`)

Executable code (Python/Bash/etc.) for tasks that require deterministic reliability or are repeatedly rewritten.

- **When to include**: When the same code is being rewritten repeatedly or deterministic reliability is needed
- **Example**: `scripts/rotate_pdf.py` for PDF rotation tasks
- **Benefits**: Token efficient, deterministic, may be executed without loading into context

#### References (`references/`)

Documentation and reference material intended to be loaded as needed into context to inform your process and thinking.

- **When to include**: For documentation that should be referenced while working
- **Examples**: `references/finance.md` for financial schemas, `references/mnda.md` for company NDA template, `references/policies.md` for company policies, `references/api_docs.md` for API specifications
- **Use cases**: Database schemas, API documentation, domain knowledge, company policies, detailed workflow guides
- **Benefits**: Keeps SKILL.md lean, loaded only when you determine it's needed
- **Best practice**: If files are large (>10k words), include grep search patterns in SKILL.md
- **Avoid duplication**: Information should live in either SKILL.md or references files, not both. Prefer references files for detailed information unless it's truly core to the skill — this keeps SKILL.md lean while making information discoverable without hogging the context window. Keep only essential procedural instructions and workflow guidance in SKILL.md; move detailed reference material, schemas, and examples to references files.

#### Assets (`assets/`)

Files not intended to be loaded into context, but rather used to produce output.

- **When to include**: When the skill needs files that will be used in the final output
- **Examples**: `assets/logo.png` for brand assets, `assets/slides.pptx` for PowerPoint templates, `assets/frontend-template/` for HTML/React boilerplate, `assets/font.ttf` for typography
- **Use cases**: Templates, images, icons, boilerplate code, fonts, sample documents that get copied or modified
- **Benefits**: Separates output resources from documentation, enables you to use files without loading them into context

## Progressive Disclosure Design Principle

Skills use a three-level loading system to manage context efficiently:

1. **Metadata (name + description)** - Always in context (~100 words)
2. **SKILL.md body** - When skill triggers (<5k words)
3. **Bundled resources** - As needed (Unlimited because scripts can be executed without reading into context window)

### Progressive Disclosure Patterns

Keep SKILL.md body to the essentials and under 500 lines to minimize context bloat. Split content into separate files when approaching this limit. When splitting out content into other files, it is very important to reference them from SKILL.md and describe clearly when to read them, to ensure the reader of the skill knows they exist and when to use them.

**Key principle:** When a skill supports multiple variations, frameworks, or options, keep only the core workflow and selection guidance in SKILL.md. Move variant-specific details (patterns, examples, configuration) into separate reference files.

**Pattern 1: High-level guide with references**

```markdown
# PDF Processing

## Quick start

Extract text with pdfplumber:
[code example]

## Advanced features

- **Form filling**: See [FORMS.md](FORMS.md) for complete guide
- **API reference**: See [REFERENCE.md](REFERENCE.md) for all methods
- **Examples**: See [EXAMPLES.md](EXAMPLES.md) for common patterns
```

Load FORMS.md, REFERENCE.md, or EXAMPLES.md only when needed.

**Pattern 2: Domain-specific organization**

For Skills with multiple domains, organize content by domain to avoid loading irrelevant context:

```
bigquery-skill/
├── SKILL.md (overview and navigation)
└── reference/
    ├── finance.md (revenue, billing metrics)
    ├── sales.md (opportunities, pipeline)
    ├── product.md (API usage, features)
    └── marketing.md (campaigns, attribution)
```

When a user asks about sales metrics, you only read sales.md.

Similarly, for skills supporting multiple frameworks or variants, organize by variant:

```
cloud-deploy/
├── SKILL.md (workflow + provider selection)
└── references/
    ├── aws.md (AWS deployment patterns)
    ├── gcp.md (GCP deployment patterns)
    └── azure.md (Azure deployment patterns)
```

When the user chooses AWS, you only read aws.md.

**Pattern 3: Conditional details**

Show basic content, link to advanced content:

```markdown
# DOCX Processing

## Creating documents

Use docx-js for new documents. See [DOCX-JS.md](DOCX-JS.md).

## Editing documents

For simple edits, modify the XML directly.

**For tracked changes**: See [REDLINING.md](REDLINING.md)
**For OOXML details**: See [OOXML.md](OOXML.md)
```

You read REDLINING.md or OOXML.md only when the user needs those features.

**Important guidelines:**

- **Avoid deeply nested references** - Keep references one level deep from SKILL.md. All reference files should link directly from SKILL.md.
- **Structure longer reference files** - For files longer than 100 lines, include a table of contents at the top so you can see the full scope when previewing.

## Steps (subagents)

A skill can delegate specific **steps** of its workflow to subagents. A subagent is a separate, short-lived agent that executes one step of the skill on the same computer and returns its result to the main agent (via the `execute_step` tool).

**Why steps exist (cost savings).** Running a subagent on a focused step with a small, fresh context (and optionally a cheaper model) is much more cost-efficient than having the main agent carry out repetitive or well-defined steps itself.

**Do not add steps without asking the user first.** Introducing steps changes how the skill executes and deliberately limits the context a step runs with, so only add them when the user has explicitly agreed.

### Declaring steps

Steps are declared in the YAML frontmatter under a `steps` key, mapping each step name to its definition:

```yaml
---
name: salesforce
description: Use this skill to interact with the Salesforce CRM.
steps:
  login:
    description: Navigates to salesforce and logs in.
    required_step_inputs:
      user_name: the Salesforce user name to log in with
      password: the password for that user
---
```

**Colon restriction:** Within the YAML frontmatter, the colon character (`:`) may be used only as the structural separator between a mapping key and its value, as shown in the example above. Never include a colon inside any key or value, including `description` text, step names, input names, input descriptions, or model names. Do not rely on quoting or escaping to include a colon. Rewrite the text to avoid it instead.

Each step's instructions live in a standalone file next to the SKILL.md at `steps/<step_name>.md`. For the example above, the instructions for the `login` step belong in `steps/login.md`:

```
salesforce/
├── SKILL.md
└── steps/
    └── login.md   # <step-by-step instructions for opening Salesforce and logging in>
```

Each step definition has:

- **`description`** (required): a short summary of what the step does. It is documentation for the main agent (and skill author) when choosing which step to run — it is not passed to the subagent. State what the step *returns* here as well, so the orchestrator knows what it gets back.
- **`required_step_inputs`** (optional, but usually required): a map of input name to a short description of what the caller must provide. The main agent passes a value for each of these when it runs the step.
- **`model`** (optional): the model the subagent runs on. Omit to use the default.

The file `steps/<step_name>.md` (its entire contents) becomes the subagent's instructions.

### Defining `required_step_inputs` correctly

This is the most important part to get right. **A subagent starts with a completely fresh context: the only things it ever sees are the step's instruction file and the values declared in `required_step_inputs`.** It has no access to the main agent's conversation, memory, or anything the user said earlier.

Because of that:

- The step file's instructions plus the declared inputs must be **self-sufficient** to complete the task correctly on their own.
- Capture every piece of information the step needs as an input (identifiers, credentials, values to enter, target record names, etc.). If it is neither written in the step file nor passed as an input, the subagent cannot know it.
- Give each input a clear, specific description so the main agent supplies the right value.
- Keep the set minimal but complete: only what the step actually needs, and nothing it needs should be missing.

## Skill Creation Process

The user might want to teach you knowledge, how to use a tool, or a whole workflow.

Basic flow if user teaches workflow:

- Tell user something like: "Let's go through a workflow together using an example"
- User tells you step by step what to do. You try to execute the steps.
- Iteratively write/update the skill files as you progress through the steps of the workflow

### Skill creation location

Create every new skill inside the `/agent/skills/editable/managed-skills` directory.

The resulting structure must be:

/agent/skills/editable/managed-skills/skill-name/SKILL.md

Do not create new skills elsewhere. When updating an existing skill, modify it in its current location.

---

# Workflow methodology

Everything below *applies* only once a skill executes as a multi-step agentic workflow — but read it regardless, before you decide what the skill is. A skill built from scratch often reveals itself as a workflow only partway through, and by then you need to already understand the boundary, contracts, state, and failure classes to avoid building it wrong. The **Steps (subagents)** section above covers the mechanics of declaring a step and its inputs; this section covers how to structure the workflow around those steps.

## The two layers and the boundary

There are two kinds of file with two jobs.

The **SKILL.md orchestrator** holds the plan and the state. It decides what runs, does a few things directly, launches steps, stores what they return, and writes the final summary. It is the only place that sees the whole run.

A **step.md executor** does one bounded job and returns a result. Launched fresh with only its input, a system prompt, and its own file, it must be self-contained for the job it owns (this is the isolation covered under **Defining `required_step_inputs` correctly**).

The single most important structural decision is drawing the **orchestrate-vs-execute boundary and writing it down** — a "Responsibility split" section listing what the orchestrator *decides*, what it *does directly* (without a step), what *steps do*, and the *manual fallback*. When the boundary is implicit, reliability is at risk. The rule: **anything declared as a step must be run as that step**, never re-implemented inline; the orchestrator acts directly only when there is no step for it, or as a manual fallback after a step has already failed.

## One source of truth, placed local to its use

Each fact is defined exactly once. The test is mechanical: if you changed this fact, how many places would you edit? More than one is a latent bug, because copies drift.

**Locality of reference** — define a fact next to where it is consumed, unless it is consumed in several places with no natural owner, in which case centralize it and signpost the consumers. A decision that gates one stage lives inline in that stage.

Two opposite failure modes to police:

- **Duplication** — the same fact in two co-equal places. Collapse to one owner; make the others point to it.
- **Structure without content** — a heading whose body is only a cross-reference. A pointer earns its place only if it prevents a wrong inference; otherwise demote it to a line or cut it.

Keep pointers honest. A pointer that *teaches* ("this value takes precedence; a later stage won't overwrite it") carries information and stays. A pointer that merely redirects ("see the other section") stays short. A pointer that *re-lists* what it points at has re-introduced the duplication — strip it to the bare reference.

## Contracts must match across all three layers

A step's input/output contract appears in three places: the orchestrator's **frontmatter** (`required_step_inputs` + described return), the **stage** that launches it, and the **step file**. These must agree exactly — same field names, shapes, and optionality.

- **No value has two names.** If it is `customer_name_exact` in the workflow, it is not `<client name>` in the step.
- **Reference the exact field, not a vague placeholder.** `<customer_name_exact>` tells the agent precisely what to substitute; "the client name" invites improvisation.
- **After any rename, grep every file** for the old name before considering it done.

A step's returns are part of the contract too: named values in the shape and format the consumers expect, so the orchestrator translates nothing (see **Step-file anatomy**).

## Intake: raw request into named state first

The first stage parses the raw input (message, attached files, files that links resolve to) into a typed **state object** before any other stage runs. Everything downstream reads named fields instead of re-parsing free text.

A good intake stage: lists each field with an **extraction rule** (not just a name); **classifies rather than assumes**; **captures verbatim** where wording matters (preserve original language and order); and **stops on ambiguity instead of guessing**. Make intake the single definition of extraction; a batch path points at it rather than restating it.

## State: produce it, carry it, reuse it — and persist it

Each stage declares what it **Produces**; the orchestrator stores those outputs and reuses them, never recomputing a value an earlier stage produced. Let the `Produces` blocks be the authoritative record of what exists — don't maintain a second enumerated list beside them.

**Normalize at the producer, not in a central mapping point.** The cleanest state needs no normalization stage: each step emits values already in the names, shape, and format its consumers expect, so the orchestrator just carries them.

**Persist durably for long runs.** In a long agentic run the context window is periodically compacted, so state you merely "carry forward" can be silently lost or lossily rewritten. For anything a later step depends on, write it to a durable store (a JSON file) as you go and make that file authoritative: The orchestrator should read that file and use the contents for input for the corresponding step, rather than trusting what's still in context. For shorter runs, in-context carry is fine.

## The produces/consumes graph must balance

The highest-value end-to-end check, walked both ways:

- **Every consumed field has a producer.** A field consumed but never produced is a wiring gap — easy to miss precisely because the field is *defined* somewhere, and being defined is not the same as being produced.
- **Every produced field is consumed.** A field passed to a step but never used is a dead input; an output nobody reads is an orphan. Kill both.

Run this after any change to inputs or outputs; it catches failures that live in the seam between files.

A note on **Needs vs Produces**. A stage's input list (`Needs`) is usually a verbatim subset of the step's frontmatter inputs — pure duplication. A stage's `Produces` is *not* duplicated anywhere. So when slimming a workflow, cut the input echoes and keep `Produces` — it is the half of the wiring with no frontmatter twin. Keep genuine `Needs` only where it adds what the frontmatter cannot: provenance ("read in step 4"), a branch selection, a precondition ("an open profile"), or the inputs of an orchestrator-direct stage. **Exception:** when a *small* model orchestrates, keep the full echo — restating each stage's inputs inline measurably helps a small orchestrator feed the right values, a deliberate cost paid for reliability.

## Stage anatomy in the orchestrator

Give every stage a consistent shape — **Applies / Run / Produces / If failure / Notes** (plus **Needs** where it earns its place). Consistency lets the agent parse each stage the same way.

Make `Applies` state the **actual condition**, not a bare label: "CONDITIONAL — run only when X is present." Each field holds **one kind of thing**: a procedure goes in `Run`; every failure outcome in `If failure`; `Notes` holds only a genuine cross-stage caveat or extra sub-action — never a procedure or failure condition. Don't state a condition twice — let `Applies` own the gate and `Run` name the calls.

## Centralize only what has no natural owner

Centralizing is the exception, not the default; a "shared decisions" section is a magnet for things that don't belong. Before centralizing a decision, test it: *is it consulted by more than one stage, and owned by none of them?* Most candidates fail and resolve to somewhere better:

- **Redundant** — already implied elsewhere. Cut it.
- **Single-stage** — consulted by exactly one stage. Inline it there (keep a value's earlier origin as a one-line parenthetical).
- **Decided in one place, read elsewhere** — put it in the deciding stage; others consume the produced value, not the rule.
- **Splittable by data-availability** — a precedence rule whose branches depend on inputs arriving at different times splits: intake-available branches go to intake, the branch needing a later output goes to that stage.

## Step-file anatomy

Give every step file a consistent structure — **Mission / Inputs / Instructions / Return value / Important** — and make it self-contained for its one job.

- `Inputs` and `Return value` must match the contract.
- **Emit consumer-ready values — the orchestrator should translate nothing.** Return values already in the names, shape, and format consumers expect. When you find the orchestrator post-processing a step's output, fix the step.
- **Return named values** Never return a bare tuple — the orchestrator would decode it by position (brittle, silently wrong if order changes).
- **Reference shared rules rather than restating them** — one "matching rules" block the instructions point to, not the rule repeated per branch.
- The **Important** block is a short recap of the few genuinely critical, easily-forgotten constraints (the irreversible action, the must-happen save) — not a re-listing of the instructions.

**Cross-file rule.** A step can't see the SKILL — so it never re-decides a shared convention (it doesn't invent its own empty-value scheme), but anything a convention *governs* must be written into the step file or carried in its input, because there is no other channel. A SKILL convention that shapes a step's output is restated concretely at the point of use.

## Write for the agent that executes

The consumer is the model running the workflow. A section that exists only to help a human skim — a redundant index, a paraphrased summary of another section — is, for the agent, just more tokens and another copy to drift; cut it or reduce it to something that can't drift. Use minimal, consistent formatting. Write plain instructions rather than explaining behavior by appeal to the file's own structure.

## Batch / fan-out

Structure a multi-item run as **intake normalization → ambiguity filtering → one primary-workflow run per item → aggregation**. Each child run acts as the orchestrator for its own item. Parallelize across items, never within one item unless the steps are genuinely independent. Point the batch path at the single intake and step definitions rather than restating them.

---

# Schemas

Build a new SKILL.md or step.md against these skeletons. Section names are a convention; what matters is that each role is filled. Sections are **core** (nearly every workflow), **conditional** (only when the workflow has that concern), or **rare** (usually absent — create only when a specific test is met).

## Schema — SKILL.md (the orchestrator)

### Frontmatter (YAML)

| Key | Purpose |
| --- | --- |
| `name` | the skill's identifier |
| `description` | when to trigger **and when not to** |
| `steps:` | map of step name → contract: `description` (what it does and returns), `required_step_inputs` (each input → a one-line contract), `model:` (the model the subagent runs on) |

### Body sections, in order — reference material first, temporal spine last

| # | Section | Status | Role |
| --- | --- | --- | --- |
| 1 | `# <name>` + one-line purpose | core | title and a one-line statement of what the skill does |
| 2 | `## How to use this file` | core | the genuine how-to — read the reference sections, run the spine (or the batch path for many items). Should not be a repetition of any detailed instructions laid out elsewhere in-detail |
| 3 | `## Scope` | core | in / out of scope of the workflow / skill |
| 4 | `## <Rules / Conventions>` | core | one umbrella for every always-on convention (subsections below) |
| 5 | `## Responsibility split` | core | decides / does directly / steps do / manual fallback — the written boundary; keep genuine choices in *decides*, genuine activities in *does directly*, never both |
| 6 | `## Shared decisions` | rare | only for a decision genuinely consulted by several stages with no owner; most workflows have none — test every candidate and expect it to dissolve |
| 7 | `## <Primary> workflow` | core | the numbered spine; each stage follows the stage-block schema below |
| 8 | `## Batch workflow` | conditional | fan-out only, pointing at intake |
| 9 | `## Final output format` | conditional | template + output rules; also terminal, kept at the end |

### The Rules / Conventions umbrella

One home rather than five or six top-level sections. Might hold subsections such as these:

- `### General` — domain terms and always-true rules
- `### Platform` — how the target system behaves
- `### Data` — value formats and handling
- `### File / artifact` — any file-reading rules that might apply
- `### Failure-handling rules` - how to handle certain types of failures

### Stage-block schema

Each stage is a `### N. <verb-first name>` heading with these fields, each holding one kind of thing:

| Field | Holds |
| --- | --- |
| `Applies:` | `ALWAYS`, or the actual condition stated inline |
| `Run:` | the step to launch, or the orchestrator-direct procedure itself |
| `Produces:` | the outputs entering state |
| `If failure:` | every failure outcome |
| `Notes:` | only cross-stage caveats or an extra sub-action |

## Schema — step.md (an executor)

- **Heading** — `### <step title>`.
- **`Mission:`** — one or two lines: the single job and what it returns. *core.*
- **`Inputs:`** — each input; must match the frontmatter and the launching stage. *core.*
- **`Instructions:`** — the ordered procedure. Reference a shared block rather than restating a rule used in several places. *core.*
- **`Return value:`** — each field returned as a named value, in the shape/format the consumer expects, with the empty/absent case stated. Must match what the stage consumes. *core.*
- **`Important:`** — a short recap of the few critical, easily-forgotten constraints. *conditional but common.*
- **Optional blocks as warranted:** `General rules:`; branch subsections (`#### If <case>`, keeping the shared tail common); named shared blocks; a possible-outcomes enumeration when several end states are valid.

**Cross-file rule.** A step gets only its inputs, a system prompt, and its own file. So it never re-decides a shared convention, but anything a convention governs must be written into the step file or carried in its input.
