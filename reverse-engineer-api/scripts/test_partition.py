#!/usr/bin/env python3
# Tests for partition.py — S0 PARTITION. Runs with plain `python` (no pytest, no live browser). Synthetic
# mission-style step files exercise: verb-prior nature classification, maximal DATA_WORK coalescing into
# stable-id ApiSegments, pure-NAVIGATE absorption, 0-segment (all-UI) workflows, and the typed handoff graph.
import sys

import partition as p

# ---- Synthetic step fixtures (mission-style; the numbered Instructions block IS the workflow W) ----

# Mixed: login (FUZZY) + navigate (UI) -> apply + export (DATA_WORK segment) -> review (UI). Generic verbs.
MIXED = """### Produce an artifact

Mission: turn the given record into a downloadable artifact.

Inputs:
- record_id: the record id.
- account_id: the account id.

Instructions:
1. Open Chrome and log in.
2. Navigate to the record list.
3. Apply the chosen template to the record.
4. Wait for it to save, then export the result as a file.
5. Review the downloaded file looks right.

Return value:
- note_file_path: the path, or NAN.

Important:
- Read-only where possible.
"""

# All-UI: nothing but navigation and reading -> 0 segments -> KEEP UI.
ALL_UI = """### Look something up

Mission: read a value off a page.

Inputs:
- record_id: the record id.

Instructions:
1. Open Chrome.
2. Navigate to the record.
3. Read the displayed total.

Return value:
- total: the number, or NAN.
"""

# A pure NAVIGATE sits BETWEEN two data-work actions -> must be ABSORBED so the chain is one segment.
NAV_SPLIT = """### Two-call chain with a hop between

Mission: do two data actions with a navigation in the middle.

Inputs:
- record_id: the record id.

Instructions:
1. Submit the form to create a draft.
2. Navigate to the draft's detail page.
3. Export the draft as a file.

Return value:
- file_path: the path, or NAN.
"""

# Two separate data-work runs split by a COMPREHEND boundary -> TWO segments (s0, s1).
TWO_SEGMENTS = """### Two independent segments

Mission: two unrelated data actions separated by a human check.

Inputs:
- record_id: the record id.

Instructions:
1. Generate the first report.
2. Review the first report and decide if a second is needed.
3. Generate the second report.

Return value:
- ok: yes or no.
"""


def _seg_ids(md):
    return p.partition("steps/x.md", md, None)["segment_ids"]


def _regions(md):
    return p.partition("steps/x.md", md, None)["regions"]


# ---- nature classification (verb prior) ----
def test_nature_login_is_fuzzy():
    assert p.classify_nature("Open Chrome and log in.") == "FUZZY"

def test_nature_apply_is_data_work():
    assert p.classify_nature("Apply the chosen template to the record.") == "DATA_WORK"

def test_nature_export_is_data_work():
    assert p.classify_nature("Export the result as a file.") == "DATA_WORK"

def test_nature_navigate_is_navigate():
    assert p.classify_nature("Navigate to the record list.") == "NAVIGATE"

def test_nature_review_is_comprehend():
    assert p.classify_nature("Review the downloaded file looks right.") == "COMPREHEND"

def test_nature_fuzzy_beats_navigate():
    # "click" alone is movement, but a login is irreproducible setup -> FUZZY must win.
    assert p.classify_nature("Click sign in and enter your password.") == "FUZZY"


# ---- provisional flag (nature is a prior, confirmed against capture later) ----
def test_nature_marked_provisional():
    out = p.partition("steps/x.md", MIXED, None)
    assert out["nature_provisional"] is True

def test_grounded_against_recorded_but_not_read():
    out = p.partition("steps/x.md", MIXED, ".o11y/run")
    assert out["grounded_against"] == ".o11y/run"


# ---- segment coalescing + stable ids ----
def test_mixed_yields_one_segment():
    assert _seg_ids(MIXED) == ["s0"]

def test_segment_holds_maximal_data_work_run():
    seg = next(r for r in _regions(MIXED) if r["kind"] == "ApiSegment")
    texts = [a["text"] for a in seg["actions"]]
    assert any("Apply" in t for t in texts) and any("export" in t for t in texts)
    assert all(a["nature"] == "DATA_WORK" for a in seg["actions"])

def test_two_segments_get_sequential_ids():
    assert _seg_ids(TWO_SEGMENTS) == ["s0", "s1"]

def test_no_data_work_action_outside_a_segment():
    # CONTRACT: no DATA_WORK action may sit in a UiRegion.
    for r in _regions(MIXED):
        if r["kind"] == "UiRegion":
            assert all(a["nature"] != "DATA_WORK" for a in r["actions"])


# ---- pure-NAVIGATE absorption ----
def test_navigate_between_data_work_is_absorbed():
    # the middle "Navigate" must fold into the single segment, not split it into two.
    assert _seg_ids(NAV_SPLIT) == ["s0"]
    seg = next(r for r in _regions(NAV_SPLIT) if r["kind"] == "ApiSegment")
    assert len(seg["actions"]) == 3
    assert all(a["nature"] == "DATA_WORK" for a in seg["actions"])

def test_navigate_bounded_by_non_data_work_stays_ui():
    # ALL_UI's "Navigate to the record" sits between NAVIGATE (open) and COMPREHEND (read) — no
    # adjacent DATA_WORK, so it must NOT be absorbed; it stays a UI hop.
    regions = _regions(ALL_UI)
    assert all(r["kind"] == "UiRegion" for r in regions)
    nav = [a for r in regions for a in r["actions"] if a["nature"] == "NAVIGATE"]
    assert any("Navigate" in a["text"] for a in nav)

def test_adjacent_navigate_is_absorbed_not_a_ui_region():
    # MIXED's "Navigate to the record list" touches the DATA_WORK run -> absorbed into the segment,
    # leaving the leading UiRegion as the FUZZY login only.
    regions = _regions(MIXED)
    assert regions[0]["kind"] == "UiRegion"
    assert all(a["nature"] == "FUZZY" for a in regions[0]["actions"])
    seg = next(r for r in regions if r["kind"] == "ApiSegment")
    assert any("Navigate" in a["text"] for a in seg["actions"])


# ---- 0-segment workflow -> KEEP UI ----
def test_all_ui_yields_zero_segments():
    assert _seg_ids(ALL_UI) == []

def test_all_ui_has_only_ui_regions():
    assert all(r["kind"] == "UiRegion" for r in _regions(ALL_UI))


# ---- typed handoff graph ----
def test_step_inputs_become_step_input_handoffs():
    out = p.partition("steps/x.md", MIXED, None)
    origins = {h["ref"]: h["origin"] for h in out["handoffs"]}
    assert origins.get("r_input_record_id") == "STEP_INPUT"
    assert origins.get("r_input_account_id") == "STEP_INPUT"

def test_step_input_enters_at_from_null_and_targets_first_segment():
    out = p.partition("steps/x.md", MIXED, None)
    h = next(h for h in out["handoffs"] if h["ref"] == "r_input_record_id")
    assert h["from"] is None and h["to"] == "s0"

def test_step_input_targets_null_when_all_ui():
    out = p.partition("steps/x.md", ALL_UI, None)
    h = next(h for h in out["handoffs"] if h["ref"] == "r_input_record_id")
    assert h["to"] is None  # no segment to consume it

def test_segment_consumes_lists_step_input():
    seg = next(r for r in _regions(MIXED) if r["kind"] == "ApiSegment")
    refs = {c["ref"] for c in seg["consumes"]}
    assert "r_input_record_id" in refs and "r_input_account_id" in refs


# ---- grounded per-action produces/consumes -> PRIOR_SEGMENT handoff ----
def test_prior_segment_handoff_from_grounded_io():
    # simulate the grounded case: action 3 (i=2) produces r1, action 4 (i=3) consumes r1 (same segment).
    io = {2: {"produces": ["r1"], "consumes": []}, 3: {"produces": ["r2"], "consumes": ["r1"]}}
    out = p.partition("steps/x.md", MIXED, ".o11y/run", handoffs_by_action=io)
    h = next(h for h in out["handoffs"] if h["ref"] == "r1")
    assert h["origin"] == "PRIOR_SEGMENT" and h["from"] == "s0" and h["to"] == "s0"
    assert h["shape"].get("entropy") == "high"

def test_terminal_produce_leaves_at_to_null():
    io = {2: {"produces": ["r1"], "consumes": []}, 3: {"produces": ["r2"], "consumes": ["r1"]}}
    out = p.partition("steps/x.md", MIXED, ".o11y/run", handoffs_by_action=io)
    h = next(h for h in out["handoffs"] if h["ref"] == "r2")
    assert h["to"] is None  # final output, nothing consumes it


# ---- output envelope ----
def test_envelope_schema_and_bail():
    out = p.partition("steps/x.md", MIXED, None)
    assert out["schema"] == "segments/v1"
    assert out["step"] == "steps/x.md"
    assert out["bail"] is None

def test_action_indices_strictly_increasing():
    out = p.partition("steps/x.md", MIXED, None)
    seen = -1
    for r in out["regions"]:
        for a in r["actions"]:
            assert a["i"] == seen + 1, f"i jumped: {a['i']} after {seen}"
            seen = a["i"]


# ---- parse guards ----
def test_missing_instructions_raises():
    try:
        p.partition("steps/x.md", "Mission: x\n\nReturn value:\n- y", None)
    except ValueError as e:
        assert "Instructions" in str(e)
    else:
        raise AssertionError("expected ValueError for missing Instructions")

def test_empty_instructions_raises():
    try:
        p.partition("steps/x.md", "Instructions:\n\nReturn value:\n- y", None)
    except ValueError as e:
        assert "numbered steps" in str(e)
    else:
        raise AssertionError("expected ValueError for empty Instructions")

def test_return_value_block_not_parsed_as_actions():
    # numbered-looking lines AFTER `Return value:` must not be slurped as workflow actions.
    md = "Inputs:\n- a: x\n\nInstructions:\n1. Apply the thing.\n\nReturn value:\n1. nope not an action\n"
    out = p.partition("steps/x.md", md, None)
    total = sum(len(r["actions"]) for r in out["regions"])
    assert total == 1, f"slurped extra actions: {total}"


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
