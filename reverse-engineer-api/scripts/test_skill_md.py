#!/usr/bin/env python3
# Unit tests for SKILL.md's gate-driven, strictly-generic shape. Runs with plain `python` (no pytest, no
# browser, no other in-flight module). It reads the on-disk SKILL.md + references/examples.md as fixtures
# and asserts the structure DESIGN §7 mandates: 3 Iron Laws, a tick-box checklist whose every box is a
# command + a gate token, teach_insert gated behind the passes, a generic rationalizations table, per-step
# notes, a References pointer — and ZERO app/artifact/protocol names anywhere in SKILL.md itself.
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SKILL = open(os.path.join(_ROOT, "SKILL.md"), encoding="utf-8").read()
EXAMPLES = open(os.path.join(_ROOT, "references", "examples.md"), encoding="utf-8").read()

# Names that must NEVER appear in SKILL.md — concrete apps, protocols, artifacts, auth schemes. The method
# is app-agnostic; these belong only in references/examples.md, clearly labelled "illustrative".
FORBIDDEN_IN_SKILL = [
    "wave", "metaview", "alphaskill", "browserbase",
    "graphql", "grpc", "soap",
    "pdf", "csv", "xlsx", "png", "jpeg", "zip",
    "s3", "cloudflare", "datadome", "akamai", "perimeterx",
    "invoice", "template",  # app-domain nouns the old SKILL leaned on
]

# `_engine/` is the vendored Browserbase MIT engine; naming the directory in the script list is unavoidable
# and is NOT an app/protocol name. Strip those mentions before the genericness scan.
SKILL_SCAN = re.sub(r"`_engine/`,?\s*Browserbase[^)]*", "", SKILL, flags=re.I)


def _frontmatter(md: str) -> str:
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", md, re.DOTALL)
    assert m, "SKILL.md must open with YAML frontmatter"
    return m.group(1)


# ---- frontmatter ----
def test_frontmatter_present():
    fm = _frontmatter(SKILL)
    assert "name: reverse-engineer-api" in fm

def test_frontmatter_has_description():
    fm = _frontmatter(SKILL)
    assert re.search(r"^description:", fm, re.M), "description frontmatter must be kept"


# ---- 3 Iron Laws, in delete-and-restart form ----
def test_exactly_three_iron_laws():
    laws = re.findall(r"^\s*(\d)\.\s+\*\*", SKILL, re.M)
    # the three numbered, bolded laws under "The three Iron Laws"
    section = SKILL.split("The three Iron Laws", 1)[1].split("## Inputs", 1)[0]
    nums = re.findall(r"^\s*(\d)\.\s+\*\*", section, re.M)
    assert nums == ["1", "2", "3"], f"expected exactly 3 Iron Laws, got {nums}"

def test_iron_laws_are_delete_and_restart():
    section = SKILL.split("The three Iron Laws", 1)[1].split("## Inputs", 1)[0]
    assert "delete-and-restart" in section
    # each law must carry a delete/restart imperative, not just prose
    assert section.lower().count("delete") >= 3, "each Iron Law must state the delete-and-restart move"

def test_iron_law_unit_is_segment():
    assert re.search(r"unit is a SEGMENT", SKILL, re.I)

def test_iron_law_replay_faithfully_prove_judges():
    # Iron Law 2 is now "replay faithfully; PROVE judges" — an unexplained value is replayed verbatim, not a
    # stop; the one classify-level stop is BAIL-1, and PROVE is the arbiter.
    assert "verbatim" in SKILL and "BAIL-1" in SKILL and "PROVE" in SKILL

def test_iron_law_prove_and_bail_is_success():
    assert re.search(r"PROVEN", SKILL) and re.search(r"bail.{0,30}success", SKILL, re.I | re.S)


# ---- the tick-box CHECKLIST ----
def _checklist() -> str:
    m = re.search(r"```(.*?)```", SKILL.split("The CHECKLIST", 1)[1], re.DOTALL)
    assert m, "the CHECKLIST must contain a fenced tick-box block"
    return m.group(1)

def test_checklist_boxes_zero_through_nine():
    cl = _checklist()
    boxes = re.findall(r"^\[ \]\s+(\d)\b", cl, re.M)
    assert boxes == [str(i) for i in range(10)], f"expected boxes 0..9 in order, got {boxes}"

def test_every_box_has_a_command():
    cl = _checklist()
    # each box is followed by an actual command (a python/printf/git/author invocation), not just prose
    for box, name in [("0", "partition.py"), ("1", "capture_cdp.py"), ("2", "analyze.py"),
                      ("3", "detect_replayable.py"), ("4", "classify_values.py"), ("5", "probe_auth.py"),
                      ("7", "prove_runner.py"), ("8", "teach_insert.py"), ("9", "git ")]:
        assert name in cl, f"box {box} must name its command {name!r}"

def test_every_box_has_a_gate_token():
    cl = _checklist()
    # boxes 0..9 each declare a GATE / outcome the operator branches on
    assert cl.count("GATE:") >= 8, "nearly every box must print a GATE: line with the required token"

def test_gates_name_the_output_tokens():
    cl = _checklist()
    for token in ("segment_ids != []", 'verdict == "API-CANDIDATE"', "BAIL-1",
                  "working == true", 'verdict == "PROVEN"'):
        assert token in cl, f"a gate must check the literal output token {token!r}"

def test_keep_ui_is_an_explicit_exit():
    cl = _checklist()
    assert cl.count("KEEP UI") >= 5, "multiple boxes must offer an explicit '→ KEEP UI' exit"

def test_teach_insert_gated_behind_passes():
    cl = _checklist()
    # box 8 (teach_insert) must be reachable ONLY after the gates pass
    box8 = cl.split("[ ]  8", 1)[1] if "[ ]  8" in cl else cl.split("] 8", 1)[1]
    box8 = box8.split("[ ]  9", 1)[0] if "[ ]  9" in box8 else box8.split("] 9", 1)[0]
    assert re.search(r"only on box-7\s+PROVEN", box8, re.I), "box 8 must say it runs only on box-7 PROVEN"
    assert re.search(r"do NOT run", box8), "box 8 must say: on KEEP UI, do NOT run teach_insert"

def test_result_line_calls_both_outcomes_correct():
    cl = _checklist()
    assert re.search(r"api-added.*kept-ui.*BOTH are correct", cl, re.S | re.I)


# ---- rationalizations -> required action table (generic) ----
def _rationalizations() -> str:
    return SKILL.split("Known rationalizations", 1)[1].split("## Per-step notes", 1)[0]

def test_rationalizations_table_present():
    tbl = _rationalizations()
    assert "Required action" in tbl
    rows = [ln for ln in tbl.splitlines() if ln.strip().startswith("|") and "Required action" not in ln
            and set(ln.strip()) - set("|-: ")]
    assert len(rows) >= 6, f"expected >=6 rationalization rows, got {len(rows)}"

def test_rationalizations_cover_known_traps():
    tbl = _rationalizations().lower()
    assert "worked once" in tbl                      # "it worked once"
    assert "sleep" in tbl or "settimeout" in tbl     # "I'll add a sleep"
    assert "already exists" in tbl                    # "the thing already exists, just fetch it"

def test_rationalizations_are_generic():
    tbl = _rationalizations()
    for bad in FORBIDDEN_IN_SKILL:
        assert not re.search(rf"\b{re.escape(bad)}\b", tbl, re.I), f"rationalization names {bad!r} — must be generic"


# ---- per-step notes + references ----
def test_per_step_notes_cover_every_box():
    notes = SKILL.split("## Per-step notes", 1)[1].split("## Safety", 1)[0]
    for i in range(10):
        assert f"Box {i} " in notes or f"Box {i}—" in notes or f"Box {i} —" in notes, f"missing note for box {i}"

def test_references_point_to_examples_and_hard_cases():
    refs = SKILL.split("## References", 1)[1]
    assert "references/examples.md" in refs
    assert "references/hard-cases.md" in refs


# ---- STRICT GENERICNESS: zero app/artifact/protocol names in SKILL.md ----
def test_skill_md_names_no_app_artifact_or_protocol():
    hits = []
    for bad in FORBIDDEN_IN_SKILL:
        for m in re.finditer(rf"\b{re.escape(bad)}\b", SKILL_SCAN, re.I):
            hits.append((bad, m.start()))
    assert not hits, f"SKILL.md must be strictly generic; found app/artifact/protocol names: {sorted({h[0] for h in hits})}"

def test_skill_md_has_no_worked_example_block():
    # worked examples were moved OUT to references/examples.md
    assert "Worked example" not in SKILL and "## Example" not in SKILL


# ---- references/examples.md carries the moved, LABELLED examples ----
def test_examples_file_has_three_worked_examples():
    headers = re.findall(r"^##\s+Example\s+[A-C]\b", EXAMPLES, re.M)
    assert len(headers) == 3, f"expected 3 worked examples (A,B,C), got {headers}"

def test_each_example_labelled_illustrative_app_agnostic():
    count = len(re.findall(r"[Ii]llustrative\s*[—-]\s*the method is app-agnostic", EXAMPLES))
    assert count >= 3, f"each example must be labelled 'illustrative — the method is app-agnostic' ({count} found)"

def test_examples_include_rest_job_poll_and_named_concrete_instances():
    # the concrete app/protocol names are ALLOWED here (clearly labelled illustrative), unlike in SKILL.md
    assert re.search(r"job.?poll|/jobs/|status.{0,20}COMPLETE", EXAMPLES, re.I), "needs a REST job-poll example"
    low = EXAMPLES.lower()
    assert "wave" in low and "metaview" in low, "examples may name the concrete instances (Wave/Metaview)"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'} ({len(tests)} tests)")
    sys.exit(1 if failed else 0)
