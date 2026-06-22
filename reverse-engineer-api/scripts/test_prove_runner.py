#!/usr/bin/env python3
# Tests for prove_runner.py — the S6/G3 PROVEN gate. Runs with plain `python3` (no pytest, no browser).
# The actual API run + UI golden are injected via a FakeRunner; the comparator block is injected by
# monkeypatching the module-level _verify_equivalence, so nothing here shells out or touches a live page.
import json
import sys
from typing import Any

import prove_runner as p

# ---- fakes -------------------------------------------------------------------------------------------


class FakeRunner:
    # api path is fake (the comparison is monkeypatched); the golden is written as REAL JSON so the G3.5
    # mask-validity check can load it. `vary_mask` makes the masked field differ per instance -> invalid mask.
    def __init__(self, missing_api: set[str] | None = None, missing_golden: set[str] | None = None,
                 vary_mask: bool = False) -> None:
        self._missing_api = missing_api or set()
        self._missing_golden = missing_golden or set()
        self._vary_mask = vary_mask

    def run_api(self, command: str, instance: "p.Instance", run: int) -> str | None:
        if instance.id in self._missing_api:
            return None
        return f"/tmp/api.{instance.id}.{run}.bin"

    def run_golden(self, instance: "p.Instance", run: int) -> str | None:
        if instance.id in self._missing_golden:
            return None
        creation = f"D:{instance.id}" if self._vary_mask else "D:20260101000000Z"
        obj = {"answer": 42, "metadata": {"CreationDate": creation}}
        path = f"/tmp/golden.{instance.id}.{run}.json"
        with open(path, "w") as f:
            json.dump(obj, f)
        return path


def _match_block(_api: str, _golden: str, _cmp: "p.Comparator") -> dict[str, Any]:
    return {"verdict": "MATCH", "method": "pdf-text-jaccard", "overlap": 0.98, "threshold": 0.9}


def _mismatch_block(_api: str, _golden: str, _cmp: "p.Comparator") -> dict[str, Any]:
    return {"verdict": "MISMATCH", "method": "pdf-text-jaccard", "overlap": 0.40, "threshold": 0.9}


def _patch(block_fn: Any) -> Any:
    orig = p._verify_equivalence
    p._verify_equivalence = block_fn
    return orig


def _unpatch(orig: Any) -> None:
    p._verify_equivalence = orig


# ---- fixtures ----------------------------------------------------------------------------------------

# Two mutually-isolated instances (different tenants, cross-declared), spanning two boundaries.
def _two_good_instances() -> list[p.Instance]:
    return [
        p.Instance(id="inv_777", role="fresh", boundary="nominal", tenant="org_777", isolated_from=["org_888"]),
        p.Instance(id="inv_888", role="boundary", boundary="large-paginating", tenant="org_888",
                   isolated_from=["org_777"], forces_pagination=True,
                   perturbs_computed=["json-ptr:/variables/idempotencyKey"]),
    ]


def _frozen_cmp(**kw: Any) -> p.Comparator:
    base: dict[str, Any] = {"kind": "NORMALIZED", "frozen": True, "tag": "pdf", "threshold": 0.9}
    base.update(kw)
    return p.Comparator(**base)


def _plan(computed: list[str] | None = None, repeats: bool = False) -> dict[str, Any]:
    values = [{"bucket": "COMPUTED", "carrier": c} for c in (computed or [])]
    return {"values": values, "control_flow": {"repeats": [{"node": "n9"}] if repeats else []}}


# ---- happy path --------------------------------------------------------------------------------------


def test_all_match_isolated_boundary_is_proven() -> None:
    orig = _patch(_match_block)
    try:
        r = p.prove(
            command="command.sh",
            instances=_two_good_instances(),
            comparator=_frozen_cmp(field_mask=["/metadata/CreationDate"]),
            runner=FakeRunner(),
            runs_n=2,
            plan=_plan(computed=["json-ptr:/variables/idempotencyKey"], repeats=True),
            build_instance_id="inv_build",
        )
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.PROVEN, r["verdict"]
    assert r["bail"] is None
    assert r["schema"] == "verify_receipt/v1"
    assert r["comparator"]["frozen"] is True and r["comparator"]["mask_valid"] is True
    assert r["coverage"]["mutually_isolated"] is True
    assert r["coverage"]["forces_pagination"] is True
    assert r["coverage"]["perturbs_every_computed"] is True
    # every instance carries n>=2 results, each MATCH
    assert all(b["n"] == 2 and all(x["match"] for x in b["results"]) for b in r["runs"])


# ---- content MATCH failures (BAIL-5 / FAILED) --------------------------------------------------------


def test_any_mismatch_is_failed_bail5() -> None:
    orig = _patch(_mismatch_block)
    try:
        r = p.prove("command.sh", _two_good_instances(), _frozen_cmp(), FakeRunner(), runs_n=2)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.FAILED
    assert r["bail"]["code"] == "BAIL-5" and "content_equal failed" in r["bail"]["reason"]


def test_missing_api_output_is_failed() -> None:
    orig = _patch(_match_block)
    try:
        r = p.prove("command.sh", _two_good_instances(), _frozen_cmp(), FakeRunner(missing_api={"inv_888"}),
                    runs_n=2)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.FAILED
    assert "no output" in r["bail"]["reason"]


def test_missing_golden_is_failed() -> None:
    orig = _patch(_match_block)
    try:
        r = p.prove("command.sh", _two_good_instances(), _frozen_cmp(), FakeRunner(missing_golden={"inv_777"}),
                    runs_n=2)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.FAILED


# ---- coverage failures (UNCOVERED, never a false ship) -----------------------------------------------


def test_single_instance_is_uncovered() -> None:
    orig = _patch(_match_block)
    try:
        one = [p.Instance(id="a", role="fresh", boundary="nominal", tenant="org_a", isolated_from=["org_b"])]
        r = p.prove("command.sh", one, _frozen_cmp(), FakeRunner(), runs_n=2)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.UNCOVERED and "2 proof instances" in r["bail"]["reason"]


def test_runs_below_two_is_uncovered() -> None:
    orig = _patch(_match_block)
    try:
        r = p.prove("command.sh", _two_good_instances(), _frozen_cmp(), FakeRunner(), runs_n=1)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.UNCOVERED and ">=2 runs" in r["bail"]["reason"]


def test_build_instance_reused_is_uncovered() -> None:
    orig = _patch(_match_block)
    try:
        insts = _two_good_instances()
        # the fresh proof instance IS the build instance -> shared-state false-pass
        r = p.prove("command.sh", insts, _frozen_cmp(), FakeRunner(), runs_n=2, build_instance_id="inv_777")
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.UNCOVERED and "build instance" in r["bail"]["reason"]


def test_not_mutually_isolated_is_uncovered() -> None:
    orig = _patch(_match_block)
    try:
        # same tenant, no cross-declared isolation -> NOT isolated
        insts = [
            p.Instance(id="a", role="fresh", boundary="nominal", tenant="org_same"),
            p.Instance(id="b", role="boundary", boundary="max", tenant="org_same"),
        ]
        r = p.prove("command.sh", insts, _frozen_cmp(), FakeRunner(), runs_n=2)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.UNCOVERED and "isolated" in r["bail"]["reason"]


def test_single_boundary_band_is_uncovered() -> None:
    orig = _patch(_match_block)
    try:
        insts = [
            p.Instance(id="a", role="fresh", boundary="nominal", tenant="org_a", isolated_from=["org_b"]),
            p.Instance(id="b", role="boundary", boundary="nominal", tenant="org_b", isolated_from=["org_a"]),
        ]
        r = p.prove("command.sh", insts, _frozen_cmp(), FakeRunner(), runs_n=2)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.UNCOVERED and "boundaries" in r["bail"]["reason"]


def test_repeat_without_pagination_forcing_is_uncovered() -> None:
    orig = _patch(_match_block)
    try:
        # plan HAS a REPEAT, but no instance forces pagination
        insts = [
            p.Instance(id="a", role="fresh", boundary="nominal", tenant="org_a", isolated_from=["org_b"]),
            p.Instance(id="b", role="boundary", boundary="max", tenant="org_b", isolated_from=["org_a"]),
        ]
        r = p.prove("command.sh", insts, _frozen_cmp(), FakeRunner(), runs_n=2, plan=_plan(repeats=True))
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.UNCOVERED and "pagination" in r["bail"]["reason"]


def test_computed_not_perturbed_is_uncovered() -> None:
    orig = _patch(_match_block)
    try:
        # plan declares a COMPUTED carrier no instance perturbs
        insts = [
            p.Instance(id="a", role="fresh", boundary="nominal", tenant="org_a", isolated_from=["org_b"]),
            p.Instance(id="b", role="boundary", boundary="max", tenant="org_b", isolated_from=["org_a"]),
        ]
        r = p.prove("command.sh", insts, _frozen_cmp(), FakeRunner(), runs_n=2,
                    plan=_plan(computed=["json-ptr:/nonce"]))
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.UNCOVERED and "COMPUTED" in r["bail"]["reason"]


def test_invalid_mask_is_uncovered() -> None:
    orig = _patch(_match_block)
    try:
        # mask_constant=False: a masked field varies with input -> illegal mask
        r = p.prove("command.sh", _two_good_instances(), _frozen_cmp(field_mask=["/x"]), FakeRunner(),
                    runs_n=2, mask_constant=False)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.UNCOVERED and "masked field" in r["bail"]["reason"]
    assert r["comparator"]["mask_valid"] is False


def test_unfrozen_comparator_cannot_ship() -> None:
    orig = _patch(_match_block)
    try:
        r = p.prove("command.sh", _two_good_instances(), _frozen_cmp(frozen=False), FakeRunner(), runs_n=2)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.UNCOVERED and "not frozen" in r["bail"]["reason"]


def test_nondeterministic_binary_requires_projection() -> None:
    orig = _patch(_match_block)
    try:
        # BYTE_EQ on a pdf with no projection -> forbidden fallthrough
        cmp_ = p.Comparator(kind="BYTE_EQ", frozen=True, tag="pdf", projection=None)
        r = p.prove("command.sh", _two_good_instances(), cmp_, FakeRunner(), runs_n=2)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.UNCOVERED and "projection" in r["bail"]["reason"]


def test_nondeterministic_binary_with_projection_is_allowed() -> None:
    orig = _patch(_match_block)
    try:
        cmp_ = p.Comparator(kind="EXTRACTED", frozen=True, tag="pdf", projection="pdf-text")
        r = p.prove("command.sh", _two_good_instances(), cmp_, FakeRunner(), runs_n=2)
    finally:
        _unpatch(orig)
    assert r["verdict"] == p.PROVEN, r.get("bail")


# ---- pure-predicate units ----------------------------------------------------------------------------


def test_mutually_isolated_predicate() -> None:
    iso = [
        p.Instance(id="a", role="fresh", boundary="nominal", tenant="org_a", isolated_from=["org_b"]),
        p.Instance(id="b", role="boundary", boundary="max", tenant="org_b", isolated_from=["org_a"]),
    ]
    assert p._mutually_isolated(iso) is True
    same = [
        p.Instance(id="a", role="fresh", boundary="nominal", tenant="t"),
        p.Instance(id="b", role="boundary", boundary="max", tenant="t"),
    ]
    assert p._mutually_isolated(same) is False
    assert p._mutually_isolated(iso[:1]) is False  # one instance is never isolated


def test_mask_required_but_missing() -> None:
    assert p.mask_required_but_missing(p.Comparator(kind="BYTE_EQ", frozen=True, tag="pdf")) is True
    assert p.mask_required_but_missing(p.Comparator(kind="EXTRACTED", frozen=True, tag="pdf",
                                                    projection="pdf-text")) is False
    # a deterministic text artifact may BYTE_EQ
    assert p.mask_required_but_missing(p.Comparator(kind="CANONICAL_JSON_EQ", frozen=True, tag="json")) is False


def test_has_computed_and_repeat() -> None:
    plan = _plan(computed=["a", "b"], repeats=True)
    assert p.has_computed(plan) == ["a", "b"]
    assert p.has_repeat(plan) is True
    assert p.has_computed({}) == [] and p.has_repeat({}) is False


def test_loaders_round_trip() -> None:
    insts = p._load_instances([
        {"id": "x", "role": "fresh", "boundary": "min", "tenant": "org_x", "isolated_from": ["org_y"],
         "forces": {"pagination": True, "perturbs_computed": ["/n"]}},
    ])
    assert insts[0].forces_pagination is True and insts[0].perturbs_computed == ["/n"]
    cmp_ = p._load_comparator({"kind": "NORMALIZED", "frozen": True, "tag": "pdf", "threshold": 0.8})
    assert cmp_.frozen is True and cmp_.threshold == 0.8


def test_receipt_embeds_comparison_block_verbatim() -> None:
    orig = _patch(_match_block)
    try:
        r = p.prove("command.sh", _two_good_instances(), _frozen_cmp(), FakeRunner(), runs_n=2)
    finally:
        _unpatch(orig)
    block = r["runs"][0]["results"][0]["comparison"]
    assert block["method"] == "pdf-text-jaccard" and block["overlap"] == 0.98


# ---- regressions for the review fixes ----------------------------------------------------------------

def test_mask_that_varies_with_input_is_uncovered() -> None:
    # G3.5 regression: a NORMALIZED mask field that VARIES across the varied-input goldens is illegal to
    # mask (it could be the load-bearing answer) -> mask_valid false -> not PROVEN.
    orig = _patch(_match_block)
    try:
        r = p.prove("command.sh", _two_good_instances(),
                    _frozen_cmp(field_mask=["/metadata/CreationDate"]), FakeRunner(vary_mask=True), runs_n=2,
                    plan=_plan(repeats=True), build_instance_id="inv_build")
    finally:
        _unpatch(orig)
    assert r["verdict"] != p.PROVEN, r["verdict"]
    assert r["coverage"]["mask_fields_constant_across_runs"] is False


def test_unknown_tenant_is_not_isolated() -> None:
    # isolation regression: a (tenant=X, tenant=None) pair is NOT proven isolated (None could equal X).
    inst = [
        p.Instance(id="a", role="fresh", boundary="nominal", tenant="org_x"),
        p.Instance(id="b", role="fresh", boundary="large-paginating", tenant=None),
    ]
    assert p._mutually_isolated(inst) is False


def test_run_api_inherits_environment() -> None:
    # env regression: run_api must inherit the real environment (PATH etc.), not replace it with PROVE_*.
    import os
    import tempfile
    os.environ["PROVE_ENV_SENTINEL"] = "present"
    try:
        with tempfile.TemporaryDirectory() as d:
            cmd = os.path.join(d, "command.sh")
            # only writes output if the inherited sentinel is visible -> proves env was NOT wiped
            with open(cmd, "w") as f:
                f.write('#!/bin/bash\n[ -n "$PROVE_ENV_SENTINEL" ] && echo ok > "$PROVE_OUT"\n')
            out = p.SubprocessRunner({}).run_api(cmd, p.Instance(id="i1", role="fresh", boundary="nominal"), 1)
            assert out is not None, "run_api wiped the environment (PATH/sentinel lost)"
    finally:
        del os.environ["PROVE_ENV_SENTINEL"]


if __name__ == "__main__":
    import types

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
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
