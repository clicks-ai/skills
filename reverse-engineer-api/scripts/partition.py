#!/usr/bin/env python3
# partition.py — S0 PARTITION (once, before capture). Reads a mission-style step file (steps/<STEP>.md),
# treats its numbered `Instructions:` list as the workflow W, and partitions W into ordered Regions:
# UiRegion (FUZZY/NAVIGATE/COMPREHEND) | ApiSegment (a maximal contiguous run of DATA_WORK). It mints
# stable segment ids, ABSORBS pure NAVIGATE into the adjacent segment so a hop never splits a causal
# chain, and emits the typed handoff graph. Output: segments.json per CONTRACTS §1.
#
# The nature assigned here is a VERB-PRIOR — a PROVISIONAL guess from the action text. It is confirmed
# against the capture's wire later (an action is DATA_WORK if a mutation actually fired; it needs a POLL
# if a status read repeated). `grounded_against` is recorded but partition does NOT read the wire — the
# prior is the seed, never the decision. The `provisional` flag on every action's nature says so.
#
# A workflow may yield 0 segments (all UI) -> `segment_ids == [] ⇒ KEEP UI, done`.
#
# Usage:
#   python partition.py --step steps/<STEP>.md [--grounded-against .o11y/run] [--out segments.json]

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

Nature = str  # FUZZY | NAVIGATE | DATA_WORK | COMPREHEND
JsonObj = dict[str, Any]  # a heterogeneous JSON-shaped record (action / region / handoff)

# ---- Verb-prior keyword tables ----
# These are a PRIOR, never the decision (DESIGN GEN-6): trace grounding confirms DATA_WORK / POLL later.
# Order of precedence below resolves an action matching several tables: DATA_WORK (a mutation/act) wins
# over NAVIGATE, COMPREHEND wins over NAVIGATE, FUZZY (irreproducible setup) is the floor.

# Data-work = state mutation or the produce-an-artifact act (the unit we API-ify).
_DATA_WORK = (
    "apply", "applies", "submit", "submits", "create", "creates", "generate", "generates",
    "export", "exports", "download", "downloads", "upload", "uploads", "send", "sends",
    "delete", "deletes",
    "remove", "removes", "save", "saves", "update", "updates", "edit", "edits",
    "post", "posts", "publish", "publishes", "import", "imports", "process", "processes",
    "convert", "converts", "render", "renders", "build", "builds", "request", "requests",
    "fetch", "fetches", "pull", "pulls", "sync", "approve", "approves", "confirm", "confirms",
    "compute", "calculate", "set", "sets", "add", "adds", "attach", "attaches", "issue",
    "search", "searches", "query", "queries", "filter", "filters", "fill", "fills",
    "enter", "enters", "type", "types", "select", "selects", "choose", "pick", "picks",
    "run", "runs",
)
# Pure movement — no mutation, no irreproducible effect. Absorbed into an adjacent segment.
_NAVIGATE = (
    "navigate", "navigates", "visit", "visits", "browse", "scroll", "scrolls",
    "go to", "open the", "open chrome", "open a", "open up", "click on the",
    "switch to", "view the", "land on", "return to", "head to",
)
# Reading/understanding for the human — a region boundary, never API-ified.
_COMPREHEND = (
    "read", "reads", "review", "reviews", "inspect", "inspects", "verify", "verifies",
    "understand", "observe", "observes", "compare", "compares", "interpret",
    "assess", "assesses", "examine", "examines", "note", "notes",
    "check that", "confirm that", "summari",  # summari{se,ze}
)
# Irreproducible / ambient setup (login, credentials, manual judgement) — the FUZZY floor.
_FUZZY = (
    "login", "authenticate", "credential", "credentials", "password",
    "manually", "decide", "decides", "judge", "judges",
    "log in", "sign in", "by hand", "if needed", "as appropriate",
)


def _matches(text: str, table: tuple[str, ...]) -> bool:
    # Single-token verbs match on WORD BOUNDARIES (so "download" never fires on "downloaded"); multi-word
    # phrases match as substrings. Keeps the prior precise without an inflection explosion.
    low = text.lower()
    words = set(re.findall(r"[a-z]+", low))
    for kw in table:
        if " " in kw:
            if kw in low:
                return True
        elif kw in words:
            return True
    return False


def classify_nature(text: str) -> Nature:
    # Precedence: FUZZY (irreproducible) > DATA_WORK (mutation/act) > COMPREHEND (read) > NAVIGATE (move).
    # FUZZY wins outright because an irreproducible setup must not be folded into an API segment even if
    # it also says "click"; everything else falls through to NAVIGATE as the benign default.
    if _matches(text, _FUZZY):
        return "FUZZY"
    if _matches(text, _DATA_WORK):
        return "DATA_WORK"
    if _matches(text, _COMPREHEND):
        return "COMPREHEND"
    if _matches(text, _NAVIGATE):
        return "NAVIGATE"
    return "NAVIGATE"


# ---- Step-file parsing: the numbered Instructions list IS the workflow W ----

_NUM_LINE = re.compile(r"^\s*(\d+)[.)]\s+(.*\S)\s*$")


def parse_workflow(step_md: str) -> list[JsonObj]:
    if "Instructions:" not in step_md:
        raise ValueError("not a mission-style UI step (no 'Instructions:' heading)")
    after = step_md.split("Instructions:", 1)[1]
    # Stop at the next mission section so we never absorb Return value / Important bullets as actions.
    for stop in ("\nReturn value:", "\nImportant:", "\n## "):
        idx = after.find(stop)
        if idx != -1:
            after = after[:idx]

    actions: list[JsonObj] = []
    for line in after.splitlines():
        m = _NUM_LINE.match(line)
        if not m:
            continue
        text = m.group(2).strip()
        actions.append({"i": len(actions), "text": text, "nature": classify_nature(text)})
    if not actions:
        raise ValueError("Instructions block has no numbered steps")
    return actions


# ---- Handoff inference: every value crossing a region boundary as a typed HandoffSpec ----

_INPUTS_BULLET = re.compile(r"^\s*-\s+([A-Za-z_][\w]*)\s*:", re.MULTILINE)


def parse_inputs(step_md: str) -> list[str]:
    if "Inputs:" not in step_md:
        return []
    block = step_md.split("Inputs:", 1)[1]
    for stop in ("\nInstructions:", "\nReturn value:", "\nImportant:", "\n## "):
        idx = block.find(stop)
        if idx != -1:
            block = block[:idx]
    return _INPUTS_BULLET.findall(block)


# ---- Region assembly ----


def _absorb_navigate(natures: list[Nature]) -> list[Nature]:
    # A pure NAVIGATE adjacent to an ApiSegment is folded into DATA_WORK so it never splits a causal chain
    # (DESIGN CON-4a). "Adjacent" = a NAVIGATE that touches a DATA_WORK run on either side. A NAVIGATE
    # bounded only by FUZZY/COMPREHEND stays a UI hop (nothing to absorb into).
    out = list(natures)
    n = len(out)
    changed = True
    while changed:
        changed = False
        for i, nat in enumerate(out):
            if nat != "NAVIGATE":
                continue
            left = out[i - 1] if i > 0 else None
            right = out[i + 1] if i + 1 < n else None
            if left == "DATA_WORK" or right == "DATA_WORK":
                out[i] = "DATA_WORK"
                changed = True
    return out


def build_regions(
    actions: list[JsonObj],
    handoffs_by_action: dict[int, dict[str, list[str]]],
) -> tuple[list[JsonObj], list[str]]:
    natures = _absorb_navigate([str(a["nature"]) for a in actions])

    regions: list[JsonObj] = []
    segment_ids: list[str] = []
    seg_seq = 0
    ui_seq = 0
    i = 0
    n = len(actions)

    while i < n:
        if natures[i] == "DATA_WORK":
            j = i
            seg_actions: list[JsonObj] = []
            while j < n and natures[j] == "DATA_WORK":
                a = actions[j]
                io = handoffs_by_action.get(int(a["i"]), {"produces": [], "consumes": []})
                seg_actions.append({
                    "i": a["i"], "text": a["text"], "nature": "DATA_WORK",
                    "produces": list(io["produces"]), "consumes": list(io["consumes"]),
                })
                j += 1
            sid = f"s{seg_seq}"
            seg_seq += 1
            segment_ids.append(sid)
            regions.append({"kind": "ApiSegment", "id": sid, "actions": seg_actions,
                            "consumes": [], "produces": []})
            i = j
        else:
            # Coalesce a contiguous run of non-DATA_WORK into one UiRegion; its kind-nature is the first
            # action's (FUZZY/NAVIGATE/COMPREHEND), which is enough for a boundary marker.
            j = i
            ui_actions: list[JsonObj] = []
            while j < n and natures[j] != "DATA_WORK":
                a = actions[j]
                ui_actions.append({"i": a["i"], "text": a["text"], "nature": natures[j]})
                j += 1
            regions.append({"kind": "UiRegion", "id": f"u{ui_seq}",
                            "nature": natures[i], "actions": ui_actions, "produces": []})
            ui_seq += 1
            i = j

    return regions, segment_ids


def _first_segment_id(regions: list[JsonObj]) -> str | None:
    for region in regions:
        if region["kind"] == "ApiSegment":
            return str(region["id"])
    return None


def build_handoffs(
    regions: list[JsonObj],
    step_inputs: list[str],
) -> list[JsonObj]:
    # Mint one HandoffSpec per ValueRef crossing a region boundary. Two sources of refs:
    #   1. STEP_INPUT — each `Inputs:` bullet enters the workflow at from:null and is consumed by the
    #      first ApiSegment (the natural data-work consumer); to:null when there is no segment.
    #   2. per-action produces/consumes (present only when grounded) — a ref produced by region X and
    #      consumed downstream gets origin PRIOR_SEGMENT (X is an ApiSegment) or PRIOR_UI (X is a UiRegion);
    #      a terminal produce with no consumer leaves at to:null.
    producer_of: dict[str, str] = {}  # ref -> region id that produces it
    producer_kind: dict[str, str] = {}
    consumer_of: dict[str, str] = {}  # ref -> first region id that consumes it
    for region in regions:
        rid = str(region["id"])
        for a in region.get("actions", []):
            if not isinstance(a, dict):
                continue
            for ref in a.get("produces", []):
                producer_of.setdefault(ref, rid)
                producer_kind.setdefault(ref, str(region["kind"]))
            for ref in a.get("consumes", []):
                consumer_of.setdefault(ref, rid)

    handoffs: list[JsonObj] = []
    seen: set[str] = set()
    first_seg = _first_segment_id(regions)

    def emit(ref: str, origin: str, frm: str | None, to: str | None, entropy: str | None) -> None:
        if ref in seen:
            return
        seen.add(ref)
        shape: JsonObj = {"type": "string"}
        if entropy:
            shape["entropy"] = entropy
        handoffs.append({
            "ref": ref, "shape": shape, "extractor": "json-ptr:/" + ref,
            "origin": origin, "from": frm, "to": to,
        })

    # 1. STEP_INPUT bullets -> consumed by the first segment (or to:null if the workflow is all-UI).
    for name in step_inputs:
        ref = f"r_input_{name}"
        emit(ref, "STEP_INPUT", None, consumer_of.get(ref, first_seg), entropy=None)

    # 2. grounded produces/consumes edges.
    all_refs = set(producer_of) | set(consumer_of)
    for ref in sorted(all_refs):
        if ref in producer_of:
            kind = producer_kind[ref]
            origin = "PRIOR_SEGMENT" if kind == "ApiSegment" else "PRIOR_UI"
            emit(ref, origin, producer_of[ref], consumer_of.get(ref), entropy="high")
        else:
            # consumed but never produced in-workflow -> a step input the action referenced directly.
            emit(ref, "STEP_INPUT", None, consumer_of[ref], entropy=None)

    return handoffs


def attach_region_handoffs(
    regions: list[JsonObj],
    handoffs: list[JsonObj],
) -> None:
    # Populate each segment's consumes/produces from the global handoff graph so a reader of one region
    # has its boundary contract inline (DESIGN §2.1 Region.consumes/produces). A consume is any ref a
    # segment action consumes OR any handoff whose `to` is this segment (STEP_INPUT routed here); a
    # produce is any ref a segment action produces.
    spec_by_ref = {str(h["ref"]): h for h in handoffs}
    for region in regions:
        if region["kind"] != "ApiSegment":
            continue
        rid = str(region["id"])
        consumes: list[JsonObj] = []
        produces: list[JsonObj] = []

        def add(into: list[JsonObj], ref: str) -> None:
            spec = spec_by_ref.get(ref)
            if spec and not any(x["ref"] == ref for x in into):
                into.append(_region_spec(spec))

        for h in handoffs:
            if h["to"] == rid:
                add(consumes, str(h["ref"]))
        for a in region["actions"]:
            for ref in a.get("consumes", []):
                add(consumes, ref)
            for ref in a.get("produces", []):
                add(produces, ref)
        region["consumes"] = consumes
        region["produces"] = produces


def _region_spec(h: JsonObj) -> JsonObj:
    return {"ref": h["ref"], "shape": h["shape"], "extractor": h["extractor"], "origin": h["origin"]}


def partition(
    step_path: str,
    step_md: str,
    grounded_against: str | None,
    handoffs_by_action: dict[int, dict[str, list[str]]] | None = None,
) -> JsonObj:
    actions = parse_workflow(step_md)
    step_inputs = parse_inputs(step_md)
    handoffs_by_action = handoffs_by_action or {}

    regions, segment_ids = build_regions(actions, handoffs_by_action)
    handoffs = build_handoffs(regions, step_inputs)
    attach_region_handoffs(regions, handoffs)

    return {
        "schema": "segments/v1",
        "step": step_path,
        "grounded_against": grounded_against,
        # The nature here is a PROVISIONAL verb-prior, confirmed against capture later (DESIGN GEN-6).
        "nature_provisional": True,
        "regions": regions,
        "handoffs": handoffs,
        "segment_ids": segment_ids,
        "bail": None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="partition")
    ap.add_argument("--step", required=True, help="path to the mission-style steps/<STEP>.md")
    ap.add_argument("--grounded-against", default=None,
                    help="a capture run dir to record as the grounding source (nature confirmed later)")
    ap.add_argument("--out", default=None, help="write segments.json here; omit to print to stdout")
    args = ap.parse_args(argv)

    try:
        with open(args.step) as f:
            step_md = f.read()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = partition(args.step, step_md, args.grounded_against)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = json.dumps(result, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(out + "\n")
        n = len(result["segment_ids"])
        print(f"wrote {args.out}: {n} segment(s)" + ("  ⇒ KEEP UI (no data-work)" if n == 0 else ""))
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
