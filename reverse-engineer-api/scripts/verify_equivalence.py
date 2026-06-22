#!/usr/bin/env python3
# verify_equivalence.py — THE teach-time gate.
#
# "A file was produced" is NOT success. "The API's output equals the UI's output, on an instance we did
# NOT set up by hand" is. This compares the API artifact against the UI golden at the CONTENT level
# (PDFs carry volatile metadata, so naive byte-equality is wrong) and exits:
#   0  -> MATCH      (ship it as API)
#   1  -> MISMATCH   (keep UI — the chain does not faithfully reproduce the UI)
#   3  -> INCONCLUSIVE (no text extractor available; a human must eyeball both artifacts)
#
# Comparators (operator-declared, frozen per segment — DESIGN §2.5 / CONTRACTS §4):
#   BYTE_EQ            sha256 byte-equality                 (text/structured artifacts)
#   CANONICAL_JSON_EQ  parse + sort keys + compare          (JSON, key-order/whitespace insensitive)
#   NORMALIZED         mask volatile fields, compare rest   (timestamps, generated ids)
#   EXTRACTED          project a sub-value, compare that    (the load-bearing answer only)
#   ASSEMBLED          reduce a SET of responses to one     (streamed / paginated goldens)
# The legacy pdf-text-jaccard path (and its INCONCLUSIVE fallback) is preserved as the default for the
# binary-projection case where the projection is "extracted PDF text".
#
# Two known-nondeterministic facts the design makes mechanical:
#   * a masked field that VARIES WITH INPUT is illegal to mask (MASK-VALIDITY -> error). FP-3.
#   * a known-nondeterministic binary container (image/zip/pdf) REQUIRES a projection; BYTE_EQ
#     fallthrough is forbidden (GEN-8).
#
# Usage:
#   python verify_equivalence.py --api /tmp/api_out.pdf --golden /tmp/ui_golden.pdf [--threshold 0.9]
#   python verify_equivalence.py --api a.json --golden b.json --comparator CANONICAL_JSON_EQ
#   python verify_equivalence.py --api a.json --golden b.json --comparator NORMALIZED --mask /ts --mask /id
#   python verify_equivalence.py --api a.json --golden b.json --comparator EXTRACTED --projection /total
#   python verify_equivalence.py --comparator ASSEMBLED --projection /items --golden-set p1 p2 --api whole.json

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

MATCH, MISMATCH, INCONCLUSIVE = 0, 1, 3

COMPARATORS = ("BYTE_EQ", "CANONICAL_JSON_EQ", "NORMALIZED", "EXTRACTED", "ASSEMBLED")
# containers whose bytes are non-reproducible by construction (metadata, compression, ordering) —
# a projection is mandatory; byte-equality on these is meaningless. Keyed by leading magic bytes.
NONDETERMINISTIC_MAGIC = {b"%PDF-": "pdf", b"PK\x03\x04": "zip", b"\x89PNG": "image", b"GIF8": "image"}


class MaskValidityError(Exception):
    # a masked field that varies with input is illegal to mask (FP-3 / G3.5).
    pass


def file_info(path: str) -> dict[str, object]:
    data = open(path, "rb").read()
    return {
        "path": path,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest()[:16],
        "magic": data[:5].decode("latin-1"),
    }


def _nbytes(info: dict[str, object]) -> int:
    n = info.get("bytes", 0)
    return n if isinstance(n, int) else 0


def is_pdf(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(5) == b"%PDF-"


def container_tag(path: str) -> str | None:
    with open(path, "rb") as f:
        head = f.read(8)
    for magic, tag in NONDETERMINISTIC_MAGIC.items():
        if head.startswith(magic):
            return tag
    return None


def pdf_text(path: str) -> str | None:
    """Extract text via poppler's pdftotext, else pypdf. None if neither is available."""
    try:
        out = subprocess.run(["pdftotext", "-q", path, "-"], capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            return out.stdout
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass
    try:
        from pypdf import PdfReader  # type: ignore

        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    except Exception:
        return None


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def token_jaccard(a: str | None, b: str | None) -> float:
    """Overlap of word sets — robust to reordering/metadata, sensitive to different CONTENT."""
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def size_ratio(a: int, b: int) -> float:
    hi = max(a, b)
    return round(min(a, b) / hi, 3) if hi else 1.0


# ---- JSON pointer + canonicalization (shared by the structured comparators) ----

def load_json(path: str) -> object:
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def canonical(obj: object) -> str:
    # sort keys + no whitespace -> a stable string equal iff the structures are equal.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_PTR_SENTINEL = object()


def json_pointer(obj: object, ptr: str) -> object:
    # RFC-6901-lite: "/a/0/b". "" -> whole doc. Missing path -> sentinel (caller decides).
    cur = obj
    if ptr in ("", "/"):
        return cur
    for raw in ptr.lstrip("/").split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if key not in cur:
                return _PTR_SENTINEL
            cur = cur[key]
        elif isinstance(cur, list):
            if not re.fullmatch(r"-?\d+", key):
                return _PTR_SENTINEL
            i = int(key)
            if i < -len(cur) or i >= len(cur):
                return _PTR_SENTINEL
            cur = cur[i]
        else:
            return _PTR_SENTINEL
    return cur


def _strip_ptr(ptr: str) -> str:
    # accept either "json-ptr:/a/b" or a bare "/a/b".
    return ptr.split("json-ptr:", 1)[1] if ptr.startswith("json-ptr:") else ptr


def apply_mask(obj: object, mask: list[str]) -> object:
    # return a deep copy with every masked pointer deleted (so the rest compares).
    clone = json.loads(json.dumps(obj))
    for ptr in mask:
        _delete_ptr(clone, _strip_ptr(ptr))
    return clone


def _delete_ptr(obj: object, ptr: str) -> None:
    parts = ptr.lstrip("/").split("/")
    if not parts or parts == [""]:
        return
    *parents, last = parts
    cur = obj
    for raw in parents:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        elif isinstance(cur, list) and re.fullmatch(r"-?\d+", key) and -len(cur) <= int(key) < len(cur):
            cur = cur[int(key)]
        else:
            return
    lk = last.replace("~1", "/").replace("~0", "~")
    if isinstance(cur, dict):
        cur.pop(lk, None)
    elif isinstance(cur, list) and re.fullmatch(r"-?\d+", lk) and -len(cur) <= int(lk) < len(cur):
        cur.pop(int(lk))


def assemble(parts: list[object], projection: str | None) -> object:
    # reduce an ordered SET of response bodies (stream frames / pages) into one artifact.
    # if a projection is given it must point at a list on each part -> concatenated; else parts are returned.
    if projection is None:
        return parts
    out: list[object] = []
    for p in parts:
        seg = json_pointer(p, _strip_ptr(projection))
        if seg is _PTR_SENTINEL:
            continue
        if isinstance(seg, list):
            out.extend(seg)
        else:
            out.append(seg)
    return out


# ---- mask-validity (the FP-3 gate) ----

def check_mask_validity(mask: list[str], golden_runs: list[str]) -> dict[str, object]:
    # a masked field is only legal if it is CONSTANT across the varied-input golden runs. If it varies
    # with input we'd be masking the answer. <2 runs cannot establish validity -> caller treats as unproven.
    if not mask:
        return {"mask_valid": True, "checked": False, "reason": "no mask"}
    if len(golden_runs) < 2:
        raise MaskValidityError("mask-validity needs >=2 varied-input golden runs to confirm masked fields are constant")
    docs = [load_json(p) for p in golden_runs]
    varying: list[str] = []
    for ptr in mask:
        sp = _strip_ptr(ptr)
        vals = [canonical(json_pointer(d, sp)) for d in docs if json_pointer(d, sp) is not _PTR_SENTINEL]
        if len(set(vals)) > 1:
            varying.append(ptr)
    if varying:
        raise MaskValidityError(f"masked field(s) vary with input (illegal to mask): {', '.join(varying)}")
    return {"mask_valid": True, "checked": True, "reason": "all masked fields constant across varied runs"}


# ---- comparators ----

def compare_byte_eq(api: str, golden: str, res: dict[str, object]) -> dict[str, object]:
    # forbid BYTE_EQ on a known-nondeterministic container — its bytes are not reproducible (GEN-8).
    tag = container_tag(api) or container_tag(golden)
    if tag is not None:
        return {**res, "verdict": "INCONCLUSIVE", "method": "bytes",
                "reason": f"BYTE_EQ forbidden on nondeterministic container ({tag}); declare a projection comparator"}
    same = open(api, "rb").read() == open(golden, "rb").read()
    return {**res, "verdict": "MATCH" if same else "MISMATCH", "method": "sha256",
            "reason": "byte-identical" if same else "not byte-identical"}


def compare_canonical_json(api: str, golden: str, res: dict[str, object]) -> dict[str, object]:
    try:
        ca, cg = canonical(load_json(api)), canonical(load_json(golden))
    except (ValueError, UnicodeDecodeError) as e:
        return {**res, "verdict": "INCONCLUSIVE", "method": "canonical-json", "reason": f"not parseable JSON: {e}"}
    same = ca == cg
    return {**res, "verdict": "MATCH" if same else "MISMATCH", "method": "canonical-json",
            "reason": "structurally equal (keys sorted)" if same else "structurally different after canonicalization"}


def compare_normalized(api: str, golden: str, res: dict[str, object], mask: list[str]) -> dict[str, object]:
    try:
        oa, og = load_json(api), load_json(golden)
    except (ValueError, UnicodeDecodeError) as e:
        return {**res, "verdict": "INCONCLUSIVE", "method": "normalized", "reason": f"not parseable JSON: {e}"}
    ca, cg = canonical(apply_mask(oa, mask)), canonical(apply_mask(og, mask))
    same = ca == cg
    return {**res, "verdict": "MATCH" if same else "MISMATCH", "method": "normalized", "mask": mask,
            "reason": "equal after masking volatile fields" if same else "differ in an UNMASKED field"}


def compare_extracted(api: str, golden: str, res: dict[str, object], projection: str) -> dict[str, object]:
    try:
        oa, og = load_json(api), load_json(golden)
    except (ValueError, UnicodeDecodeError) as e:
        return {**res, "verdict": "INCONCLUSIVE", "method": "extracted", "reason": f"not parseable JSON: {e}"}
    sp = _strip_ptr(projection)
    va, vg = json_pointer(oa, sp), json_pointer(og, sp)
    if va is _PTR_SENTINEL or vg is _PTR_SENTINEL:
        return {**res, "verdict": "INCONCLUSIVE", "method": "extracted",
                "reason": f"projection {projection} absent in {'api' if va is _PTR_SENTINEL else 'golden'}"}
    same = canonical(va) == canonical(vg)
    return {**res, "verdict": "MATCH" if same else "MISMATCH", "method": "extracted", "projection": projection,
            "reason": f"projected value {projection} {'equal' if same else 'differs'}"}


def compare_assembled(api: str, golden_set: list[str], res: dict[str, object], projection: str | None) -> dict[str, object]:
    # the golden is the reduction of an ordered SET of response bodies; the api artifact is the single
    # assembled body. Compare the reductions (projected to the payload list when a projection is given).
    try:
        parts = [load_json(p) for p in golden_set]
        api_obj = load_json(api)
    except (ValueError, UnicodeDecodeError) as e:
        return {**res, "verdict": "INCONCLUSIVE", "method": "assembled", "reason": f"not parseable JSON: {e}"}
    golden_assembled = assemble(parts, projection)
    api_assembled = assemble([api_obj], projection) if projection else api_obj
    same = canonical(api_assembled) == canonical(golden_assembled)
    return {**res, "verdict": "MATCH" if same else "MISMATCH", "method": "assembled",
            "projection": projection, "parts": len(golden_set),
            "reason": f"assembled over {len(golden_set)} frames {'equal' if same else 'differs'}"}


def compare_pdf_projection(api: str, golden: str, res: dict[str, object], threshold: float) -> dict[str, object]:
    # the binary-projection default: project both PDFs to their extracted text and jaccard them.
    ta, tg = pdf_text(api), pdf_text(golden)
    if ta is None or tg is None:
        return {**res, "verdict": "INCONCLUSIVE", "method": "none",
                "reason": "no pdf text extractor (install poppler-utils or pypdf); size-only is not proof — "
                "OPEN both PDFs and confirm the same fields by eye before shipping"}
    ov = round(token_jaccard(ta, tg), 3)
    return {**res, "verdict": "MATCH" if ov >= threshold else "MISMATCH", "method": "pdf-text-jaccard",
            "overlap": ov, "threshold": threshold,
            "reason": f"text token overlap {ov} {'>=' if ov >= threshold else '<'} {threshold}"}


def compare(
    api: str,
    golden: str,
    threshold: float,
    comparator: str | None = None,
    mask: list[str] | None = None,
    projection: str | None = None,
    golden_set: list[str] | None = None,
) -> dict[str, object]:
    # comparator=None preserves the legacy auto path: sha256 -> pdf-text-jaccard -> exact bytes.
    mask = mask or []
    ai, gi = file_info(api), file_info(golden)
    res: dict[str, object] = {"api": ai, "golden": gi, "sizeRatio": size_ratio(_nbytes(ai), _nbytes(gi))}

    if comparator == "BYTE_EQ":
        return compare_byte_eq(api, golden, res)
    if comparator == "CANONICAL_JSON_EQ":
        return compare_canonical_json(api, golden, res)
    if comparator == "NORMALIZED":
        return compare_normalized(api, golden, res, mask)
    if comparator == "EXTRACTED":
        if not projection:
            return {**res, "verdict": "INCONCLUSIVE", "method": "extracted", "reason": "EXTRACTED requires --projection"}
        return compare_extracted(api, golden, res, projection)
    if comparator == "ASSEMBLED":
        gs = golden_set if golden_set else [golden]
        ai2 = file_info(api)
        res2: dict[str, object] = {"api": ai2, "golden": {"path": "+".join(gs),
                      "bytes": sum(_nbytes(file_info(p)) for p in gs), "sha256": "", "magic": ""}, "sizeRatio": 1.0}
        return compare_assembled(api, gs, res2, projection)

    # ---- legacy auto path (comparator is None) ----
    if ai["sha256"] == gi["sha256"]:
        return {**res, "verdict": "MATCH", "method": "sha256", "reason": "byte-identical"}

    if is_pdf(api) and is_pdf(golden):
        return compare_pdf_projection(api, golden, res, threshold)

    # a nondeterministic container that ISN'T both-PDF must not silently fall through to a bytes verdict.
    tag = container_tag(api) or container_tag(golden)
    if tag is not None:
        return {**res, "verdict": "INCONCLUSIVE", "method": "none",
                "reason": f"nondeterministic container ({tag}); declare a projection comparator (no BYTE_EQ fallthrough)"}

    # Non-PDF, not byte-identical: generic artifacts must match exactly.
    return {
        **res,
        "verdict": "MISMATCH",
        "method": "bytes",
        "reason": "not byte-identical and not both PDF; generic artifacts must be identical to count as equivalent",
    }


# ---- verify_receipt.json (CONTRACTS §4) ----

def _results(run: dict[str, object]) -> list[dict[str, object]]:
    rs = run.get("results", [])
    return rs if isinstance(rs, list) else []


def build_receipt(
    segment_id: str,
    comparator_kind: str,
    runs: list[dict[str, object]],
    *,
    tag: str | None = None,
    field_mask: list[str] | None = None,
    projection: str | None = None,
    threshold: float = 0.9,
    mask_valid: bool = True,
    coverage: dict[str, object] | None = None,
    api_instance: str = "",
    golden_instance: str = "",
) -> dict[str, object]:
    # one verify_receipt object; verdict reduces over every instance x run.
    all_match = bool(runs) and all(r.get("match") for run in runs for r in _results(run))
    cov = coverage or {}
    coverage_ok = all(bool(cov.get(k, True)) for k in (
        "fresh_not_build_instance", "mutually_isolated", "mask_fields_constant_across_runs",
        "forces_pagination", "perturbs_every_computed",
    ))
    if not mask_valid:
        verdict = "FAILED"
        bail: dict[str, object] | None = {"code": "BAIL-5", "reason": "a masked field varies with input (illegal mask)"}
    elif not all_match:
        verdict = "FAILED"
        bail = {"code": "BAIL-5", "reason": _first_mismatch(runs)}
    elif not coverage_ok:
        verdict = "UNCOVERED"
        bail = None
    else:
        verdict = "PROVEN"
        bail = None
    return {
        "schema": "verify_receipt/v1",
        "segment_id": segment_id,
        "verdict": verdict,
        "comparator": {
            "kind": comparator_kind,
            "frozen": True,
            "tag": tag,
            "field_mask": field_mask or [],
            "projection": projection,
            "threshold": threshold,
            "mask_valid": mask_valid,
        },
        "api_instance": api_instance,
        "golden_instance": golden_instance,
        "runs": runs,
        "coverage": cov,
        "bail": bail,
    }


def _first_mismatch(runs: list[dict[str, object]]) -> str:
    for run in runs:
        inst = run.get("instance", {})
        inst_id = inst.get("id") if isinstance(inst, dict) else None
        for r in _results(run):
            if not r.get("match"):
                cmp = r.get("comparison", {})
                cmp = cmp if isinstance(cmp, dict) else {}
                return f"instance {inst_id} run {r.get('run')}: {cmp.get('verdict')} — {cmp.get('reason')}"
    return "an instance/run diverged"


def write_receipt(path: str, receipt: dict[str, object]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2)
        f.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", help="artifact produced by the API replay (on a FRESH instance)")
    ap.add_argument("--golden", help="artifact produced by the UI on that same fresh instance")
    ap.add_argument("--threshold", type=float, default=0.9, help="min PDF text overlap to count as a match")
    ap.add_argument("--comparator", choices=COMPARATORS, default=None,
                    help="frozen per-type comparator; omit for the legacy sha256->pdf-jaccard->bytes auto path")
    ap.add_argument("--mask", action="append", default=[], help="NORMALIZED masked field (json pointer); repeatable")
    ap.add_argument("--projection", default=None, help="EXTRACTED/ASSEMBLED projection (json pointer)")
    ap.add_argument("--golden-set", nargs="+", default=None, help="ASSEMBLED ordered set of golden frame bodies")
    ap.add_argument("--golden-runs", nargs="+", default=None,
                    help="varied-input golden docs for MASK-VALIDITY (>=2); checked before NORMALIZED compare")
    ap.add_argument("--receipt", default=None, help="write a verify_receipt.json here (single api/golden run)")
    ap.add_argument("--segment-id", default="s0", help="segment id for the receipt")
    args = ap.parse_args()

    needed = list(args.golden_set or [])
    for p in (args.api, args.golden):
        if p is not None:
            needed.append(p)
    for p in needed:
        if not os.path.exists(p):
            sys.exit(f"missing file: {p}")
    if not args.api or (not args.golden and not args.golden_set):
        sys.exit("need --api and (--golden or --golden-set)")

    if args.comparator == "NORMALIZED" and args.golden_runs:
        try:
            check_mask_validity(args.mask, args.golden_runs)
        except MaskValidityError as e:
            print(json.dumps({"verdict": "MISMATCH", "method": "mask-validity", "reason": str(e)}, indent=2))
            return MISMATCH

    res = compare(
        args.api, args.golden or args.api, args.threshold,
        comparator=args.comparator, mask=args.mask, projection=args.projection, golden_set=args.golden_set,
    )
    print(json.dumps(res, indent=2))
    verdict = str(res["verdict"])

    if args.receipt:
        run: dict[str, object] = {"instance": {"id": args.segment_id, "role": "fresh"}, "n": 1,
               "results": [{"run": 1, "api": args.api, "golden": args.golden or "+".join(args.golden_set or []),
                            "match": verdict == "MATCH", "comparison": res}]}
        receipt = build_receipt(
            args.segment_id, args.comparator or "BYTE_EQ", [run],
            field_mask=args.mask, projection=args.projection, threshold=args.threshold,
        )
        write_receipt(args.receipt, receipt)

    return {"MATCH": MATCH, "MISMATCH": MISMATCH, "INCONCLUSIVE": INCONCLUSIVE}[verdict]


if __name__ == "__main__":
    sys.exit(main())
