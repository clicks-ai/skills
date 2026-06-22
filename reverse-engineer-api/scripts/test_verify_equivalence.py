#!/usr/bin/env python3
# Tests for verify_equivalence.py — dependency-free (no poppler/pypdf needed).
import os
import tempfile

import verify_equivalence as v


def _w(d: str, name: str, data: bytes) -> str:
    p = os.path.join(d, name)
    open(p, "wb").write(data)
    return p


def _wj(d: str, name: str, obj: object) -> str:
    import json as _json

    return _w(d, name, _json.dumps(obj).encode("utf-8"))


def test_token_jaccard() -> None:
    assert v.token_jaccard("a b c", "a b c") == 1.0
    assert v.token_jaccard("a b c", "x y z") == 0.0
    assert v.token_jaccard("", "") == 1.0
    assert v.token_jaccard("a b", "") == 0.0
    assert 0.0 < v.token_jaccard("a b c d", "a b x y") < 1.0
    # robust to whitespace/case/order
    assert v.token_jaccard("James  GOOGLE\nEngineer", "engineer james google") == 1.0


def test_size_ratio() -> None:
    assert v.size_ratio(100, 100) == 1.0
    assert v.size_ratio(45, 107) < 0.5
    assert v.size_ratio(0, 0) == 1.0


def test_is_pdf_and_file_info() -> None:
    with tempfile.TemporaryDirectory() as d:
        pdf = _w(d, "a.pdf", b"%PDF-1.7\n...stuff...")
        txt = _w(d, "a.txt", b"hello")
        assert v.is_pdf(pdf) and not v.is_pdf(txt)
        fi = v.file_info(pdf)
        assert fi["bytes"] == len(b"%PDF-1.7\n...stuff...") and len(fi["sha256"]) == 16


def test_byte_identical_is_match() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _w(d, "a.bin", b"\x89PNG identical bytes")
        b = _w(d, "b.bin", b"\x89PNG identical bytes")
        r = v.compare(a, b, 0.9)
        assert r["verdict"] == "MATCH" and r["method"] == "sha256"


def test_non_pdf_different_is_mismatch() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _w(d, "a.bin", b"one")
        b = _w(d, "b.bin", b"two different")
        r = v.compare(a, b, 0.9)
        assert r["verdict"] == "MISMATCH" and r["method"] == "bytes"


def test_pdf_same_text_is_match(monkeypatch) -> None:  # type: ignore
    with tempfile.TemporaryDirectory() as d:
        a = _w(d, "a.pdf", b"%PDF-1.7 aaaa")
        b = _w(d, "b.pdf", b"%PDF-1.7 bbbb")  # different bytes, same extracted text
        monkeypatch.setattr(v, "pdf_text", lambda p: "James Google Engineer California")
        r = v.compare(a, b, 0.9)
        assert r["verdict"] == "MATCH" and r["method"] == "pdf-text-jaccard" and r["overlap"] == 1.0


def test_pdf_different_text_is_mismatch(monkeypatch) -> None:  # type: ignore
    with tempfile.TemporaryDirectory() as d:
        a = _w(d, "a.pdf", b"%PDF-1.7 aaaa")
        b = _w(d, "b.pdf", b"%PDF-1.7 bbbb")
        texts = {"a.pdf": "this is not a valid profile go back",  # the canary junk
                 "b.pdf": "james google software engineer california salary career highlights techster"}
        monkeypatch.setattr(v, "pdf_text", lambda p: texts.get(os.path.basename(p), ""))
        r = v.compare(a, b, 0.9)
        assert r["verdict"] == "MISMATCH" and r["method"] == "pdf-text-jaccard"


def test_pdf_no_extractor_is_inconclusive(monkeypatch) -> None:  # type: ignore
    with tempfile.TemporaryDirectory() as d:
        a = _w(d, "a.pdf", b"%PDF-1.7 aaaa")
        b = _w(d, "b.pdf", b"%PDF-1.7 bbbbbbbb")
        monkeypatch.setattr(v, "pdf_text", lambda p: None)
        r = v.compare(a, b, 0.9)
        assert r["verdict"] == "INCONCLUSIVE"


# ---- json-pointer + canonicalization helpers ----
def test_canonical_is_key_order_insensitive() -> None:
    assert v.canonical({"b": 1, "a": 2}) == v.canonical({"a": 2, "b": 1})
    assert v.canonical({"a": 1}) != v.canonical({"a": 2})


def test_json_pointer_reads_nested_and_indices() -> None:
    obj = {"a": {"b": [10, 20, 30]}}
    assert v.json_pointer(obj, "/a/b/1") == 20
    assert v.json_pointer(obj, "") == obj
    assert v.json_pointer(obj, "/a/b/-1") == 30
    assert v.json_pointer(obj, "/a/missing") is v._PTR_SENTINEL
    assert v.json_pointer(obj, "/a/b/99") is v._PTR_SENTINEL


def test_apply_mask_deletes_pointer() -> None:
    masked = v.apply_mask({"id": 1, "ts": "now", "n": {"created": "x", "keep": "y"}}, ["/ts", "/n/created"])
    assert masked == {"id": 1, "n": {"keep": "y"}}


def test_strip_ptr_accepts_both_forms() -> None:
    assert v._strip_ptr("json-ptr:/a/b") == "/a/b"
    assert v._strip_ptr("/a/b") == "/a/b"


# ---- CANONICAL_JSON_EQ ----
def test_canonical_json_eq_match_ignores_key_order() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _wj(d, "a.json", {"x": 1, "y": [2, 3]})
        b = _wj(d, "b.json", {"y": [2, 3], "x": 1})
        r = v.compare(a, b, 0.9, comparator="CANONICAL_JSON_EQ")
        assert r["verdict"] == "MATCH" and r["method"] == "canonical-json"


def test_canonical_json_eq_mismatch_on_value() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _wj(d, "a.json", {"x": 1})
        b = _wj(d, "b.json", {"x": 2})
        r = v.compare(a, b, 0.9, comparator="CANONICAL_JSON_EQ")
        assert r["verdict"] == "MISMATCH"


def test_canonical_json_eq_bad_json_is_inconclusive() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _w(d, "a.json", b"{not json")
        b = _wj(d, "b.json", {"x": 1})
        r = v.compare(a, b, 0.9, comparator="CANONICAL_JSON_EQ")
        assert r["verdict"] == "INCONCLUSIVE"


# ---- NORMALIZED(field_mask) ----
def test_normalized_masks_volatile_field() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _wj(d, "a.json", {"total": 42, "generatedAt": "2026-06-22T10:00:00Z"})
        b = _wj(d, "b.json", {"total": 42, "generatedAt": "2026-06-22T11:59:59Z"})
        r = v.compare(a, b, 0.9, comparator="NORMALIZED", mask=["/generatedAt"])
        assert r["verdict"] == "MATCH" and r["method"] == "normalized"


def test_normalized_catches_unmasked_difference() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _wj(d, "a.json", {"total": 42, "generatedAt": "t1"})
        b = _wj(d, "b.json", {"total": 99, "generatedAt": "t2"})  # answer differs -> must MISMATCH
        r = v.compare(a, b, 0.9, comparator="NORMALIZED", mask=["/generatedAt"])
        assert r["verdict"] == "MISMATCH"


# ---- EXTRACTED(projection) ----
def test_extracted_projects_the_load_bearing_value() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _wj(d, "a.json", {"meta": {"rid": "abc"}, "result": {"total": 7}})
        b = _wj(d, "b.json", {"meta": {"rid": "zzz"}, "result": {"total": 7}})  # rid differs, total same
        r = v.compare(a, b, 0.9, comparator="EXTRACTED", projection="/result/total")
        assert r["verdict"] == "MATCH" and r["method"] == "extracted"


def test_extracted_mismatch_on_projected_value() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _wj(d, "a.json", {"result": {"total": 7}})
        b = _wj(d, "b.json", {"result": {"total": 8}})
        r = v.compare(a, b, 0.9, comparator="EXTRACTED", projection="/result/total")
        assert r["verdict"] == "MISMATCH"


def test_extracted_missing_projection_is_inconclusive() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _wj(d, "a.json", {"result": {"total": 7}})
        b = _wj(d, "b.json", {"result": {"total": 7}})
        r = v.compare(a, b, 0.9, comparator="EXTRACTED", projection="/result/missing")
        assert r["verdict"] == "INCONCLUSIVE"


def test_extracted_requires_projection_arg() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _wj(d, "a.json", {"x": 1})
        b = _wj(d, "b.json", {"x": 1})
        r = v.compare(a, b, 0.9, comparator="EXTRACTED", projection=None)
        assert r["verdict"] == "INCONCLUSIVE" and "requires --projection" in r["reason"]


# ---- ASSEMBLED(reduce over a set of responses) ----
def test_assembled_concatenates_paginated_frames() -> None:
    with tempfile.TemporaryDirectory() as d:
        p1 = _wj(d, "p1.json", {"items": [1, 2], "next": "c1"})
        p2 = _wj(d, "p2.json", {"items": [3, 4], "next": None})
        api = _wj(d, "api.json", {"items": [1, 2, 3, 4]})  # api assembled the pages itself
        r = v.compare(api, api, 0.9, comparator="ASSEMBLED", projection="/items", golden_set=[p1, p2])
        assert r["verdict"] == "MATCH" and r["method"] == "assembled" and r["parts"] == 2


def test_assembled_detects_truncation() -> None:
    # the FP-6 case: the api dropped a page -> the assembled goldens must NOT match a truncated api.
    with tempfile.TemporaryDirectory() as d:
        p1 = _wj(d, "p1.json", {"items": [1, 2]})
        p2 = _wj(d, "p2.json", {"items": [3, 4]})
        api = _wj(d, "api.json", {"items": [1, 2]})  # missing the second page
        r = v.compare(api, api, 0.9, comparator="ASSEMBLED", projection="/items", golden_set=[p1, p2])
        assert r["verdict"] == "MISMATCH"


def test_assemble_helper_flattens_lists() -> None:
    parts = [{"items": [1, 2]}, {"items": [3]}, {"items": []}]
    assert v.assemble(parts, "/items") == [1, 2, 3]


# ---- BYTE_EQ + nondeterministic-container guard (GEN-8) ----
def test_byte_eq_matches_identical_text() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _w(d, "a.csv", b"id,name\n1,x\n")
        b = _w(d, "b.csv", b"id,name\n1,x\n")
        r = v.compare(a, b, 0.9, comparator="BYTE_EQ")
        assert r["verdict"] == "MATCH" and r["method"] == "sha256"


def test_byte_eq_forbidden_on_pdf_container() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _w(d, "a.pdf", b"%PDF-1.7 aaaa")
        b = _w(d, "b.pdf", b"%PDF-1.7 bbbb")
        r = v.compare(a, b, 0.9, comparator="BYTE_EQ")
        assert r["verdict"] == "INCONCLUSIVE" and "forbidden" in r["reason"]


def test_byte_eq_forbidden_on_zip_container() -> None:
    with tempfile.TemporaryDirectory() as d:
        a = _w(d, "a.zip", b"PK\x03\x04zzzz")
        b = _w(d, "b.zip", b"PK\x03\x04zzzz")
        r = v.compare(a, b, 0.9, comparator="BYTE_EQ")
        assert r["verdict"] == "INCONCLUSIVE"  # even byte-identical zips refuse BYTE_EQ -> projection required


def test_auto_path_no_byte_eq_fallthrough_on_png() -> None:
    # legacy auto path must NOT emit a bytes MISMATCH on a nondeterministic container; it stays INCONCLUSIVE.
    with tempfile.TemporaryDirectory() as d:
        a = _w(d, "a.png", b"\x89PNG\r\n\x1a\nAAAA")
        b = _w(d, "b.png", b"\x89PNG\r\n\x1a\nBBBB")
        r = v.compare(a, b, 0.9)
        assert r["verdict"] == "INCONCLUSIVE" and "no BYTE_EQ fallthrough" in r["reason"]


def test_container_tag_detection() -> None:
    with tempfile.TemporaryDirectory() as d:
        assert v.container_tag(_w(d, "a.pdf", b"%PDF-1.7")) == "pdf"
        assert v.container_tag(_w(d, "a.zip", b"PK\x03\x04")) == "zip"
        assert v.container_tag(_w(d, "a.png", b"\x89PNG\r\n")) == "image"
        assert v.container_tag(_w(d, "a.csv", b"id,name\n")) is None


# ---- MASK-VALIDITY (a masked field that varies with input is illegal -> error) ----
def test_mask_validity_passes_when_masked_field_constant() -> None:
    with tempfile.TemporaryDirectory() as d:
        # two varied-input runs; the masked field (createdAt) is volatile, the answer (total) varies legally.
        r1 = _wj(d, "r1.json", {"total": 10, "createdAt": "2026-01-01"})
        r2 = _wj(d, "r2.json", {"total": 20, "createdAt": "2026-01-01"})
        out = v.check_mask_validity(["/createdAt"], [r1, r2])
        assert out["mask_valid"] is True and out["checked"] is True


def test_mask_validity_rejects_input_varying_masked_field() -> None:
    with tempfile.TemporaryDirectory() as d:
        # masking /total would hide the very value that varies with input -> illegal.
        r1 = _wj(d, "r1.json", {"total": 10, "createdAt": "2026-01-01"})
        r2 = _wj(d, "r2.json", {"total": 20, "createdAt": "2026-01-01"})
        raised = False
        try:
            v.check_mask_validity(["/total"], [r1, r2])
        except v.MaskValidityError as e:
            raised = True
            assert "vary with input" in str(e)
        assert raised, "masking an input-varying field must raise MaskValidityError"


def test_mask_validity_needs_two_runs() -> None:
    with tempfile.TemporaryDirectory() as d:
        r1 = _wj(d, "r1.json", {"total": 10, "createdAt": "x"})
        raised = False
        try:
            v.check_mask_validity(["/createdAt"], [r1])
        except v.MaskValidityError:
            raised = True
        assert raised, "mask-validity needs >=2 varied-input runs"


def test_mask_validity_empty_mask_is_trivially_valid() -> None:
    out = v.check_mask_validity([], ["nonexistent_is_ignored"])
    assert out["mask_valid"] is True and out["checked"] is False


# ---- verify_receipt.json (CONTRACTS §4) ----
def _match_run(match: bool, **inst: object) -> dict:
    return {
        "instance": {"id": "i", "role": "fresh", **inst},
        "n": 2,
        "results": [
            {"run": 1, "api": "/tmp/a", "golden": "/tmp/g", "match": match,
             "comparison": {"verdict": "MATCH" if match else "MISMATCH", "method": "normalized", "reason": "x"}},
            {"run": 2, "api": "/tmp/a", "golden": "/tmp/g", "match": match,
             "comparison": {"verdict": "MATCH" if match else "MISMATCH", "method": "normalized", "reason": "x"}},
        ],
    }


def _full_coverage() -> dict:
    return {
        "instances": 2, "min_runs_each": 2, "fresh_not_build_instance": True, "mutually_isolated": True,
        "boundaries_spanned": ["nominal", "large-paginating"], "forces_pagination": True,
        "perturbs_every_computed": True, "mask_fields_constant_across_runs": True,
    }


def test_receipt_proven_when_all_match_and_covered() -> None:
    rec = v.build_receipt(
        "s0", "NORMALIZED", [_match_run(True), _match_run(True)],
        tag="pdf", field_mask=["/metadata/CreationDate"], coverage=_full_coverage(),
    )
    assert rec["schema"] == "verify_receipt/v1" and rec["segment_id"] == "s0"
    assert rec["verdict"] == "PROVEN" and rec["bail"] is None
    assert rec["comparator"]["frozen"] is True and rec["comparator"]["kind"] == "NORMALIZED"


def test_receipt_failed_on_any_mismatch() -> None:
    rec = v.build_receipt("s0", "NORMALIZED", [_match_run(True), _match_run(False)], coverage=_full_coverage())
    assert rec["verdict"] == "FAILED" and rec["bail"]["code"] == "BAIL-5"


def test_receipt_failed_when_mask_invalid() -> None:
    rec = v.build_receipt("s0", "NORMALIZED", [_match_run(True)], mask_valid=False, coverage=_full_coverage())
    assert rec["verdict"] == "FAILED" and "mask" in rec["bail"]["reason"].lower()


def test_receipt_uncovered_when_coverage_missing() -> None:
    cov = _full_coverage()
    cov["mutually_isolated"] = False
    rec = v.build_receipt("s0", "EXTRACTED", [_match_run(True), _match_run(True)], coverage=cov)
    assert rec["verdict"] == "UNCOVERED" and rec["bail"] is None


def test_receipt_roundtrips_to_disk() -> None:
    import json as _json

    with tempfile.TemporaryDirectory() as d:
        rec = v.build_receipt("s0", "CANONICAL_JSON_EQ", [_match_run(True), _match_run(True)], coverage=_full_coverage())
        path = os.path.join(d, "verify_receipt.json")
        v.write_receipt(path, rec)
        loaded = _json.loads(open(path).read())
        assert loaded["verdict"] == "PROVEN" and loaded["comparator"]["kind"] == "CANONICAL_JSON_EQ"


if __name__ == "__main__":
    # tiny runner so it works with or without pytest (matches the other test files)
    import sys
    import types

    class _MP:
        def __init__(self) -> None:
            self._undo: list = []

        def setattr(self, obj: object, name: str, val: object) -> None:
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def undo(self) -> None:
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo.clear()

    tests = [(k, fn) for k, fn in sorted(globals().items())
             if k.startswith("test_") and isinstance(fn, types.FunctionType)]
    failed = 0
    for name, fn in tests:
        mp = _MP()
        try:
            if "monkeypatch" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                fn(mp)
            else:
                fn()
            print(f"  PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {e}")
        finally:
            mp.undo()
    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'} ({len(tests)} tests)")
    sys.exit(1 if failed else 0)
