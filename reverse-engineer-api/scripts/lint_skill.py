#!/usr/bin/env python3
# lint_skill — OPTIONAL CI consistency check for the reverse-engineer-api single-file pattern.
# NOT part of the teaching procedure (the method enforces shape mechanically via teach_insert.py). Run it
# in CI if you want a second guard. Exit 0 = clean, 1 = violations.
#
#   python lint_skill.py <skill-dir>        # e.g. .../wave
#
# Model: one file per step. A plain step is UI prose. An API-backed step is the SAME file with a one-line
# provenance header comment, an `## API` section (one run-in-page call), the original `## UI` fallback,
# and a `## Report` block. No `.ui.md`/`.capture.json` sidecars; no `-api.md` siblings.

from __future__ import annotations

import json  # noqa: F401  (kept for callers/tests that import json via this module)
import os
import re
import sys
from typing import Any

from run_in_page import CONTRACT_VERSION, classify

# Closed Model union (sub_agents.py / domain.persona). A glob is NOT acceptable.
_MODEL_BASES = ("claude-sonnet-4-6", "claude-opus-4-6", "claude-opus-4-7", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini")
_MODEL_EFFORTS = ("", "-low", "-medium", "-high", "-xhigh")
VALID_MODELS = {b + e for b in _MODEL_BASES for e in _MODEL_EFFORTS}

# Login email/password ARE allowed inline in UI steps — the agent logs in with them (like the
# Alphaskill/Metaview steps). What stays forbidden everywhere is runtime API auth that run-in-page must
# re-source live: bearer/JWT/cookie/api-key/signed-url/opaque tokens.
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}")  # dotted JWT (eyJ…)
LITERAL_BEARER_RE = re.compile(r"Bearer\s+(?!\$\{|\{\{)[A-Za-z0-9._\-]{16,}")
SIGNED_URL_RE = re.compile(r"[?&](X-Amz-Signature|Signature|sig|token)=[A-Za-z0-9%._\-]{16,}", re.I)
SECRET_ASSIGN_RE = re.compile(
    r"""(authorization|bearer|cookie|x-[a-z-]*token|\btoken|api[_-]?key|password|secret)\s*[:=]\s*["'](?!\$\{|\{\{)[A-Za-z0-9+/=._\-]{12,}["']""",
    re.I,
)
OPAQUE_LITERAL_RE = re.compile(r"""["'`]([A-Za-z0-9+/=._\-]{40,})["'`]""")  # incl. '.' to catch dotted tokens/JWTs
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class V:  # a violation
    def __init__(self, rule: str, where: str, msg: str) -> None:
        self.rule, self.where, self.msg = rule, where, msg

    def __str__(self) -> str:
        return f"[{self.rule}] {self.where}: {self.msg}"


def _parse_frontmatter(md: str) -> tuple[dict[str, Any], str]:
    m = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", md, re.DOTALL)
    if not m:
        raise ValueError("missing YAML frontmatter")
    import yaml
    raw = yaml.safe_load(m.group(1))
    if not isinstance(raw, dict):
        raise ValueError("frontmatter is not a mapping")
    return raw, md[m.end():]


def _section(md: str, heading: str) -> str:
    """Body of a `## heading` section, up to the next top-level `## ` heading (or EOF)."""
    if heading not in md:
        return ""
    after = md.split(heading, 1)[1]
    m = re.search(r"\n##\s", after)
    return after[: m.start()] if m else after


def _scan_secrets(rule: str, where: str, text: str) -> list[V]:
    out: list[V] = []
    for label, rx in (("jwt", JWT_RE), ("literal-bearer", LITERAL_BEARER_RE),
                      ("signed-url", SIGNED_URL_RE), ("secret-assign", SECRET_ASSIGN_RE)):
        if rx.search(text):
            out.append(V(rule, where, f"possible {label} literal — secrets/identity must never be committed"))
    for m in OPAQUE_LITERAL_RE.finditer(text):
        tok = m.group(1)
        if "{{" in tok or UUID_RE.match(tok) or set(tok) <= set("0123456789"):
            continue  # a template var, a real UUID, or pure digits — not a secret
        out.append(V(rule, where, f"long opaque literal {tok[:12]}… — looks like a token; use a live re-source recipe"))
    return out


def lint_skill(skill_dir: str) -> list[V]:
    out: list[V] = []
    skill_md = os.path.join(skill_dir, "SKILL.md")
    steps_dir = os.path.join(skill_dir, "steps")
    if not os.path.exists(skill_md):
        return [V("structure", skill_dir, "no SKILL.md")]

    try:
        fm, _body = _parse_frontmatter(open(skill_md).read())
    except ValueError as e:
        return [V("frontmatter", "SKILL.md", str(e))]

    out += _scan_secrets("no-secrets-no-identity", "SKILL.md", open(skill_md).read())  # secrets are repo-wide

    name = fm.get("name", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(name)):
        out.append(V("skill-hygiene", "SKILL.md", f"name {name!r} must match [A-Za-z0-9._-], <=64"))
    if len(str(fm.get("description", ""))) > 1024:
        out.append(V("skill-hygiene", "SKILL.md", "description > 1024 chars"))

    declared = fm.get("steps", {}) or {}
    for sname, spec in declared.items():
        mdl = (spec or {}).get("model")
        if mdl is not None and mdl not in VALID_MODELS:
            out.append(V("model-enum-exact", "SKILL.md", f"step {sname}: model {mdl!r} not in the closed Model union"))

    if not os.path.isdir(steps_dir):
        out.append(V("structure", skill_dir, "no steps/ dir"))
        return out

    # one file per step: no sidecars, no -api.md sibling, every step file declared
    for fn in sorted(os.listdir(steps_dir)):
        if fn.endswith(".ui.md") or fn.endswith(".capture.json"):
            out.append(V("no-sidecars", fn, "single-file model: delete sidecars (provenance is the header; the UI lives in ## UI)"))
            continue
        if not fn.endswith(".md"):
            continue
        if fn.endswith("-api.md"):
            out.append(V("no-undeclared-sibling", fn, "forbidden -api.md sibling (inline the API into the declared step)"))
            continue
        step = fn[:-3]
        if step not in declared:
            out.append(V("every-step-declared", fn, "step file not declared under SKILL.md steps:"))

    for step, spec in declared.items():
        out += _lint_step(steps_dir, step, spec or {})
    return out


def _lint_step(steps_dir: str, step: str, spec: dict[str, Any]) -> list[V]:
    out: list[V] = []
    step_md_p = os.path.join(steps_dir, f"{step}.md")
    if not os.path.exists(step_md_p):
        return [V("structure", f"{step}.md", "declared step has no file")]
    step_md = open(step_md_p).read()
    out += _scan_secrets("no-secrets-no-identity", f"{step}.md", step_md)  # secrets hazard every step, not just API

    # A UI-only step (no run-in-page) needs nothing more.
    if "run-in-page" not in step_md:
        return out

    # ---- API-backed step ----
    hm = re.search(r"<!--(.*?)-->", step_md, re.DOTALL)
    header = hm.group(1) if hm else ""

    for sec in ("## API attempt", "## UI instructions"):
        if sec not in step_md:
            out.append(V("single-file-sections", f"{step}.md", f"API step missing '{sec}' section"))
    if "Return value:" not in step_md:
        out.append(V("single-file-sections", f"{step}.md", "API step missing a 'Return value:' block"))
    api = _section(step_md, "## API attempt")

    # helper-by-name / one-call-in-api / no-repo-paths / inputs-not-envvars
    if "run-in-page" not in api:
        out.append(V("helper-by-name-only", f"{step}.md", "the run-in-page call must live in the ## API attempt section"))
    if "/agent/skills/" in step_md or "replay_in_page" in step_md:
        out.append(V("helper-by-name-only", f"{step}.md", "skill-relative helper path — call run-in-page by name"))
    for bad in ("--js-file", ".capture.json", ".ui.md", "/tmp/", "-result.json"):
        if bad in api:
            out.append(V("no-runtime-repo-paths", f"{step}.md", f"## API references {bad!r} — inline the JS, no file handoff"))
    if re.search(r"\$[A-Z_]{2,}\b", api):
        out.append(V("inputs-exported-not-envvars", f"{step}.md", "shell $VAR in ## API — step_inputs are not env vars; build --vars-json"))
    if "base64" in api.lower() and "--out" not in api:
        out.append(V("one-call-one-branch", f"{step}.md", "base64 payload without --out (the helper writes binaries to --out)"))

    # helper-contract-pinned (== installed CONTRACT_VERSION)
    cm = re.search(r"--contract\s+(\d+)", api)
    if not cm:
        out.append(V("helper-contract-pinned", f"{step}.md", "## API missing --contract N"))
    elif int(cm.group(1)) != CONTRACT_VERSION:
        out.append(V("helper-contract-pinned", f"{step}.md", f"--contract {cm.group(1)} != installed helper {CONTRACT_VERSION}"))

    # classify the in-page fetch (single source of truth = the helper's own classifier)
    jm = re.search(r"--js\s+'(.*?)'\s*```", api, re.DOTALL) or re.search(r"--js\s+'(.*)", api, re.DOTALL)
    js = jm.group(1) if jm else api
    derived = classify(js)

    # provenance header: class / approved / validated
    hclass = re.search(r"class\s+(READ|WRITE)", header, re.I)
    if not hclass:
        out.append(V("provenance-header", f"{step}.md", "header comment must state 'class READ|WRITE'"))
    if not re.search(r"validated:\s*\S", header):
        out.append(V("provenance-header", f"{step}.md", "header comment must state 'validated: <state>'"))
    header_class = hclass.group(1).lower() if hclass else None

    # class-derived-from-body: a WRITE may not be under-labelled READ
    if header_class == "read" and derived in ("write", "unknown"):
        out.append(V("class-derived-from-body", f"{step}.md", f"header says READ but the body derives {derived} (mutation/unclassified)"))

    # write gate: bare --allow-mutation + a recorded approver
    has_bare_flag = bool(re.search(r"(?<!=)--allow-mutation(?:\s|$|\\)", api)) and "--allow-mutation=" not in api
    is_write = derived in ("write", "unknown") or header_class == "write"
    if is_write:
        if not re.search(r"approved:\s*\S", header):
            out.append(V("write-requires-approval", f"{step}.md", "WRITE step header must record 'approved: <human> (<why-safe>)'"))
        if not has_bare_flag:
            out.append(V("write-requires-approval", f"{step}.md", "WRITE step must pass a bare --allow-mutation flag"))
    elif has_bare_flag:
        out.append(V("write-requires-approval", f"{step}.md", "READ step must not pass --allow-mutation"))

    # validated-state-honest: a WRITE 'validated' against production is forbidden
    if is_write and re.search(r"\bproduction\b", header, re.I) and "not" not in header.lower():
        out.append(V("validated-state-honest", f"{step}.md", "a WRITE validated against production is forbidden"))

    # inputs declared & referenced
    declared_inputs = set((spec.get("required_step_inputs") or {}).keys())
    used_vars = set(re.findall(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}", step_md))
    for v in sorted(used_vars - declared_inputs):
        out.append(V("inputs-declared-and-referenced", f"{step}.md", f"uses {{{{{v}}}}} but it is not a required_step_input"))
    # NOTE: allow_mutation is a header approval record + the hardcoded --allow-mutation flag, NOT a runtime
    # input — the mechanical insert edits only the step file, never SKILL.md. The write-gate stays enforced
    # by the bare flag (above) + a recorded approver in the header.

    return out


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: lint_skill.py <skill-dir>", file=sys.stderr)
        return 2
    violations = lint_skill(argv[0])
    for v in violations:
        print(str(v))
    print(f"\n{'CLEAN' if not violations else f'{len(violations)} VIOLATION(S)'} — {argv[0]}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
