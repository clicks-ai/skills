#!/usr/bin/env python3
# analyze.py — run the vendored engine's ANALYSIS stages only (load -> filter -> normalize -> infer)
# and surface candidate endpoints for the agent to turn into an in-page fetch step.
#
# The engine's `emit` stage is PURE RENDERING (openapi/client/report/html). We never run it, so none
# of those files are generated. All the analysis we need (request/response pairing, parameter
# identification, schema inference, secret redaction) is done by load..infer and lands in
# `endpoints.with-schemas.jsonl`.
#
# Beyond the per-endpoint candidate view, we also derive — straight from the capture-ordered wire
# (`paired.jsonl`, the wire of record) — the structural facts S2 needs: an ORDERED call list, 1:N
# response grouping (a request may yield many response frames — never truncate to the first), the
# value sites in each request keyed by a pluggable extractor (header / path-tmpl / multipart-part /
# whole-payload for opaque bodies — un-introspectable is NOT auto-UNEXPLAINED), an async/poll signal
# when a status read repeats or a 202/404->200 transition appears, and an artifactOrigin flag (does
# the golden MIME surface as ANY response content-type? if not -> client-rendered hint).
#
# Usage:
#   python analyze.py --run .o11y/<run> [--match <url-substr>] [--top N] [--no-engine]
#                     [--golden-tag pdf] [--golden-mime application/pdf]
#
# Reads:  <run>/api-spec/intermediate/endpoints.with-schemas.jsonl   (engine infer output)
#         <run>/api-spec/intermediate/paired.jsonl                   (capture-ordered wire of record)
#         <run>/api-spec/samples/<method>__<hash>.json               (redacted concrete example)
# Output: compact JSON of candidate endpoints + the ordered structural analysis -> stdout (the agent
#         picks the one matching the demonstrated action and writes the in-page fetch from it).

import argparse
import json
import os
import re
import subprocess
import sys

ENGINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_engine", "discover.mjs")
ANALYSIS_STAGES = ["load", "filter", "normalize", "infer"]  # deliberately NOT "emit"

# Headers the browser sets automatically on an in-page fetch (credentials: 'include'); the agent
# should NOT hand-set these. What's left in customHeaders is app-specific and likely required
# (CSRF tokens, x-requested-with, etc.).
AUTO_HEADERS = {
    "host", "connection", "content-length", "origin", "referer", "cookie",
    "user-agent", "accept", "accept-encoding", "accept-language",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    "pragma", "cache-control", "dnt", "te",
    # Auth headers are surfaced via observedAuthHeaders (cookie=auto, bearer=re-source live),
    # not as copyable customHeaders — their captured values are redacted and unusable.
    "authorization", "x-api-key",
}


def run_engine(run: str) -> None:
    if not os.path.exists(ENGINE):
        sys.exit(f"engine not found at {ENGINE}")
    for stage in ANALYSIS_STAGES:
        proc = subprocess.run(
            ["node", ENGINE, "--run", run, "--stage", stage],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            sys.exit(f"engine stage '{stage}' failed:\n{proc.stderr or proc.stdout}")


def load_jsonl(path: str) -> list[dict[str, object]]:
    if not os.path.exists(path):
        return []
    rows: list[dict[str, object]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def sample_for(run: str, method: str, path_hash: str) -> dict[str, object] | None:
    path = os.path.join(run, "api-spec", "samples", f"{method.lower()}__{path_hash}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def custom_headers(sample: dict[str, object] | None) -> dict[str, str]:
    if not sample:
        return {}
    request = sample.get("request")
    headers = (request.get("headers") if isinstance(request, dict) else None) or {}
    return {str(k): str(v) for k, v in headers.items() if str(k).lower() not in AUTO_HEADERS}


def compact(obj: object, limit: int = 4000) -> object:
    """Return the example as-is when small; otherwise a truncated-but-useful view so a giant response can't
    bloat the output. Either way the agent sees the success field WITHOUT re-opening the raw capture."""
    if obj is None:
        return None
    s = json.dumps(obj)
    if len(s) <= limit:
        return obj
    return {"__truncated__": True, "preview": s[:limit] + " …",
            "topLevelKeys": sorted(obj.keys()) if isinstance(obj, dict) else None}


# ---- structural analysis (over paired.jsonl, the capture-ordered wire of record) ----
#
# These feed S2 (golden source + causal subset). They are protocol/artifact/auth agnostic: nothing
# branches on REST vs GraphQL, JSON vs binary, cookie vs Bearer. App-specific cues (e.g. "status",
# "ApplyTemplate") only ever appear as evidence read off the wire, never as an assumption.

# Header carriers worth threading. Auto/auth headers (§AUTO_HEADERS) are NOT value sites — the browser
# sets the former and the latter are redacted + re-sourced live, so transcribing them is a dead end.
STATUS_FIELD_KEYS = ("status", "state", "phase", "progress", "stage")  # priors for a readiness field
READY_VALUES = ("complete", "completed", "done", "ready", "succeeded", "success", "finished", "available")


def operation_of(row: dict[str, object]) -> str | None:
    body = row.get("reqBody")
    if isinstance(body, dict):
        op = body.get("operationName")
        if isinstance(op, str) and op:
            return op
    return None


def locator_of(row: dict[str, object]) -> str:
    # operation-aware so a multiplexed endpoint (one URL, many ops — e.g. GraphQL) does not collapse
    # distinct operations into one "call". Falls back to method+path for plain REST.
    op = operation_of(row)
    base = f"{row.get('method', '')} {row.get('origin') or ''}{row.get('path') or ''}".strip()
    return f"{base} [{op}]" if op else base


def is_mutation(row: dict[str, object]) -> bool:
    method = str(row.get("method") or "").upper()
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    body = row.get("reqBody")
    # A GraphQL POST is a mutation only if its document says so; a query over POST is a read.
    if isinstance(body, dict) and isinstance(body.get("query"), str):
        return bool(re.match(r"\s*mutation\b", body["query"]))
    return True  # non-GraphQL write methods + plain POST are presumed write


def flatten_json_pointers(obj: object, prefix: str = "") -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            token = str(k).replace("~", "~0").replace("/", "~1")
            out.extend(flatten_json_pointers(v, f"{prefix}/{token}"))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            out.extend(flatten_json_pointers(v, f"{prefix}/{idx}"))
    else:
        out.append((prefix or "/", obj))
    return out


def request_value_sites(row: dict[str, object]) -> list[dict[str, object]]:
    # Every value a request CARRIES, each addressed by a pluggable extractor. Opaque/binary bodies fall
    # to WHOLE-PAYLOAD — un-introspectable is a real value site, never auto-UNEXPLAINED.
    sites: list[dict[str, object]] = []

    # path-tmpl: high-entropy-looking segments of the URL path are candidate path params.
    for seg in str(row.get("path") or "").split("/"):
        if seg and re.search(r"[0-9a-fA-F]{6,}|[0-9]{4,}|_", seg) and not seg.isalpha():
            sites.append({"extractor": "path-tmpl", "path": seg, "value": seg})

    headers = row.get("reqHeaders") if isinstance(row.get("reqHeaders"), dict) else {}
    assert isinstance(headers, dict)
    # header: only non-auto, non-auth headers (CSRF, x-requested-with, custom op headers).
    for k, val in headers.items():
        if str(k).lower() not in AUTO_HEADERS:
            sites.append({"extractor": f"header:{str(k).lower()}", "path": str(k).lower(), "value": val})

    ctype = str(row.get("contentType") or "").lower()
    req_ctype = str(headers.get("content-type") or headers.get("Content-Type") or "").lower()
    body = row.get("reqBody")

    if isinstance(body, (dict, list)):
        for ptr, leaf in flatten_json_pointers(body):
            sites.append({"extractor": f"json-ptr:{ptr}", "path": ptr, "value": leaf})
    elif isinstance(body, str) and body:
        if "multipart/form-data" in req_ctype:
            # each part is its own value site; without a parsed body we expose the whole part stream.
            parts = re.findall(r'name="([^"]+)"', body)
            for part in parts:
                sites.append({"extractor": f"multipart-part:{part}", "path": part, "value": None})
            if not parts:
                sites.append({"extractor": "whole-payload", "path": "/", "value": None})
        elif "application/x-www-form-urlencoded" in req_ctype or ("=" in body and "&" in body):
            for pair in body.split("&"):
                key = pair.split("=", 1)[0]
                if key:
                    sites.append({"extractor": f"form-key:{key}", "path": key, "value": None})
        else:
            # opaque string body (binary/proto/encoded) -> WHOLE-PAYLOAD, addressable but not introspected.
            sites.append({"extractor": "whole-payload", "path": "/", "value": None})
    elif body is None and ctype and "json" not in ctype and str(row.get("method") or "").upper() in {"POST", "PUT", "PATCH"}:
        sites.append({"extractor": "whole-payload", "path": "/", "value": None})

    return sites


def status_field_of(body: object) -> tuple[str, object] | None:
    # The (json-ptr, value) of the most status-like leaf in a response body — the readiness candidate.
    if not isinstance(body, (dict, list)):
        return None
    for ptr, leaf in flatten_json_pointers(body):
        last = ptr.rsplit("/", 1)[-1].lower()
        if any(key in last for key in STATUS_FIELD_KEYS) and isinstance(leaf, (str, bool, int)):
            return ptr, leaf
    return None


def detect_async(group: list[tuple[int, dict[str, object]]]) -> dict[str, object] | None:
    # An async/poll signal: a status read repeated (>=2 reads of one read-locator) OR a 202/404->200
    # transition on one locator. Either means the UI waited on a readiness the replay must POLL.
    reads = [r for _, r in group if not is_mutation(r)]
    statuses = [r.get("status") for _, r in group]

    transition = (any(s in (202, 404) for s in statuses[:-1]) and statuses[-1] == 200) if len(statuses) >= 2 else False
    repeated_read = len(reads) >= 2

    if not (transition or repeated_read):
        return None

    last = group[-1][1]
    sf: tuple[str, object] | None = None
    for _, r in reversed(group):
        sf = status_field_of(r.get("respBody"))
        if sf:
            break

    signal: dict[str, object] = {
        "poll_url": f"{last.get('origin') or ''}{last.get('path') or ''}",
        "operation": operation_of(last),
        "evidence": {"repeated_read": repeated_read, "read_count": len(reads),
                     "status_transition": transition, "statuses": statuses},
        "readyField": None,
        "readyValue": None,
        "readyValueRecognized": False,
    }
    if sf:
        ptr, val = sf
        signal["readyField"] = f"json-ptr:{ptr}"
        signal["readyValue"] = val  # the terminal observation of the status leaf
        # high-confidence when the terminal value is a recognized ready token, not just the last poll.
        signal["readyValueRecognized"] = isinstance(val, str) and val.lower() in READY_VALUES
    elif transition:
        signal["readyField"] = "status-code"
        signal["readyValue"] = 200
        signal["readyValueRecognized"] = True
    return signal


def ordered_calls(rows: list[dict[str, object]], golden_mime: str | None) -> dict[str, object]:
    # The ORDERED call list (not a ranked candidate set): one entry per exchange in capture order, with
    # 1:N responses grouped per locator, value sites, and a per-locator async/poll signal.
    calls: list[dict[str, object]] = []
    by_locator: dict[str, list[tuple[int, dict[str, object]]]] = {}
    for seq, row in enumerate(rows):
        loc = locator_of(row)
        by_locator.setdefault(loc, []).append((seq, row))
        calls.append({
            "seq": seq,
            "exchange_ref": row.get("requestId"),
            "locator": loc,
            "method": row.get("method"),
            "url": f"{row.get('origin') or ''}{row.get('path') or ''}",
            "operation": operation_of(row),
            "status": row.get("status"),
            "is_mutation": is_mutation(row),
            "responseContentType": row.get("contentType"),
            "valueSites": request_value_sites(row),
        })

    # 1:N — group rows sharing a locator into an ORDERED response list; never truncate to the first frame.
    responses: list[dict[str, object]] = []
    polls: list[dict[str, object]] = []
    for loc, group in by_locator.items():
        frames = [{"seq": s, "status": r.get("status"), "contentType": r.get("contentType")}
                  for s, r in group]
        responses.append({
            "locator": loc,
            "frame_count": len(group),
            "is_one_to_n": len(group) > 1,
            "exchange_seqs": [s for s, _ in group],
            "frames": frames,
        })
        async_signal = detect_async(group)
        if async_signal:
            polls.append(async_signal)

    # artifactOrigin — does the golden MIME appear as ANY response content-type? if not -> client-rendered.
    response_mimes = sorted({str(r.get("contentType") or "").split(";")[0].strip().lower()
                             for r in rows if r.get("contentType")})
    golden_present: bool | None = None
    if golden_mime:
        gm = golden_mime.split(";")[0].strip().lower()
        golden_present = any(gm == m or (bool(gm) and bool(m) and gm in m) for m in response_mimes)
    artifact_origin: dict[str, object] = {
        "golden_mime": golden_mime,
        "response_content_types": response_mimes,
        "appears_in_a_response": golden_present,
        # the hint, never a verdict: the S2 gate (classify_values) owns BAIL-1.
        "hint": (None if golden_present is None
                 else ("server-rendered" if golden_present else "client-rendered (BAIL-1 candidate)")),
    }

    return {
        "ordered_calls": calls,
        "responses": responses,
        "polls": polls,
        "artifactOrigin": artifact_origin,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--match", default=None, help="only endpoints whose url contains this substring")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--no-engine", action="store_true", help="skip running the engine (analysis already done)")
    ap.add_argument("--golden-tag", default=None, help="type tag of the UI golden (e.g. pdf) — labels artifactOrigin")
    ap.add_argument("--golden-mime", default=None,
                    help="MIME of the UI golden (e.g. application/pdf) — checks it surfaces as a response content-type")
    args = ap.parse_args()

    if not args.no_engine:
        run_engine(args.run)

    eps = load_jsonl(os.path.join(args.run, "api-spec", "intermediate", "endpoints.with-schemas.jsonl"))
    if not eps:
        sys.exit("no endpoints analyzed; did the capture record any traffic?")

    candidates: list[dict[str, object]] = []
    for ep in eps:
        url = f"{ep.get('origin') or ''}{ep.get('path') or ''}"
        if args.match and args.match not in url:
            continue
        sample = sample_for(args.run, str(ep.get("method") or "GET"), str(ep.get("pathHash") or ""))
        resp_example = ep.get("responseExample")
        candidates.append({
            "method": ep.get("method"),
            "url": url,
            # GraphQL / multiplexed endpoints carry these so the agent can dispatch the operation:
            "operationName": ep.get("operationName"),
            "parentPath": ep.get("parentPath"),
            "discriminatorField": ep.get("discriminatorField"),
            "pathParams": ep.get("pathParams") or [],
            "queryParams": ep.get("queryParams") or [],
            "requestContentType": ep.get("requestContentType"),
            # Auth headers OBSERVED in the trace. Cookies ride the in-page fetch automatically;
            # a Bearer/Authorization or token header must be re-sourced from the page (see SKILL.md).
            "observedAuthHeaders": ep.get("observedAuthHeaders") or [],
            # App-specific non-auto headers the fetch likely needs (CSRF, x-requested-with, ...).
            "customHeaders": custom_headers(sample),
            "requestExample": ep.get("requestExample"),  # redacted body template
            # FULL redacted response (nested) so the predicate's success field (e.g. data.x.pdfUrl) is
            # visible WITHOUT re-opening the raw capture. The engine already inferred this; we used to drop
            # it to top-level keys — which is exactly what forced the manual python-digging.
            "responseExample": compact(resp_example),
            "responseContentTypes": ep.get("responseContentTypes"),  # json vs binary -> predicate + dataBase64/url choice
            "responseBodyKnown": ep.get("responseBodyKnown"),         # false -> body not captured -> can't derive a predicate -> keep UI
            "sampleCount": ep.get("sampleCount"),
            "statusCodes": ep.get("statusCodes") or [],
        })

    # Rank by sample count as a hint only — the agent picks the endpoint matching the action it
    # just demonstrated (use --match to narrow by URL).
    def _sample_count(c: dict[str, object]) -> int:
        n = c.get("sampleCount")
        return n if isinstance(n, int) else 0

    candidates.sort(key=_sample_count, reverse=True)

    # Structural analysis over the capture-ordered wire (paired.jsonl). Independent of the per-endpoint
    # candidate view above: it preserves capture order, 1:N responses, value sites, and the poll signal.
    rows = load_jsonl(os.path.join(args.run, "api-spec", "intermediate", "paired.jsonl"))
    structure = ordered_calls(rows, args.golden_mime)
    structure["golden_tag"] = args.golden_tag

    print(json.dumps({
        "run": args.run,
        "candidate_count": len(candidates),
        "candidates": candidates[: args.top],
        "structure": structure,
    }, indent=2))


if __name__ == "__main__":
    main()
