#!/usr/bin/env python3
# Tests the docs component's load-bearing invariants. Plain `python3`, no pytest, no browser, no other
# in-flight module. Reads the doc fixtures from the repo and asserts the contract this rebuild froze:
# the operator playbook stays at OPERATOR altitude (no internal commands; the agent's checklist + gates live
# in SKILL.md/DESIGN.md), the single-home cautionary tale, the fixed step.md bug, the README script list,
# and the internal label on test-plan.md.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = (ROOT / "docs" / "operator-playbook.md").read_text(encoding="utf-8")
TEACH = (ROOT / "docs" / "templates" / "teach-prompt.md").read_text(encoding="utf-8")
STEP = (ROOT / "docs" / "templates" / "step.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
TEST_PLAN = (ROOT / "docs" / "test-plan.md").read_text(encoding="utf-8")


# ---- operator-playbook: Leo's SIMPLE operator surface ----
# The agent's internal checklist + gate table are locked in SKILL.md (test_skill_md.py) and DESIGN.md; the
# capture disciplines (whole-segment / varied inputs / gate-before-write) are locked in the teach prompt
# (test_teach_prompt_carries_the_disciplines). The playbook must stay at OPERATOR altitude — a drive-and-review
# flow, never a list of internal commands Leo is asked to run.
def test_playbook_is_operator_altitude() -> None:
    # Leo never runs the pipeline by hand -> the internal command names must NOT appear as operator steps.
    for internal in ("partition.py", "classify_values.py", "prove_runner.py", "capture_cdp.py",
                      "analyze.py", "probe_auth.py", "teach_insert.py"):
        assert internal not in PLAYBOOK, f"operator playbook leaked the internal command {internal!r}"
    # it carries the simple drive-and-review flow instead
    for marker in ("Warm up", "Teach", "Review", "Test it for real", "slash-free", "git diff"):
        assert marker in PLAYBOOK, f"operator flow marker {marker!r} missing"
    # and points the curious to the real procedure rather than reproducing it
    assert "SKILL.md" in PLAYBOOK and "DESIGN.md" in PLAYBOOK

def test_playbook_both_outcomes_are_correct() -> None:
    assert "api-added" in PLAYBOOK and "kept-ui" in PLAYBOOK
    assert "both correct" in PLAYBOOK  # neither outcome is an error
    assert "NOT a failure" in PLAYBOOK  # kept-ui framed as success


# ---- the cautionary tale lives in EXACTLY ONE place ----
def test_cautionary_tale_single_home() -> None:
    # the narrative ("the one bug this whole playbook exists to prevent") appears once, in the playbook
    assert PLAYBOOK.count("## The cautionary tale") == 1
    # the other docs reference that home, they do not re-tell it
    assert "cautionary tale in docs/operator-playbook.md" in STEP
    assert "cautionary tale in docs/operator-playbook.md" in TEACH
    # the tale stays generic — no app/protocol/artifact baked into the narrative body
    tale = PLAYBOOK.split("## The cautionary tale", 1)[1].split("##", 1)[0]
    for banned in ("One Pager", "Metaview", "Wave", "GraphQL"):
        assert banned not in tale, f"cautionary tale leaked {banned!r} — keep it generic"


# ---- step.md: the FIXED bug ----
def test_step_template_does_not_say_only_last_action() -> None:
    # the old, wrong framing: "the LAST data action ... that's the one teaching mode turns into an API call"
    assert "that's the one teaching mode\n    turns into an API call" not in STEP
    assert "this is what gets API-ified" not in STEP

def test_step_template_says_whole_segment_including_setup() -> None:
    assert "WHOLE data segment, including any SETUP" in STEP
    assert "captures the ENTIRE\n    chain" in STEP

def test_step_template_is_generic() -> None:
    # the UI-step template is filled per app; it must not bake one app's UI phrasing into the slots.
    for banned in ("apply a template", '"Saved"', "One Pager", "Metaview", "Wave", "GraphQL"):
        assert banned not in STEP, f"step template leaked {banned!r} — keep slots app-agnostic"


# ---- teach-prompt: short, generic, gate-disciplined ----
def test_teach_prompt_is_generic() -> None:
    # app/protocol names AND app-specific UI phrasing only inside the clearly-labeled example block, never in
    # the reusable prompt (a literal UI string like "Saved" or "apply a template" is one app's flavour).
    head = TEACH.split("## Filled example", 1)[0]
    for banned in ("Metaview", "Wave", "GraphQL", "One Pager", '"Saved"', "apply a template", "template applied"):
        assert banned not in head, f"reusable teach prompt leaked {banned!r}"

def test_teach_prompt_carries_the_disciplines() -> None:
    assert "WHOLE SEGMENT FROM A CLEAN STATE" in TEACH
    assert "varied inputs" in TEACH
    assert "NOT run teach_insert" in TEACH and "until the real gates" in TEACH
    assert "SKILL.md" in TEACH and "checklist" in TEACH.lower()  # the agent works the SKILL.md checklist

def test_teach_prompt_example_is_labeled() -> None:
    assert "illustrative only" in TEACH


# ---- README: the bundled-scripts list ----
def test_readme_lists_all_bundled_scripts() -> None:
    for script in (
        "partition.py", "classify_values.py", "prove_runner.py", "recombine.py",
        "check_chain.py", "verify_equivalence.py", "capture_cdp.py", "analyze.py",
        "detect_replayable.py", "probe_auth.py", "run_in_page.py", "teach_insert.py",
    ):
        assert script in README, f"README does not list {script}"

def test_readme_points_to_design_and_playbook() -> None:
    assert "docs/DESIGN.md" in README
    assert "docs/operator-playbook.md" in README

def test_readme_verify_receipt_described() -> None:
    assert "verify_receipt.json" in README  # the receipt the prove gate emits


# ---- test-plan: labeled internal ----
def test_test_plan_labeled_internal() -> None:
    head = TEST_PLAN[:600]
    assert "INTERNAL" in head
    assert "maintainers only" in head
    assert "operator-playbook.md" in head  # points operators away to their doc


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'} ({len(tests)} tests)")
    sys.exit(1 if failed else 0)
