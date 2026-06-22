#!/usr/bin/env python3
# classify_values.py — S2 SUBSET + S3 CLASSIFY (the heart) + the INV-1/INV-2 gate (DESIGN §3, CONTRACTS §3).
#
# Locates the golden's source response, backward-closes the minimal causal subset R over TRANSITIVE
# request-dependency (not just G), then buckets EVERY value in EVERY request of R into one of
# CONST | INPUT | AMBIENT-INPUT | DERIVED | PRODUCED | COMPUTED | CONTESTED | UNEXPLAINED — using
# co-variation across >=2 varied-input runs plus uniqueness/high-entropy guards. A value matching two
# buckets is CONTESTED (never first-match-resolved). PRODUCED = a DERIVED whose source request is a
# mutation; that mutation must be in R (a missing one surfaces as a dangling PRODUCED — the fingerprint
# of an incomplete capture). Emits plan.json and runs the G1 (self-contained) + G2 (no-fixed-wait) gates.
#
# Exit: 0 = gates pass (verdict API-CANDIDATE) · nonzero = a MISS or a BAIL (verdict KEEP-UI), with a
# clear report on stderr naming what was unexplained / contested / dangling.
#
# Usage:
#   python classify_values.py --runs .o11y/run .o11y/run2 [.o11y/run3 …] \
#       --segment-id s0 [--match <url-substr>] [--plan plan.json]
#
# Reads:  <run>/api-spec/intermediate/paired.jsonl   (the wire of record — never the raw CDP)
#         <run>/segment_inputs.json                  (handoff ref -> concrete captured value, per run)
# Output: plan.json (CONTRACTS §3) + a human report on stderr.

import argparse
import base64
import binascii
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from typing import Any

JsonObj = dict[str, Any]  # a heterogeneous JSON-shaped record (paired row / value / step / plan)

# ---- entropy / cardinality guards (a DERIVED/INPUT edge needs a value that can't be a coincidence) ----
MIN_ENTROPY_LEN = 8       # below this a string is too short to be a unique high-entropy handle
MIN_SHANNON_BITS = 2.5    # per-char Shannon entropy floor — rejects "default", "aaaaaa", small repeats
LOW_CARD_LITERALS = {"true", "false", "none", "null", "default", "all", "asc", "desc", "0", "1", "-1", ""}

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
GENERATOR_HINTS = ("idempot", "nonce", "request_id", "requestid", "uuid", "trace", "_key", "client_id")


# ---- value addressing ------------------------------------------------------------------------------
# Every request value is addressed by an `extractor` string (CONTRACTS §0 conventions): json-ptr / form-key
# / header / path-tmpl / query-key. A carrier = (extractor, value) read out of one request.


def _json_ptr_escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _flatten(obj: object, prefix: str, out: list[tuple[str, object]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}/{_json_ptr_escape(str(k))}", out)
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            _flatten(v, f"{prefix}/{idx}", out)
    else:
        out.append((prefix, obj))


def path_carriers(row: JsonObj) -> list[tuple[str, object]]:
    # A value threaded ONLY through a URL path segment (REST `/jobs/{jobId}`) is a real request value;
    # without this it is never surfaced and a path-threaded PRODUCED under-detects (the Metaview miss on
    # a path-threaded API). A segment is a path-param carrier iff it is high-entropy — the SAME generic
    # guard used everywhere (no id-format assumption): route literals (`jobs`, `api`, `v1`) and short/
    # low-cardinality segments are rejected, opaque handles (`job_7f3a…`) are kept. The param name is the
    # preceding collection segment (REST convention `/jobs/<id>` -> `path-tmpl:jobs`), else its index.
    carriers: list[tuple[str, object]] = []
    raw = row.get("path")
    if not isinstance(raw, str) or not raw:
        return carriers
    segments = [s for s in raw.split("?", 1)[0].split("/") if s]
    for idx, seg in enumerate(segments):
        if is_high_entropy(seg):
            name = segments[idx - 1] if idx > 0 else str(idx)
            carriers.append((f"path-tmpl:{name}", seg))
    return carriers


def request_carriers(row: JsonObj) -> list[tuple[str, object]]:
    # Every scalar leaf the request SENDS: body (json-ptr or form-key), URL path-template segments, query
    # params, and non-auto headers.
    carriers: list[tuple[str, object]] = []
    body = row.get("reqBody")
    if isinstance(body, (dict, list)):
        leaves: list[tuple[str, object]] = []
        _flatten(body, "", leaves)
        for ptr, val in leaves:
            carriers.append((f"json-ptr:{ptr}", val))
    elif isinstance(body, str) and "=" in body and "{" not in body:
        # urlencoded form body — one carrier per key
        for part in body.split("&"):
            if "=" in part:
                k, _, v = part.partition("=")
                carriers.append((f"form-key:{k}", v))
    carriers.extend(path_carriers(row))
    for k, v in (row.get("query") or {}).items():
        carriers.append((f"query-key:{k}", v))
    for k, v in (row.get("reqHeaders") or {}).items():
        carriers.append((f"header:{k.lower()}", v))
    return carriers


def _stable_key(row: JsonObj) -> str:
    # A cross-run-stable identity for a request: method + id-normalized path + GraphQL operationName. Lets us
    # align the SAME logical request across runs whose R differs in length (an extra CSRF GET shifts ranks).
    method = (row.get("method") or "").upper()
    raw = str(row.get("path") or row.get("url") or "")
    norm = "/".join("{}" if is_high_entropy(s) else s for s in raw.split("?", 1)[0].split("/"))
    op = ""
    body = row.get("reqBody")
    if isinstance(body, dict) and isinstance(body.get("operationName"), str):
        op = body["operationName"]
    return f"{method} {norm} {op}"


def _carrier_value(row: JsonObj, extractor: str) -> object | None:
    for ext, val in request_carriers(row):
        if ext == extractor:
            return val
    return None


def response_sources(row: JsonObj) -> list[tuple[str, object]]:
    # Every scalar leaf a response PRODUCES (json-ptr), plus producing-side header values — the pool a
    # DERIVED value must match against.
    sources: list[tuple[str, object]] = []
    body = row.get("respBody")
    if isinstance(body, (dict, list)):
        leaves: list[tuple[str, object]] = []
        _flatten(body, "", leaves)
        for ptr, val in leaves:
            sources.append((f"json-ptr:{ptr}", val))
    for k, v in (row.get("respHeaders") or {}).items():
        sources.append((f"header:{k.lower()}", v))
    return sources


# ---- entropy / uniqueness --------------------------------------------------------------------------


def shannon_bits(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def is_high_entropy(val: object) -> bool:
    # A value is "high entropy" iff it's a non-low-cardinality string long+varied enough to be a unique
    # handle. Small ints / bools / sentinels can never anchor a DERIVED/INPUT edge (DESIGN CLASS-8).
    if not isinstance(val, str):
        return False
    s = val.strip()
    if s.lower() in LOW_CARD_LITERALS or len(s) < MIN_ENTROPY_LEN:
        return False
    return shannon_bits(s) >= MIN_SHANNON_BITS


def canon(val: object) -> str:
    # canonical form for cross-run equality (DESIGN GEN-3): order-independent for dict/list.
    return json.dumps(val, sort_keys=True, separators=(",", ":"))


# ---- run model -------------------------------------------------------------------------------------


class Run:
    # One varied-input capture: its ordered exchanges + its input bindings/ambient identity.
    def __init__(self, run_dir: str) -> None:
        self.run_dir = run_dir
        self.rows = _load_paired(run_dir)
        inputs = _load_inputs(run_dir)
        self.label: str = (inputs.get("input_identity") or {}).get("label") or run_dir
        self.ambient: dict[str, object] = (inputs.get("input_identity") or {}).get("ambient") or {}
        self.bindings: list[JsonObj] = inputs.get("bindings") or []
        self.golden: JsonObj = inputs.get("golden") or {}
        # input value -> the ref it was supplied as (only STEP_INPUT/PRIOR_UI count as a segment input)
        self.input_values: dict[str, str] = {}
        for b in self.bindings:
            if b.get("origin") in ("STEP_INPUT", "PRIOR_UI") and b.get("value") is not None:
                self.input_values[canon(b["value"])] = b["ref"]


def _load_paired(run_dir: str) -> list[JsonObj]:
    path = os.path.join(run_dir, "api-spec", "intermediate", "paired.jsonl")
    if not os.path.exists(path):
        sys.exit(f"no paired trace at {path}; run analyze.py first")
    rows: list[JsonObj] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _load_inputs(run_dir: str) -> JsonObj:
    path = os.path.join(run_dir, "segment_inputs.json")
    if not os.path.exists(path):
        sys.exit(f"no segment_inputs.json at {path}; capture must emit it")
    with open(path) as f:
        data: JsonObj = json.load(f)
    return data


def is_mutation(row: JsonObj) -> bool:
    # method case-insensitive (some capture tools emit "post"); GraphQL detection ANCHORED — a read named
    # "query GetMutationStatus {...}" merely CONTAINS "mutation" and must not be flagged a write.
    if (row.get("method") or "").upper() in MUTATING_METHODS:
        body = row.get("reqBody")
        if isinstance(body, dict) and isinstance(body.get("query"), str):
            return re.match(r"\s*mutation\b", body["query"], re.IGNORECASE) is not None
        return True
    return False


# ---- S2 SUBSET: golden source + transitive request-dependency closure ------------------------------


# A binary artifact is identified by its leading MAGIC bytes — its IDENTITY, independent of the envelope it
# is delivered in (raw body, base64-in-JSON, …). New container types add one row; new ENVELOPES add a
# recovery strategy in _artifact_extractor (see the extension note there).
_MAGIC_FOR_TAG: dict[str, bytes] = {
    "pdf": b"%PDF-", "zip": b"PK\x03\x04", "xlsx": b"PK\x03\x04", "docx": b"PK\x03\x04", "pptx": b"PK\x03\x04",
    "png": b"\x89PNG", "jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff", "gif": b"GIF8", "gz": b"\x1f\x8b",
}
_B64ISH = re.compile(r"[A-Za-z0-9+/_=-]+\Z")


def find_golden_source(run: Run) -> JsonObj:
    # Locate the response that CARRIES the golden artifact and HOW (the extractor recipe). We match the
    # artifact's IDENTITY (type-magic / sha256), not just a content-type envelope — so a binary delivered
    # base64-in-JSON is found, not wrongly declared client-rendered. found:false => BAIL-1.
    golden = run.golden
    tag = golden.get("tag")
    magic = _MAGIC_FOR_TAG.get(tag) if isinstance(tag, str) else None
    raw_sha = golden.get("sha256")
    gsha = raw_sha if isinstance(raw_sha, str) and len(raw_sha) >= 16 else None
    hits: list[tuple[int, str]] = []  # (exchange seq, extractor recipe)
    for i, row in enumerate(run.rows):
        ext = _artifact_extractor(row, tag, magic, gsha)
        if ext is not None:
            hits.append((i, ext))
    if not hits:
        return {"found": False, "mode": "single", "exchange_seqs": [], "extractor": None, "comparator_hint": None}
    seqs = _resolve_golden_seqs(run, golden, [i for i, _ in hits])
    extractor = next((e for i, e in reversed(hits) if i in seqs), "whole-payload")  # the chosen (terminal) source's recipe
    mode = "assembled" if len(seqs) > 1 else "single"
    return {
        "found": True,
        "mode": mode,
        "exchange_seqs": seqs,
        "extractor": extractor,
        "comparator_hint": "assembled" if mode == "assembled" else "binary-projection",
    }


def _artifact_extractor(row: JsonObj, tag: object, magic: bytes | None, gsha: str | None) -> str | None:
    # Does this response carry the golden artifact, and by what recovery recipe? Strategies, in order:
    #   1) RAW    — the body's own content-type IS the artifact's type (typed download / pre-signed-URL GET).
    #   2) BASE64 — a binary artifact base64-encoded inside a structured (JSON) body; matched by decoded magic.
    # EXTENSION POINTS (add a strategy here, never a per-app branch): URL-behind-JSON (a field holding a link
    # whose later GET carries the bytes) and gzip/deflate-wrapped bodies.
    ctype = ((row.get("respHeaders") or {}).get("content-type", "") or row.get("contentType", "") or "").lower()
    if isinstance(tag, str) and tag and (tag in ctype or _ctype_matches_tag(ctype, tag)):
        return "whole-payload"
    if magic is not None:
        body = row.get("respBody")
        if isinstance(body, (dict, list)):
            leaves: list[tuple[str, object]] = []
            _flatten(body, "", leaves)
            for ptr, val in leaves:
                dec = _b64_artifact(val) if isinstance(val, str) else None
                if dec is not None and (dec.startswith(magic) or (gsha is not None and _sha16(dec) == gsha)):
                    return f"json-ptr:{ptr}|base64"
    return None


def _b64_artifact(val: str) -> bytes | None:
    # Decode a string IFF it plausibly carries a base64 artifact (long, base64 charset). The magic check at the
    # call site is the real filter; this just recovers candidate bytes (standard or URL-safe alphabet).
    s = "".join(val.split())
    if len(s) < 64 or not _B64ISH.match(s):
        return None
    padded = s + "=" * (-len(s) % 4)
    decoder = base64.urlsafe_b64decode if ("-" in s or "_" in s) else base64.b64decode
    try:
        return decoder(padded)
    except (ValueError, binascii.Error):
        return None


def _sha16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def _resolve_golden_seqs(run: Run, golden: JsonObj, matches: list[int]) -> list[int]:
    # A common content-type (e.g. json) matches MANY responses — multiplicity is NOT evidence of an assembled
    # (streamed/paginated) golden. Pin to the producing exchange via produces_ref when the trace carries it,
    # else take the TERMINAL match (the final export is the source in the common case). A single terminal
    # source errs toward keep-UI; auto-'assembled' over-selects R into a false PROVEN. (sha256/byte matching is
    # unavailable here — the trace stores parsed bodies, not the raw artifact.)
    if len(matches) == 1:
        return matches
    ref = golden.get("produces_ref")
    if ref:
        pinned = [i for i in matches if run.rows[i].get("produces_ref") == ref or run.rows[i].get("ref") == ref]
        if pinned:
            return pinned
    return [matches[-1]]


def _ctype_matches_tag(ct: str, tag: str) -> bool:
    table = {"pdf": "pdf", "zip": "zip", "csv": "csv", "png": "png", "jpg": "jpeg", "json": "json"}
    return table.get(tag, tag) in ct


def transitive_subset(run: Run, golden_seqs: list[int]) -> list[int]:
    # Seed R with the golden-producing exchange(s); then backward-close: any earlier call whose response
    # produces a high-entropy value that some in-R request CONSUMES is pulled in (keeps the CSRF-token GET
    # etc. — DESIGN FN-2). A call is noise only if no in-R request derives a field from it.
    in_r: set[int] = set(golden_seqs)
    changed = True
    while changed:
        changed = False
        # the pool of high-entropy values each earlier response could supply
        for cons_seq in sorted(in_r):
            needs = [v for _, v in request_carriers(run.rows[cons_seq]) if is_high_entropy(v)]
            need_canon = {canon(v) for v in needs}
            for src_seq in range(cons_seq):
                if src_seq in in_r:
                    continue
                produced = {canon(v) for _, v in response_sources(run.rows[src_seq]) if is_high_entropy(v)}
                if need_canon & produced:
                    in_r.add(src_seq)
                    changed = True
    return sorted(in_r)


# ---- S3 CLASSIFY: every value in every request of R, all-matching-buckets --------------------------


class Classifier:
    def __init__(self, runs: list[Run], r_seqs: list[int]) -> None:
        self.runs = runs
        self.primary = runs[0]
        self.r_seqs = r_seqs
        self.node_of_seq = {seq: f"n{rank}" for rank, seq in enumerate(r_seqs)}
        # per-run: does this run produce the same R shape? we align by seq-rank within R.
        self._varied = self._inputs_vary()

    def _inputs_vary(self) -> bool:
        labels = {r.label for r in self.runs}
        return len(labels) >= 2

    def _seq_by_key(self, run: Run, key: str) -> int | None:
        for seq in transitive_subset(run, find_golden_source(run)["exchange_seqs"]):
            if _stable_key(run.rows[seq]) == key:
                return seq
        return None

    def _value_across_runs(self, rank: int, extractor: str) -> list[object | None]:
        # Align by a STABLE request key, NOT R-rank: each run's R is recomputed independently, so an
        # extra/missing call (e.g. a CSRF GET) shifts ranks and would compare unrelated requests.
        key = _stable_key(self.primary.rows[self.r_seqs[rank]])
        out: list[object | None] = []
        for run in self.runs:
            seq = self._seq_by_key(run, key)
            out.append(_carrier_value(run.rows[seq], extractor) if seq is not None else None)
        return out

    def _source_in_responses(self, run: Run, r_seqs: list[int], up_to_rank: int, val: object) -> tuple[int, str] | None:
        # find an EARLIER in-R response exposing this exact (high-entropy) value -> a DERIVED edge.
        cval = canon(val)
        for rank in range(up_to_rank):
            for ext, sval in response_sources(run.rows[r_seqs[rank]]):
                if canon(sval) == cval and is_high_entropy(sval):
                    return rank, ext
        return None

    def classify_value(self, rank: int, extractor: str, val: object) -> JsonObj:
        # Compute ALL matching buckets (DESIGN CLASS-1 — two matches = CONTESTED, never first-match).
        node = self.node_of_seq[self.r_seqs[rank]]
        across = self._value_across_runs(rank, extractor)
        present = [v for v in across if v is not None]
        runs_confirmed = len(self.runs)
        differs = len({canon(v) for v in present}) > 1 if present else False
        stable = (not differs) and len(present) == runs_confirmed
        high_entropy = is_high_entropy(val)

        matches: list[str] = []
        info: JsonObj = {"node": node, "carrier": extractor}

        # INPUT — equals a declared segment input in EVERY run, co-varying across the >=2 varied inputs.
        # NOT entropy-gated: the evidence is co-variation with a KNOWN declared input (entropy guards DERIVED
        # against coincidental response matches, but a short id/page-number/enum that tracks the input IS the
        # input — e.g. invoiceId 42 -> 97). Without this, every low-entropy input is a false UNEXPLAINED.
        input_ref = self._input_match(val)
        co_varies = self._co_varies_with_input(rank, extractor, across)
        if input_ref and co_varies and runs_confirmed >= 2:
            matches.append("INPUT")
            info["_input"] = {"ref": input_ref, "co_varies": True}

        # DERIVED / PRODUCED — equals a unique high-entropy value in an earlier in-R response, co-varying.
        src = self._source_in_responses(self.primary, self.r_seqs, rank, val)
        if src and high_entropy and self._derived_confirmed(rank, extractor, src):
            src_rank, src_ext = src
            src_seq = self.r_seqs[src_rank]
            src_mut = is_mutation(self.primary.rows[src_seq])
            info["_derived"] = {
                "src_node": self.node_of_seq[src_seq],
                "src_path": src_ext,
                "src_is_mutation": src_mut,
                "co_varies": co_varies,
            }
            matches.append("PRODUCED" if src_mut else "DERIVED")

        # AMBIENT-INPUT — otherwise-CONST but also present in auth/session (tenant/org) ambient context.
        amb_path = self._ambient_match(val)
        if amb_path and stable and not input_ref:
            info["_ambient"] = {"path": amb_path}
            matches.append("AMBIENT-INPUT")

        # CONST — identical across runs that vary input AND ambient identity (and not an ambient value).
        if stable and not amb_path and "INPUT" not in matches and "DERIVED" not in matches and "PRODUCED" not in matches:
            info["_const"] = {"value": val}
            matches.append("CONST")

        # COMPUTED (generator) — a high-entropy value that differs every run, matches no input/response,
        # on a carrier whose name hints a minted nonce/idempotency key (DESIGN FN-1, CLASS-6).
        if high_entropy and differs and not input_ref and not src and self._looks_generated(extractor):
            info["_computed"] = {"recipe": {"kind": "generator", "fn": "uuid_v4", "args": []}, "differs_across_runs": True}
            matches.append("COMPUTED")

        return self._finalize(rank, extractor, val, matches, info, across)

    def _finalize(self, rank: int, extractor: str, val: object, matches: list[str], info: JsonObj, across: list[object | None]) -> JsonObj:
        node, carrier = info["node"], info["carrier"]
        runs_confirmed = len(self.runs)
        unique = is_high_entropy(val)

        if len(matches) >= 2:
            return {
                "node": node, "carrier": carrier, "bucket": "CONTESTED",
                "all_matching_buckets": matches,
                "reason": f"value matched multiple buckets {matches} — cannot disambiguate",
            }
        if not matches:
            return {
                "node": node, "carrier": carrier, "bucket": "UNEXPLAINED",
                "all_matching_buckets": [],
                "evidence": {"unique": unique, "entropy": "high" if unique else "low", "runs_confirmed": runs_confirmed},
                "reason": "matched no bucket — the fingerprint of a missed call or an unreproducible value",
            }

        bucket = matches[0]
        out: JsonObj = {"node": node, "carrier": carrier, "bucket": bucket, "all_matching_buckets": matches}
        if bucket == "INPUT":
            ref = info["_input"]["ref"]
            out["ref"] = ref
            out["source"] = {"kind": "step_input", "ref": ref}
            out["binds_as"] = f"INPUT({ref})"
            out["evidence"] = {"co_varies_with_input": True, "unique": True, "entropy": "high", "runs_confirmed": runs_confirmed}
        elif bucket in ("DERIVED", "PRODUCED"):
            d = info["_derived"]
            out["source"] = {"kind": "response", "src_node": d["src_node"], "src_path": d["src_path"]}
            out["binds_as"] = f"DERIVED({d['src_node']}, {d['src_path']})"
            ev = {"co_varies_with_input": d["co_varies"], "unique": True, "entropy": "high", "runs_confirmed": runs_confirmed}
            if bucket == "PRODUCED":
                ev["src_is_mutation"] = True
            out["evidence"] = ev
        elif bucket == "AMBIENT-INPUT":
            out["source"] = {"kind": "ambient", "path": info["_ambient"]["path"]}
            out["binds_as"] = f"AMBIENT-INPUT({info['_ambient']['path']})"
            out["evidence"] = {"stable_across_runs": True, "from_ambient": True, "runs_confirmed": runs_confirmed}
        elif bucket == "CONST":
            out["value"] = info["_const"]["value"]
            out["binds_as"] = f"CONST({canon(info['_const']['value'])})"
            out["evidence"] = {"stable_across_runs": True, "stable_across_ambient": True}
        elif bucket == "COMPUTED":
            out["recipe"] = info["_computed"]["recipe"]
            out["binds_as"] = f"COMPUTED({info['_computed']['recipe']['fn']})"
            out["evidence"] = {"co_varies_with_input": False, "unique": True, "entropy": "high", "differs_across_runs": True}
        return out

    # ---- per-bucket evidence helpers ----

    def _input_match(self, val: object) -> str | None:
        # equals a segment input in EVERY run (so its ref is the input it tracks).
        cval = canon(val)
        ref = self.primary.input_values.get(cval)
        return ref

    def _co_varies_with_input(self, rank: int, extractor: str, across: list[object | None]) -> bool:
        # the value must change BETWEEN runs exactly as the input changes — a constant can't be INPUT,
        # a value that changes when the input didn't isn't tracking the input (DESIGN CLASS-3).
        if len(self.runs) < 2:
            return False
        pairs: list[tuple[str, str]] = []
        for run, v in zip(self.runs, across):
            if v is None:
                return False
            # this run's input identity, by the ref the value matches
            ref = self.primary.input_values.get(canon(across[0])) if across else None
            in_val = next((b.get("value") for b in run.bindings if b.get("ref") == ref), None)
            pairs.append((canon(in_val), canon(v)))
        # co-variation: input differs => value differs, input same => value same
        in_distinct = len({p[0] for p in pairs})
        val_distinct = len({p[1] for p in pairs})
        return in_distinct >= 2 and in_distinct == val_distinct

    def _derived_confirmed(self, rank: int, extractor: str, src: tuple[int, str]) -> bool:
        # the same response->request edge holds in EVERY run (same src rank exposes the request's value).
        src_rank, src_ext = src
        # Align consumer AND source by stable key in every run (R is recomputed per run; positional ranks
        # drift when a run has an extra/missing call).
        cons_key = _stable_key(self.primary.rows[self.r_seqs[rank]])
        src_key = _stable_key(self.primary.rows[self.r_seqs[src_rank]])
        for run in self.runs:
            cons_seq = self._seq_by_key(run, cons_key)
            src_seq = self._seq_by_key(run, src_key)
            if cons_seq is None or src_seq is None:
                return False
            req_val = _carrier_value(run.rows[cons_seq], extractor)
            if req_val is None:
                return False
            resp_vals = {canon(v) for e, v in response_sources(run.rows[src_seq]) if e == src_ext}
            if canon(req_val) not in resp_vals:
                return False
        return True

    def _ambient_match(self, val: object) -> str | None:
        # value appears in the run's ambient (auth/session) identity -> AMBIENT-INPUT, threaded not hardcoded.
        cval = canon(val)
        for k, v in (self.primary.ambient or {}).items():
            if canon(v) == cval:
                return f"ambient:{k}"
        return None

    def _looks_generated(self, extractor: str) -> bool:
        low = extractor.lower()
        return any(h in low for h in GENERATOR_HINTS)


# ---- control flow (S4): POLL / REPEAT / RETRY from the trace, zero fixed sleeps --------------------


def build_control_flow(run: Run, r_seqs: list[int], node_of_seq: dict[int, str]) -> JsonObj:
    polls: list[JsonObj] = []
    repeats: list[JsonObj] = []
    retries: list[JsonObj] = []
    # POLL: an in-R read repeated >=2x against the same locator with a body status field -> readiness gap.
    locator_counts: Counter[str] = Counter()
    for seq in r_seqs:
        locator_counts[_locator(run.rows[seq])] += 1
    seen: set[str] = set()
    for seq in r_seqs:
        row = run.rows[seq]
        loc = _locator(row)
        if locator_counts[loc] >= 2 and not is_mutation(row) and loc not in seen:
            status_path = _status_field(row)
            if status_path:
                # Read the ready value from the TERMINAL occurrence (COMPLETE), NOT the first (RUNNING) — else
                # the generated poll is satisfied immediately and fetches the artifact prematurely.
                terminal_row = _last_row_for_locator(run, r_seqs, loc)
                polls.append({
                    "read": node_of_seq[seq],
                    "predicate": {"over": "body-field", "path": status_path,
                                  "equals": _terminal_status(terminal_row, status_path),
                                  "timeout_s": 60, "interval_s": 2},
                })
                seen.add(loc)
    # REPEAT: a response carrying a continuation signal (cursor/next/has_more) fed back -> pagination loop.
    for seq in r_seqs:
        cont = _continuation_signal(run.rows[seq])
        if cont:
            repeats.append({"node": node_of_seq[seq], "until_predicate": cont, "accumulate": "items"})
    return {"polls": polls, "repeats": repeats, "retries": retries}


def _locator(row: JsonObj) -> str:
    op = (row.get("reqBody") or {}).get("operationName") if isinstance(row.get("reqBody"), dict) else None
    base = (row.get("origin") or "") + (row.get("path") or "")
    return f"{base}[{op}]" if op else base


def _last_row_for_locator(run: Run, r_seqs: list[int], loc: str) -> JsonObj:
    for seq in reversed(r_seqs):
        if _locator(run.rows[seq]) == loc:
            return run.rows[seq]
    return run.rows[r_seqs[0]]


def _status_field(row: JsonObj) -> str | None:
    body = row.get("respBody")
    if isinstance(body, dict):
        leaves: list[tuple[str, object]] = []
        _flatten(body, "", leaves)
        for ptr, val in leaves:
            if ptr.lower().endswith(("/status", "/state", "/phase")) and isinstance(val, str):
                return f"json-ptr:{ptr}"
    return None


def _terminal_status(row: JsonObj, status_path: str) -> str:
    ptr = status_path.split(":", 1)[1]
    body = row.get("respBody")
    leaves: list[tuple[str, object]] = []
    if isinstance(body, dict):
        _flatten(body, "", leaves)
    for p, val in leaves:
        if p == ptr:
            return str(val)
    return "COMPLETE"


def _continuation_signal(row: JsonObj) -> JsonObj | None:
    body = row.get("respBody")
    if not isinstance(body, dict):
        return None
    leaves: list[tuple[str, object]] = []
    _flatten(body, "", leaves)
    for ptr, val in leaves:
        low = ptr.lower()
        if low.endswith(("/next_cursor", "/nextcursor", "/cursor", "/next")) and val not in (None, "", False):
            return {"over": "body-field", "path": f"json-ptr:{ptr}", "present": True}
        if low.endswith("/has_more") and val is True:
            return {"over": "body-field", "path": f"json-ptr:{ptr}", "equals": False}
    return None


def build_steps(r_seqs: list[int], node_of_seq: dict[int, str], values: list[JsonObj], control_flow: JsonObj) -> list[JsonObj]:
    poll_reads = {p["read"] for p in control_flow["polls"]}
    poll_by_read = {p["read"]: p for p in control_flow["polls"]}
    steps: list[JsonObj] = []
    for seq in r_seqs:
        node = node_of_seq[seq]
        if node in poll_reads:
            steps.append({"op": "POLL", "read": node, "predicate": poll_by_read[node]["predicate"]})
            continue
        # COMPUTE any minted value this node needs, then BIND any DERIVED/PRODUCED input, then ISSUE.
        for v in values:
            if v["node"] == node and v["bucket"] == "COMPUTED":
                steps.append({"op": "COMPUTE", "ref": f"r_{node}_{_short(v['carrier'])}", "recipe": v["recipe"]})
        for v in values:
            if v["node"] == node and v["bucket"] in ("DERIVED", "PRODUCED"):
                s = v["source"]
                steps.append({"op": "BIND", "ref": f"r_{node}_{_short(v['carrier'])}", "src": s["src_node"], "path": s["src_path"]})
        steps.append({"op": "ISSUE", "node": node})
    steps.append({"op": "ASSERT", "predicate": {"over": "status-code", "equals": 200}})
    return steps


def _short(carrier: str) -> str:
    return carrier.rsplit("/", 1)[-1].rsplit(":", 1)[-1] or "v"


# ---- assembly + gates ------------------------------------------------------------------------------


def classify_all(runs: list[Run], match: str | None) -> tuple[list[JsonObj], list[int], JsonObj, dict[int, str]]:
    primary = runs[0]
    gs = find_golden_source(primary)
    r_seqs = transitive_subset(primary, gs["exchange_seqs"]) if gs["found"] else []
    node_of_seq = {seq: f"n{rank}" for rank, seq in enumerate(r_seqs)}
    clf = Classifier(runs, r_seqs)
    values: list[JsonObj] = []
    for rank, seq in enumerate(r_seqs):
        row = primary.rows[seq]
        if match and match not in _locator(row):
            # still classify everything in R; --match only narrows the human report, not R.
            pass
        for ext, val in request_carriers(row):
            if _is_auto_header(ext):
                continue
            values.append(clf.classify_value(rank, ext, val))
    return values, r_seqs, gs, node_of_seq


def _is_auto_header(extractor: str) -> bool:
    # browser-set headers carry no classifiable value (cookie/ua/accept/etc.); skip them so they don't
    # masquerade as UNEXPLAINED. Auth headers are handled by S5 (probe_auth), not here.
    if not extractor.startswith("header:"):
        return False
    name = extractor.split(":", 1)[1]
    auto = {"host", "connection", "content-length", "content-type", "origin", "referer", "cookie",
            "user-agent", "accept", "accept-encoding", "accept-language", "authorization", "x-api-key",
            "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest", "sec-fetch-mode",
            "sec-fetch-site", "pragma", "cache-control", "dnt", "te"}
    return name in auto


def collect_misses(values: list[JsonObj], r_seqs: list[int], node_of_seq: dict[int, str]) -> tuple[list[JsonObj], list[JsonObj], list[JsonObj]]:
    unexplained: list[JsonObj] = []
    contested: list[JsonObj] = []
    dangling: list[JsonObj] = []
    in_r_nodes = set(node_of_seq.values())
    for v in values:
        if v["bucket"] == "UNEXPLAINED":
            unexplained.append({"node": v["node"], "carrier": v["carrier"], "reason": v.get("reason", "no bucket")})
        elif v["bucket"] == "CONTESTED":
            contested.append({"node": v["node"], "carrier": v["carrier"],
                              "all_matching_buckets": v["all_matching_buckets"], "reason": v.get("reason", "two buckets")})
        elif v["bucket"] == "PRODUCED":
            src_node = v["source"]["src_node"]
            # a PRODUCED whose mutation source is NOT in R = an incomplete capture (the Metaview miss).
            if src_node not in in_r_nodes:
                dangling.append({"node": v["node"], "carrier": v["carrier"], "src_node": src_node,
                                 "reason": "PRODUCED source mutation is not in R — capture missed the producing call"})
    return unexplained, contested, dangling


def build_plan(runs: list[Run], segment_id: str, match: str | None) -> JsonObj:
    primary = runs[0]
    values, r_seqs, gs, node_of_seq = classify_all(runs, match)

    if not gs["found"]:
        return {
            "schema": "plan/v1", "segment_id": segment_id, "runs": [r.run_dir for r in runs],
            "golden_source": gs, "subset_R": [], "values": [], "steps": [],
            "control_flow": {"polls": [], "repeats": [], "retries": []},
            "unexplained": [], "contested": [], "dangling_produced": [],
            "gate": {"G1_self_contained": {"pass": False, "reason": "golden in no response — client-rendered"},
                     "G2_no_fixed_wait": {"pass": False, "reason": "no R to check"}},
            "verdict": "KEEP-UI",
            "bail": {"code": "BAIL-1", "reason": "golden appears in no response nor any ordered assembly"},
        }

    control_flow = build_control_flow(primary, r_seqs, node_of_seq)
    steps = build_steps(r_seqs, node_of_seq, values, control_flow)
    unexplained, contested, dangling = collect_misses(values, r_seqs, node_of_seq)

    subset_r = []
    for rank, seq in enumerate(r_seqs):
        row = primary.rows[seq]
        op = (row.get("reqBody") or {}).get("operationName") if isinstance(row.get("reqBody"), dict) else None
        subset_r.append({
            "node": node_of_seq[seq], "seq": seq, "exchange_ref": row.get("requestId"),
            "request": {"method": row.get("method"), "locator": (row.get("origin") or "") + (row.get("path") or ""),
                        "operation": op},
            "is_mutation": is_mutation(row),
            "in_R_reason": "produces the golden (terminal)" if seq in gs["exchange_seqs"]
            else "produces a value an in-R request consumes",
        })

    g1_pass = not unexplained and not contested and not dangling
    g2_pass = _g2_check(control_flow, primary, r_seqs)
    bail = None
    if not g1_pass:
        # the unresolvable-MISS bail (BAIL-2). A bounded code cross-check is the operator's next move; the
        # gate reports the miss either way.
        bail = {"code": "BAIL-2", "reason": _miss_reason(unexplained, contested, dangling)}
    elif not g2_pass:
        bail = {"code": "BAIL-3", "reason": "an async gap has no pollable observation"}

    return {
        "schema": "plan/v1", "segment_id": segment_id, "runs": [r.run_dir for r in runs],
        "signature": _signature(primary, gs),
        "golden_source": gs,
        "subset_R": subset_r,
        "values": values,
        "steps": steps,
        "control_flow": control_flow,
        "unexplained": unexplained,
        "contested": contested,
        "dangling_produced": dangling,
        "gate": {
            "G1_self_contained": {"pass": g1_pass,
                                  "reason": "all values bucketed; no unexplained/contested/dangling" if g1_pass
                                  else _miss_reason(unexplained, contested, dangling)},
            "G2_no_fixed_wait": {"pass": g2_pass,
                                 "reason": "every async gap has a POLL; zero fixed sleeps" if g2_pass
                                 else "an async gap (repeated status read) lacks a POLL"},
        },
        "verdict": "API-CANDIDATE" if (g1_pass and g2_pass) else "KEEP-UI",
        "bail": bail,
    }


def _signature(run: Run, gs: JsonObj) -> JsonObj:
    inputs = [{"ref": b["ref"], "shape": b.get("shape")} for b in run.bindings
              if b.get("origin") in ("STEP_INPUT", "PRIOR_UI")]
    golden = run.golden
    output = {"ref": golden.get("produces_ref"), "shape": {"type": "binary", "tag": golden.get("tag")}} if golden else None
    return {"inputs": inputs, "output": output}


def _g2_check(control_flow: JsonObj, run: Run, r_seqs: list[int]) -> bool:
    # every repeated-status-read gap must have a POLL; a paginating response must have a REPEAT.
    locator_counts: Counter[str] = Counter()
    for seq in r_seqs:
        locator_counts[_locator(run.rows[seq])] += 1
    needed_polls = {loc for loc, n in locator_counts.items() if n >= 2}
    have_polls = {_locator(run.rows[seq]) for seq in r_seqs
                  for p in control_flow["polls"] if p["read"] == f"n{r_seqs.index(seq)}"}
    # EVERY repeated-read gap needs a POLL — subset, not intersection (intersection passes when only ONE of
    # several async gaps is covered, shipping a chain that races on the uncovered one).
    if not needed_polls.issubset(have_polls):
        return False
    for seq in r_seqs:
        if _continuation_signal(run.rows[seq]) and not control_flow["repeats"]:
            return False
    return True


def _miss_reason(unexplained: list[JsonObj], contested: list[JsonObj], dangling: list[JsonObj]) -> str:
    parts: list[str] = []
    if unexplained:
        parts.append(f"{len(unexplained)} UNEXPLAINED ({', '.join(u['carrier'] for u in unexplained)})")
    if contested:
        parts.append(f"{len(contested)} CONTESTED ({', '.join(c['carrier'] for c in contested)})")
    if dangling:
        parts.append(f"{len(dangling)} dangling PRODUCED ({', '.join(d['carrier'] for d in dangling)})")
    return "G1 FAIL: " + "; ".join(parts)


def report(plan: JsonObj) -> None:
    g1 = plan["gate"]["G1_self_contained"]
    g2 = plan["gate"]["G2_no_fixed_wait"]
    print(f"segment {plan['segment_id']}: |R|={len(plan['subset_R'])} values={len(plan['values'])} "
          f"verdict={plan['verdict']}", file=sys.stderr)
    print(f"  G1 self-contained: {'PASS' if g1['pass'] else 'FAIL'} — {g1['reason']}", file=sys.stderr)
    print(f"  G2 no-fixed-wait:  {'PASS' if g2['pass'] else 'FAIL'} — {g2['reason']}", file=sys.stderr)
    for u in plan["unexplained"]:
        print(f"  UNEXPLAINED {u['node']} {u['carrier']}: {u['reason']}", file=sys.stderr)
    for c in plan["contested"]:
        print(f"  CONTESTED {c['node']} {c['carrier']}: {c['all_matching_buckets']}", file=sys.stderr)
    for d in plan["dangling_produced"]:
        print(f"  DANGLING-PRODUCED {d['node']} {d['carrier']}: src {d['src_node']} not in R", file=sys.stderr)
    if plan.get("bail"):
        print(f"  BAIL {plan['bail']['code']}: {plan['bail']['reason']}", file=sys.stderr)


def gate_passes(plan: JsonObj) -> bool:
    return (plan["verdict"] == "API-CANDIDATE"
            and not plan["unexplained"] and not plan["contested"] and not plan["dangling_produced"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="classify_values")
    ap.add_argument("--runs", nargs="+", required=True, help=">=2 varied-input run dirs (.o11y/run .o11y/run2 …)")
    ap.add_argument("--segment-id", default="s0")
    ap.add_argument("--match", default=None, help="narrow the human report to a url substring")
    ap.add_argument("--plan", default="plan.json", help="where to write plan.json")
    args = ap.parse_args(argv)

    if len(args.runs) < 2:
        sys.exit("classify needs >=2 varied-input runs to separate CONST/INPUT/COMPUTED")

    runs = [Run(d) for d in args.runs]
    if len({r.label for r in runs}) < 2:
        sys.exit("runs do not vary: input_identity.label is identical — capture differing inputs")

    plan = build_plan(runs, args.segment_id, args.match)
    with open(args.plan, "w") as f:
        json.dump(plan, f, indent=2)
    report(plan)
    return 0 if gate_passes(plan) else 1


if __name__ == "__main__":
    sys.exit(main())
