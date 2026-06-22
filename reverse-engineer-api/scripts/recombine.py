#!/usr/bin/env python3
# recombine.py — S7 RECOMBINE / Executor (DESIGN §3.1, CONTRACTS §1/§3). The missing piece that threads
# the per-segment decisions back into ONE runnable workflow Plan.
#
# Input: the partition graph (segments.json, CONTRACTS §1) + the per-segment verdicts (each plan.json,
# CONTRACTS §3). Output: an ordered, executable Plan — one entry per Region in workflow order, each
# carrying the typed handoffs it consumes/produces and the runner it dispatches to (UiRegion -> agent,
# ApiSegment -> its ReplayProgram). Recombine is PURE DATA ORCHESTRATION: it builds and validates the
# Plan and (when handed region runners) executes it, but issues NO live HTTP itself — the runners do.
#
# The three jobs DESIGN §3.1 pins on this stage, each made mechanical here:
#   1. ORDER       — regions in workflow order; each region's `consumes` are supplied from a shared scope.
#   2. SHAPE-GATE  — every value a region `produces` is validated against its HandoffSpec.shape (FAIL FAST:
#                    a shape mismatch stops the run; a silently-wrong handoff is the seam where the old
#                    pipeline drifted).
#   3. INV-1 @ WORKFLOW LEVEL — a value un-sourced WITHIN a segment but declared `produces` by an UPSTREAM
#                    region is INPUT, not UNEXPLAINED (resolves CON-1,2,4,5 / FN-4). This is the re-check
#                    the per-segment G1 cannot do on its own, because a segment only sees its own trace.
#
# The run-scope is keyed by ValueRef.id (`r0`,`r1`,…) — the one id space CONTRACTS §6.1 guarantees flows
# segments.json.handoffs -> plan source.ref/step ref -> here.
#
# Usage:
#   python recombine.py --segments segments.json --plans plan.s0.json plan.s1.json [--out recombine.json]
#       (--plans optional: an ApiSegment with no plan is treated as KEEP-UI for that segment)

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# A region runner takes the values the region consumes (keyed by ref) and returns the values it produces
# (keyed by ref). Live-call-free in tests; in production a UiRegion runner drives the agent and an
# ApiSegment runner executes the ReplayProgram. recombine never calls the wire itself.
RegionRunner = Callable[["RegionPlan", dict[str, Any]], dict[str, Any]]


# ---- typed Plan structures (built from the frozen JSON; immutable once recombined) -----------------
@dataclass(slots=True, frozen=True)
class Handoff:
    ref: str
    shape: dict[str, Any]
    extractor: str
    origin: str  # STEP_INPUT | PRIOR_UI | PRIOR_SEGMENT
    frm: str | None
    to: str | None


@dataclass(slots=True, frozen=True)
class RegionPlan:
    kind: str  # UiRegion | ApiSegment
    id: str
    consumes: tuple[Handoff, ...]
    produces: tuple[Handoff, ...]
    verdict: str  # API | UI — how this region runs (ApiSegment with a passing plan -> API, else UI)
    plan: dict[str, Any] | None  # the segment's plan.json when verdict == "API", else None


@dataclass(slots=True)
class Plan:
    schema: str
    step: str | None
    regions: list[RegionPlan] = field(default_factory=list)
    workflow_inputs: list[str] = field(default_factory=list)  # refs entering at the workflow edge
    workflow_outputs: list[str] = field(default_factory=list)  # refs leaving at the workflow edge
    inv1: dict[str, Any] = field(default_factory=dict)  # workflow-level INV-1 witness
    bail: dict[str, Any] | None = None


# ---- shape validation (the fail-fast gate) ---------------------------------------------------------
# A produced value must satisfy what its consumer is allowed to assume (HandoffSpec.shape). We check only
# what the shape DECLARES — unspecified facets are free (permissive consumers, CONTRACTS §6.4) — but a
# declared facet that the concrete value violates is a hard stop.
def shape_ok(value: Any, shape: dict[str, Any]) -> tuple[bool, str]:
    declared = shape.get("type")
    if declared is None:
        return True, ""  # nothing declared -> nothing to violate
    actual = _runtime_type(value)
    # "binary" is our tag for opaque bytes / whole-payload artifacts (PDF/zip/image/stream); a runner may
    # hand them back as bytes OR as a path/str sidecar — both satisfy a binary shape.
    if declared == "binary":
        if actual not in ("binary", "string"):
            return False, f"expected binary, got {actual}"
    elif actual != declared:
        return False, f"expected {declared}, got {actual}"
    tag = shape.get("tag")
    if tag is not None and isinstance(value, dict) and value.get("tag") not in (None, tag):
        return False, f"tag mismatch: expected {tag}, got {value.get('tag')}"
    return True, ""


def _runtime_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, bytes | bytearray):
        return "binary"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return "unknown"


# ---- build the ordered Plan from the frozen JSON ---------------------------------------------------
def _handoffs(specs: list[dict[str, Any]]) -> tuple[Handoff, ...]:
    return tuple(
        Handoff(
            ref=s["ref"],
            shape=s.get("shape") or {},
            extractor=s.get("extractor", ""),
            origin=s.get("origin", "STEP_INPUT"),
            frm=s.get("from"),
            to=s.get("to"),
        )
        for s in specs
    )


def _region_verdict(kind: str, plan: dict[str, Any] | None) -> str:
    # A UiRegion always runs as UI. An ApiSegment runs as API only when it has a plan that the gates
    # passed (verdict API-CANDIDATE, no bail); a missing/KEEP-UI plan keeps the UI for that segment.
    if kind != "ApiSegment":
        return "UI"
    if plan is None:
        return "UI"
    if plan.get("bail"):
        return "UI"
    return "API" if plan.get("verdict") == "API-CANDIDATE" else "UI"


def build_plan(segments: dict[str, Any], plans_by_segment: dict[str, dict[str, Any]]) -> Plan:
    plan = Plan(schema="recombine/v1", step=segments.get("step"))
    if segments.get("bail"):
        plan.bail = segments["bail"]

    for region in segments.get("regions", []):
        kind = region["kind"]
        seg_plan = plans_by_segment.get(region["id"]) if kind == "ApiSegment" else None
        plan.regions.append(
            RegionPlan(
                kind=kind,
                id=region["id"],
                consumes=_handoffs(region.get("consumes", [])),
                produces=_handoffs(region.get("produces", [])),
                verdict=_region_verdict(kind, seg_plan),
                plan=seg_plan,
            )
        )

    # Workflow-edge refs come from the handoff graph: from==null enters, to==null leaves.
    for h in segments.get("handoffs", []):
        if h.get("from") is None:
            plan.workflow_inputs.append(h["ref"])
        if h.get("to") is None:
            plan.workflow_outputs.append(h["ref"])
    return plan


# ---- workflow-level INV-1 (the re-check no per-segment gate can do) ---------------------------------
# A region consumes refs; each must be sourced by the time the region runs. Sources, in priority:
#   - STEP_INPUT      -> supplied at the workflow edge (an input, by definition)
#   - an UPSTREAM region's `produces` -> declared upstream => INPUT to this region, NOT unexplained
#   - already in scope (an earlier region produced it)
# A consume whose ref is NONE of these, and is not a workflow input, is genuinely UNEXPLAINED => FAIL.
def check_inv1(plan: Plan) -> dict[str, Any]:
    produced_upstream: set[str] = set()
    workflow_inputs = set(plan.workflow_inputs)
    unexplained: list[dict[str, str]] = []
    reclassified: list[dict[str, str]] = []  # un-sourced-in-segment-but-upstream-declared => INPUT

    for region in plan.regions:
        for h in region.consumes:
            if h.origin == "STEP_INPUT" or h.ref in workflow_inputs:
                continue
            if h.ref in produced_upstream:
                # Declared by an upstream region -> this is an INPUT to the region, never UNEXPLAINED.
                reclassified.append({"region": region.id, "ref": h.ref, "as": "INPUT"})
                continue
            unexplained.append(
                {"region": region.id, "ref": h.ref, "reason": f"consumed ref {h.ref} sourced by no upstream region nor workflow input"}
            )
        for h in region.produces:
            produced_upstream.add(h.ref)

    return {
        "pass": not unexplained,
        "unexplained": unexplained,
        "reclassified_as_input": reclassified,
        "reason": "every consumed ref is sourced upstream or at the workflow edge"
        if not unexplained
        else f"{len(unexplained)} consumed ref(s) un-sourced at the workflow level",
    }


# ---- the executor (pure orchestration; live calls live ONLY in the supplied runners) ----------------
class ShapeViolation(Exception):
    pass


class UnboundConsume(Exception):
    pass


def execute(plan: Plan, runners: dict[str, RegionRunner]) -> dict[str, Any]:
    # runners: kind ("UiRegion"/"ApiSegment") -> a callable that runs one region. The shared run-scope is
    # keyed by ValueRef.id and threaded forward; the next region's `consumes` are served from it.
    inv1 = check_inv1(plan)
    plan.inv1 = inv1
    if not inv1["pass"]:
        raise UnboundConsume(inv1["reason"])

    scope: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []

    for region in plan.regions:
        supplied = _gather_consumes(region, scope, plan)
        runner = runners.get(region.kind)
        if runner is None:
            raise KeyError(f"no runner registered for region kind {region.kind!r}")
        produced = runner(region, supplied) or {}

        for h in region.produces:
            if h.ref not in produced:
                raise ShapeViolation(f"region {region.id} did not produce declared ref {h.ref}")
            ok, why = shape_ok(produced[h.ref], h.shape)
            if not ok:
                # FAIL FAST — a wrong-shape handoff is the drift seam; never let it propagate downstream.
                raise ShapeViolation(f"region {region.id} produced ref {h.ref} of wrong shape: {why}")
            scope[h.ref] = produced[h.ref]

        trace.append({"region": region.id, "kind": region.kind, "verdict": region.verdict, "produced": [h.ref for h in region.produces]})

    return {
        "outputs": {ref: scope[ref] for ref in plan.workflow_outputs if ref in scope},
        "scope": scope,
        "trace": trace,
        "inv1": inv1,
    }


def _gather_consumes(region: RegionPlan, scope: dict[str, Any], plan: Plan) -> dict[str, Any]:
    supplied: dict[str, Any] = {}
    workflow_inputs = set(plan.workflow_inputs)
    for h in region.consumes:
        if h.ref in scope:
            supplied[h.ref] = scope[h.ref]
        elif h.origin == "STEP_INPUT" or h.ref in workflow_inputs:
            supplied[h.ref] = None  # an external input the caller binds into scope before run, else None
        else:
            raise UnboundConsume(f"region {region.id} consumes unbound ref {h.ref}")
    return supplied


# ---- serialization ---------------------------------------------------------------------------------
def plan_to_json(plan: Plan) -> dict[str, Any]:
    return {
        "schema": plan.schema,
        "step": plan.step,
        "regions": [
            {
                "kind": r.kind,
                "id": r.id,
                "verdict": r.verdict,
                "consumes": [_handoff_json(h) for h in r.consumes],
                "produces": [_handoff_json(h) for h in r.produces],
            }
            for r in plan.regions
        ],
        "workflow_inputs": plan.workflow_inputs,
        "workflow_outputs": plan.workflow_outputs,
        "inv1": plan.inv1 or check_inv1(plan),
        "bail": plan.bail,
    }


def _handoff_json(h: Handoff) -> dict[str, Any]:
    return {"ref": h.ref, "shape": h.shape, "extractor": h.extractor, "origin": h.origin, "from": h.frm, "to": h.to}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="recombine")
    ap.add_argument("--segments", required=True, help="segments.json (CONTRACTS §1)")
    ap.add_argument("--plans", nargs="*", default=[], help="per-segment plan.json files (CONTRACTS §3)")
    ap.add_argument("--out", default=None, help="write the recombined Plan here (default stdout)")
    args = ap.parse_args(argv)

    with open(args.segments) as f:
        segments = json.load(f)
    plans_by_segment: dict[str, dict[str, Any]] = {}
    for p in args.plans:
        with open(p) as f:
            pj = json.load(f)
        plans_by_segment[pj["segment_id"]] = pj

    plan = build_plan(segments, plans_by_segment)
    inv1 = check_inv1(plan)
    plan.inv1 = inv1
    out = plan_to_json(plan)

    payload = json.dumps(out, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(payload)
        print(f"wrote {args.out}")
    else:
        print(payload)
    # A workflow-level UNEXPLAINED is a real miss -> nonzero so the operator sees KEEP-UI, not a green run.
    return 0 if inv1["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
