#!/usr/bin/env python3
# prove_runner.py — S6/G3 PROVEN gate. The empirical backstop.
#
# "A file was produced" is NOT proof. "The API output equals the UI golden, on instances we did NOT build
# on, that are mutually isolated, that span the declared input boundaries, that force pagination, and that
# perturb every COMPUTED value" is. This owns the N>=2 run loop: for each instance it runs the API command
# and the UI golden, calls the FROZEN comparator (verify_equivalence.py emits the per-comparison block), and
# requires MATCH on every instance x run, plus the G3.3/G3.4/G3.5 coverage flags. It writes
# verify_receipt.json per CONTRACTS §4 and decides PROVEN | FAILED | UNCOVERED — anything but PROVEN keeps UI.
#
# Usage:
#   prove_runner.py --command command.sh --plan plan.json --comparator comparator.json \
#                   --instances instances.json --runs 2 --out verify_receipt.json
#
# The actual run is injectable (a Runner): real instances arrive at teach time; tests inject a fake so the
# gate logic is unit-testable without a browser.

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# Reuse the comparator's own json helpers (do NOT reimplement — divergence is a bug class).
from verify_equivalence import _strip_ptr, canonical, json_pointer, load_json

SCHEMA = "verify_receipt/v1"
PROVEN, FAILED, UNCOVERED = "PROVEN", "FAILED", "UNCOVERED"

# Binary containers whose bytes are nondeterministic — a projection comparator is REQUIRED (no BYTE_EQ).
NONDETERMINISTIC_TAGS = frozenset({"pdf", "png", "jpg", "jpeg", "gif", "zip", "image", "archive"})


@dataclass(slots=True, frozen=True)
class Instance:
    id: str
    role: str  # fresh | isolated | boundary
    boundary: str  # nominal | min | max | empty | large-paginating | <category>
    tenant: str | None = None
    isolated_from: list[str] = field(default_factory=list)
    forces_pagination: bool = False
    perturbs_computed: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class Comparator:
    kind: str  # BYTE_EQ | CANONICAL_JSON_EQ | NORMALIZED | EXTRACTED | ASSEMBLED
    frozen: bool
    tag: str | None = None
    field_mask: list[str] = field(default_factory=list)
    projection: str | None = None
    threshold: float = 0.9


class Runner(Protocol):
    # Run the API command (command.sh) on `instance`, run number `run` -> path to the produced artifact, or
    # None if the command produced no output (a hard FAIL for that run).
    def run_api(self, command: str, instance: Instance, run: int) -> str | None: ...

    # Produce the UI golden on the SAME instance -> path to the golden artifact (the ground truth).
    def run_golden(self, instance: Instance, run: int) -> str | None: ...


class SubprocessRunner:
    # Real teach-time runner. `run_api` shells the command; `run_golden` is operator-supplied per instance
    # (the UI export is inherently manual / browser-driven, so it is provided as a path map keyed by id).
    def __init__(self, golden_paths: dict[str, list[str]]) -> None:
        self._golden = golden_paths

    def run_api(self, command: str, instance: Instance, run: int) -> str | None:
        out = f"/tmp/api_out.{instance.id}.{run}"
        # Inherit the real environment (PATH/HOME/auth) and ADD the PROVE_* vars — replacing it would
        # wipe PATH and make node/run-in-page/curl 'command not found' in the replay chain.
        proc = subprocess.run(
            ["bash", command],
            capture_output=True,
            text=True,
            env={**os.environ, "PROVE_INSTANCE": instance.id, "PROVE_RUN": str(run), "PROVE_OUT": out},
        )
        if proc.returncode != 0:
            return None
        return out if Path(out).exists() else None

    def run_golden(self, instance: Instance, run: int) -> str | None:
        paths = self._golden.get(instance.id, [])
        return paths[run - 1] if run - 1 < len(paths) else None


def _verify_equivalence(api: str, golden: str, comparator: Comparator) -> dict[str, Any]:
    # Invoke the FROZEN comparator (verify_equivalence.py); capture its §4.1 block verbatim.
    here = Path(__file__).resolve().parent
    # Forward the FROZEN comparator — kind/field_mask/projection — or verify_equivalence falls back to the
    # legacy byte/text path and a NORMALIZED/EXTRACTED proof would MISMATCH every run.
    cmd = [sys.executable, str(here / "verify_equivalence.py"),
           "--api", api, "--golden", golden, "--threshold", str(comparator.threshold),
           "--comparator", comparator.kind]
    for m in comparator.field_mask:
        cmd += ["--mask", m]
    if comparator.projection is not None:
        cmd += ["--projection", comparator.projection]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        block: dict[str, Any] = json.loads(proc.stdout)
    except (ValueError, json.JSONDecodeError):
        return {"verdict": "INCONCLUSIVE", "method": "none", "reason": "comparator emitted no parsable block"}
    return block


def _validate_mask(comparator: Comparator, goldens: list[str]) -> bool:
    # G3.5 (FP-3): a NORMALIZED mask field that VARIES across the varied-input goldens could be the
    # load-bearing answer — masking it would hide a real divergence. Valid only if every masked field is
    # constant across all goldens. Non-NORMALIZED / no mask = nothing to validate.
    if comparator.kind != "NORMALIZED" or not comparator.field_mask:
        return True
    seen: dict[str, set[str]] = {ptr: set() for ptr in comparator.field_mask}
    for g in goldens:
        try:
            obj = load_json(g)
        except (OSError, ValueError):
            return False  # can't prove a JSON mask valid against a non-JSON golden
        for ptr in comparator.field_mask:
            seen[ptr].add(canonical(json_pointer(obj, _strip_ptr(ptr))))
    return all(len(vals) <= 1 for vals in seen.values())


def mask_required_but_missing(comparator: Comparator) -> bool:
    # A known-nondeterministic binary container MUST carry a projection (or NORMALIZED field_mask); falling
    # through to BYTE_EQ on such a type is forbidden (DESIGN GEN-8).
    tag = (comparator.tag or "").lower()
    if tag in NONDETERMINISTIC_TAGS:
        return comparator.projection is None and comparator.kind not in ("NORMALIZED", "EXTRACTED")
    return False


def has_computed(plan: dict[str, Any]) -> list[str]:
    return [v["carrier"] for v in plan.get("values", []) if v.get("bucket") == "COMPUTED"]


def has_repeat(plan: dict[str, Any]) -> bool:
    return bool(plan.get("control_flow", {}).get("repeats"))


def _coverage(
    instances: list[Instance],
    runs_n: int,
    build_instance_id: str | None,
    computed_carriers: list[str],
    repeat_present: bool,
    mask_constant: bool,
) -> dict[str, Any]:
    fresh = [i for i in instances if i.role == "fresh"]
    # Held-out (the fresh/proof instances) must not be the build instance.
    fresh_not_build = all(i.id != build_instance_id for i in fresh) if build_instance_id else True

    # Mutual isolation: every pair of proof instances declares the other as isolated-from (different tenant).
    mutually_isolated = _mutually_isolated(instances)

    boundaries = sorted({i.boundary for i in instances})

    forces_pagination = any(i.forces_pagination for i in instances)
    # Every COMPUTED carrier must be perturbed by at least one instance.
    perturbed = {c for i in instances for c in i.perturbs_computed}
    perturbs_every_computed = all(c in perturbed for c in computed_carriers)

    return {
        "instances": len(instances),
        "min_runs_each": runs_n,
        "fresh_not_build_instance": fresh_not_build,
        "mutually_isolated": mutually_isolated,
        "boundaries_spanned": boundaries,
        "forces_pagination": forces_pagination if repeat_present else None,
        "perturbs_every_computed": perturbs_every_computed if computed_carriers else None,
        "mask_fields_constant_across_runs": mask_constant,
    }


def _mutually_isolated(instances: list[Instance]) -> bool:
    if len(instances) < 2:
        return False
    for a in instances:
        for b in instances:
            if a.id == b.id:
                continue
            # A pair is PROVEN isolated only by an explicit isolated-from declaration, or by two DISTINCT
            # non-None tenants. A None (unknown) tenant is never assumed isolated — it could equal the other.
            declared = b.id in a.isolated_from or a.id in b.isolated_from
            distinct_tenants = a.tenant is not None and b.tenant is not None and a.tenant != b.tenant
            if not (declared or distinct_tenants):
                return False
    return True


def _coverage_ok(coverage: dict[str, Any], comparator: Comparator) -> tuple[bool, str]:
    if coverage["instances"] < 2:
        return False, "need >=2 proof instances"
    if coverage["min_runs_each"] < 2:
        return False, "need >=2 runs per instance"
    if not coverage["fresh_not_build_instance"]:
        return False, "a proof instance is the build instance (shared-state false-pass)"
    if not coverage["mutually_isolated"]:
        return False, "proof instances are not mutually isolated"
    if len(coverage["boundaries_spanned"]) < 2:
        return False, "fewer than 2 declared input boundaries spanned"
    if coverage["forces_pagination"] is False:
        return False, "a REPEAT exists but no instance forces pagination"
    if coverage["perturbs_every_computed"] is False:
        return False, "a COMPUTED value was not perturbed by any instance"
    if not coverage["mask_fields_constant_across_runs"]:
        return False, "a masked field varies with input (illegal mask)"
    if mask_required_but_missing(comparator):
        return False, "nondeterministic binary tag requires a projection comparator, not BYTE_EQ"
    return True, "all coverage obligations met"


def prove(
    command: str,
    instances: list[Instance],
    comparator: Comparator,
    runner: Runner,
    runs_n: int = 2,
    plan: dict[str, Any] | None = None,
    build_instance_id: str | None = None,
    segment_id: str = "s0",
    mask_constant: bool | None = None,
) -> dict[str, Any]:
    plan = plan or {}
    computed_carriers = has_computed(plan)
    repeat_present = has_repeat(plan)

    run_blocks: list[dict[str, Any]] = []
    all_match = True
    first_divergence: str | None = None
    goldens_used: list[str] = []

    for inst in instances:
        results: list[dict[str, Any]] = []
        for run in range(1, runs_n + 1):
            api = runner.run_api(command, inst, run)
            golden = runner.run_golden(inst, run)
            if golden is not None:
                goldens_used.append(golden)
            if api is None or golden is None:
                all_match = False
                missing = "api command produced no output" if api is None else "no UI golden for this run"
                if first_divergence is None:
                    first_divergence = f"instance {inst.id} run {run}: {missing}"
                results.append({"run": run, "api": api, "golden": golden, "match": False,
                                "comparison": {"verdict": "MISMATCH", "method": "none", "reason": missing}})
                continue
            block = _verify_equivalence(api, golden, comparator)
            match = block.get("verdict") == "MATCH"
            if not match:
                all_match = False
                if first_divergence is None:
                    first_divergence = f"instance {inst.id} run {run}: {json.dumps(block)}"
            results.append({"run": run, "api": api, "golden": golden, "match": match, "comparison": block})

        block_entry: dict[str, Any] = {
            "instance": {
                "id": inst.id, "role": inst.role, "tenant": inst.tenant,
                "isolated_from": inst.isolated_from, "boundary": inst.boundary,
            },
            "n": runs_n,
            "results": results,
        }
        if inst.forces_pagination or inst.perturbs_computed:
            block_entry["forces"] = {
                "pagination": inst.forces_pagination,
                "perturbs_computed": inst.perturbs_computed,
            }
        run_blocks.append(block_entry)

    # G3.5: validate the mask against the actual goldens unless the caller fixed it (tests).
    if mask_constant is None:
        mask_constant = _validate_mask(comparator, goldens_used)
    coverage = _coverage(instances, runs_n, build_instance_id, computed_carriers, repeat_present, mask_constant)
    cov_ok, cov_reason = _coverage_ok(coverage, comparator)

    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "segment_id": segment_id,
        "comparator": {
            "kind": comparator.kind,
            "frozen": comparator.frozen,
            "tag": comparator.tag,
            "field_mask": comparator.field_mask,
            "projection": comparator.projection,
            "threshold": comparator.threshold,
            "mask_valid": mask_constant,
        },
        # The held-out instances the proof actually ran on, and the build instance held out (real ids, so
        # the receipt is auditable). The teach gate checks coverage.fresh_not_build_instance, not these.
        "proof_instances": [i.id for i in instances if i.role == "fresh"],
        "build_instance": build_instance_id,
        "runs": run_blocks,
        "coverage": coverage,
    }

    # Verdict precedence: a missing/mismatched MATCH is FAILED (BAIL-5); else an unmet obligation is
    # UNCOVERED; else PROVEN. The comparator must be frozen to ship at all.
    if not comparator.frozen:
        receipt["verdict"] = UNCOVERED
        receipt["bail"] = {"code": "BAIL-5", "reason": "comparator not frozen — cannot ship"}
    elif not all_match:
        receipt["verdict"] = FAILED
        receipt["bail"] = {"code": "BAIL-5", "reason": f"content_equal failed: {first_divergence}"}
    elif not cov_ok:
        receipt["verdict"] = UNCOVERED
        receipt["bail"] = {"code": "BAIL-5", "reason": f"coverage unmet: {cov_reason}"}
    else:
        receipt["verdict"] = PROVEN
        receipt["bail"] = None
    return receipt


def _load_instances(data: list[dict[str, Any]]) -> list[Instance]:
    return [
        Instance(
            id=d["id"],
            role=d["role"],
            boundary=d.get("boundary", "nominal"),
            tenant=d.get("tenant"),
            isolated_from=d.get("isolated_from", []),
            forces_pagination=bool(d.get("forces", {}).get("pagination", d.get("forces_pagination", False))),
            perturbs_computed=d.get("forces", {}).get("perturbs_computed", d.get("perturbs_computed", [])),
        )
        for d in data
    ]


def _load_comparator(d: dict[str, Any]) -> Comparator:
    return Comparator(
        kind=d.get("kind", "BYTE_EQ"),
        frozen=bool(d.get("frozen", False)),
        tag=d.get("tag"),
        field_mask=d.get("field_mask", []),
        projection=d.get("projection"),
        threshold=float(d.get("threshold", 0.9)),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="prove_runner")
    ap.add_argument("--command", required=True, help="command.sh — the API replay chain")
    ap.add_argument("--instances", required=True, help="json: list of instance descriptors")
    ap.add_argument("--comparator", required=True, help="json: the operator-frozen comparator")
    ap.add_argument("--plan", help="plan.json — to read COMPUTED carriers + REPEAT presence")
    ap.add_argument("--golden", help="json: {instance_id: [golden_path, ...]} for the UI export per run")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--build-instance", help="id of the build/capture instance (must be held out)")
    ap.add_argument("--segment-id", default="s0")
    ap.add_argument("--out", default="verify_receipt.json")
    args = ap.parse_args(argv)

    instances = _load_instances(json.load(open(args.instances)))
    comparator = _load_comparator(json.load(open(args.comparator)))
    plan = json.load(open(args.plan)) if args.plan else {}
    golden_paths = json.load(open(args.golden)) if args.golden else {}

    runner = SubprocessRunner(golden_paths)
    receipt = prove(
        command=args.command,
        instances=instances,
        comparator=comparator,
        runner=runner,
        runs_n=args.runs,
        plan=plan,
        build_instance_id=args.build_instance,
        segment_id=args.segment_id,
    )
    Path(args.out).write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["verdict"] == PROVEN else 1


if __name__ == "__main__":
    sys.exit(main())
