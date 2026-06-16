---
name: skill-creation
description: Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends your capabilities with specialized knowledge, workflows, or tool integrations.
---

# Skill Creator

### Anatomy of a Skill

Every skill consists of a required SKILL.md file and optional bundled resources:

```
skill-name/
??? SKILL.md (required)
?   ??? YAML frontmatter metadata (required)
?   ?   ??? name: (required)
?   ?   ??? description: (required)
?   ??? Markdown instructions (required)
??? Bundled Resources (optional)
    ??? scripts/          - Executable code (Python/Bash/etc.)
    ??? references/       - Documentation intended to be loaded into context as needed
    ??? assets/           - Files used in output (templates, icons, fonts, etc.)
```

#### SKILL.md (required)

Every SKILL.md consists of:

- **Frontmatter** (YAML): Contains `name` and `description` fields. It is very important to be clear and comprehensive in describing what the skill is, and when it should be used.
- **Body** (Markdown): Instructions and guidance for using the skill. Only loaded AFTER the skill triggers (if at all).

#### Bundled Resources (optional)

##### Scripts (`scripts/`)

Executable code (Python/Bash/etc.) for tasks that require deterministic reliability or are repeatedly rewritten.

- **When to include**: When the same code is being rewritten repeatedly or deterministic reliability is needed
- **Example**: `scripts/rotate_pdf.py` for PDF rotation tasks
- **Benefits**: Token efficient, deterministic, may be executed without loading into context

##### References (`references/`)

Documentation and reference material intended to be loaded as needed into context to inform your process and thinking.

- **When to include**: For documentation that should be reference while working
- **Examples**: `references/finance.md` for financial schemas, `references/mnda.md` for company NDA template, `references/policies.md` for company policies, `references/api_docs.md` for API specifications
- **Use cases**: Database schemas, API documentation, domain knowledge, company policies, detailed workflow guides
- **Benefits**: Keeps SKILL.md lean, loaded only when you determine it's needed
- **Best practice**: If files are large (>10k words), include grep search patterns in SKILL.md
- **Avoid duplication**: Information should live in either SKILL.md or references files, not both. Prefer references files for detailed information unless it's truly core to the skill?this keeps SKILL.md lean while making information discoverable without hogging the context window. Keep only essential procedural instructions and workflow guidance in SKILL.md; move detailed reference material, schemas, and examples to references files.

##### Assets (`assets/`)

Files not intended to be loaded into context, but rather used to procude output.

- **When to include**: When the skill needs files that will be used in the final output
- **Examples**: `assets/logo.png` for brand assets, `assets/slides.pptx` for PowerPoint templates, `assets/frontend-template/` for HTML/React boilerplate, `assets/font.ttf` for typography
- **Use cases**: Templates, images, icons, boilerplate code, fonts, sample documents that get copied or modified
- **Benefits**: Separates output resources from documentation, enables you to use files without loading them into context

### Progressive Disclosure Design Principle

Skills use a three-level loading system to manage context efficiently:

1. **Metadata (name + description)** - Always in context (~100 words)
2. **SKILL.md body** - When skill triggers (<5k words)
3. **Bundled resources** - As needed (Unlimited because scripts can be executed without reading into context window)

#### Progressive Disclosure Patterns

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
??? SKILL.md (overview and navigation)
??? reference/
    ??? finance.md (revenue, billing metrics)
    ??? sales.md (opportunities, pipeline)
    ??? product.md (API usage, features)
    ??? marketing.md (campaigns, attribution)
```

When a user asks about sales metrics, you only read sales.md.

Similarly, for skills supporting multiple frameworks or variants, organize by variant:

```
cloud-deploy/
??? SKILL.md (workflow + provider selection)
??? references/
    ??? aws.md (AWS deployment patterns)
    ??? gcp.md (GCP deployment patterns)
    ??? azure.md (Azure deployment patterns)
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

Each step's instructions live in a standalone file next to the SKILL.md at `steps/<step_name>.md`. For the example above, the instructions for the `login` step belong in `steps/login.md`:

```
salesforce/
├── SKILL.md
└── steps/
    └── login.md   # <step-by-step instructions for opening Salesforce and logging in>
```

Each step definition has:

- **`description`** (required): a short summary of what the step does. It is documentation for the main agent (and skill author) when choosing which step to run — it is not passed to the subagent.
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

