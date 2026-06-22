#!/usr/bin/env python3
# run-in-page (contract 1) — execute a captured in-page fetch INSIDE the agent's already-authenticated
# browser tab over CDP, with a body-derived read/write gate, a success predicate, and binary-to-file
# output. This is a generic runtime primitive resolved BY NAME on PATH (never a path into a skill).
#
# A generated workflow step calls it like:
#   run-in-page --contract 1 [--allow-mutation] --match next.waveapps.com \
#     --out /agent/user-data/outputs/invoice.pdf \
#     --vars-json '{"invoice_id":"123","business_id":"abc"}' \
#     --js '(async () => { ... {{invoice_id}} ... return { ok, status, contentType, download:{url} }; })()'
#
# The JS must return a small JSON-serializable object:
#   { ok: <strong predicate: status + content-type + a positive shape signal>, status, contentType, ...,
#     download?: { url } ,           # helper fetches this URL and writes bytes to --out (e.g. pre-signed S3)
#     dataBase64?: "<small inline>" }# OR small inline bytes the helper decodes to --out
# {{var}} placeholders are replaced with the JSON-encoded value from --vars-json (do NOT add your own quotes).
#
# Exit codes (the step branches: 0 => done, anything else => UI fallback):
#   0 success | 1 ran but ok=false / bad output | 2 JS threw / tab not found
#   3 REFUSED: write fetch without --allow-mutation | 4 contract mismatch | 5 usage error
#
# Prereq: a CDP-enabled Chromium on a loopback debug port + `pip install websocket-client`. The browser
# must already be OPEN and on the target origin (the step opens the app first). pick_target waits up to
# --cdp-wait seconds for the browser/tab to be ready, so a just-launched/just-navigated browser is not a
# hard failure (this is what made the first live run fail: the step ran before the browser existed).
#
# POLL/REPEAT/RETRY chains loop INSIDE the single JS (no second helper). A long-but-bounded readiness loop
# can outlast --timeout, so the CDP/socket deadline is floored to the loop's OWN declared budget — the
# loop runs to its predicate instead of a premature THREW. Canonical JS forms: references/chain-patterns.md.

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request
from typing import Any

JsonObj = dict[str, Any]  # a heterogeneous JSON-shaped record (vars / CDP target / evaluate result)

CONTRACT_VERSION = 1

# Exit codes
OK, FAIL, THREW, REFUSED_WRITE, BAD_CONTRACT, USAGE = 0, 1, 2, 3, 4, 5


# ---- pure logic (unit-tested without a browser) ----------------------------

def substitute_vars(js: str, vars_obj: JsonObj) -> str:
    """Replace every ``{{key}}`` in ``js`` with the JSON-encoded value of ``vars_obj[key]``.

    JSON-encoding keeps substitution injection-safe (a string lands as ``"abc"``, a number as ``123``).
    Author the JS WITHOUT wrapping the placeholder in quotes: ``const id = {{invoice_id}};``.
    """
    placeholders = set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", js))
    missing = sorted(p for p in placeholders if p not in vars_obj)
    if missing:
        raise ValueError(f"--vars-json is missing values for: {', '.join(missing)}")
    out = js
    for key in placeholders:
        # a callable repl (not a string) so JSON backslashes/`\g` aren't read as re group refs
        replacement = json.dumps(vars_obj[key])

        def repl(_m: re.Match[str], r: str = replacement) -> str:  # r= binds this iteration's value
            return r

        out = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", repl, out)
    return out


_METHOD_LITERAL_RE = re.compile(r"""method\s*:\s*['"]([A-Za-z]+)['"]""")
_METHOD_KEY_RE = re.compile(r"\bmethod\s*:")
_GQL_MUTATION_RE = re.compile(r"\bmutation\b\s*[A-Za-z({]")  # `mutation Name` / `mutation{` / `mutation(`
_GQL_QUERY_RE = re.compile(r"\bquery\b\s*[A-Za-z({]")
_PERSISTED_RE = re.compile(r"persistedQuery|sha256Hash", re.I)


def classify(js: str) -> str:
    """Derive read|write|unknown from the fetch in ``js``. FAIL SAFE: anything not provably a READ =>
    'unknown' (which the gate treats as a write). Never trust a caller-supplied label, and never let an
    unquoted/variable method or a minified ``mutation{`` slip through as a read."""
    # 1. a GraphQL mutation anywhere => write (catches `mutation Name`, `mutation{`, `mutation(`)
    if _GQL_MUTATION_RE.search(js):
        return "write"
    literal_methods = {m.upper() for m in _METHOD_LITERAL_RE.findall(js)}
    # 2. an explicit destructive verb literal => write
    if literal_methods & {"DELETE", "PUT", "PATCH"}:
        return "write"
    # 3. a method: key whose value is NOT a quoted literal (e.g. a variable) => can't prove read => unknown
    if _METHOD_KEY_RE.search(js) and not _METHOD_LITERAL_RE.search(js):
        return "unknown"
    # 4. POST: a read only if it's a GraphQL query (mutations already excluded above); else require approval
    if "POST" in literal_methods:
        return "read" if _GQL_QUERY_RE.search(js) else "unknown"
    if _PERSISTED_RE.search(js):
        return "unknown"  # persisted op with no inline text -> can't tell -> approval
    # 5. a GET/HEAD literal, or no method key at all (default GET), with no write markers => read
    return "read"


# A POLL/REPEAT/RETRY chain loops INSIDE the single JS (CONTRACTS §5.3): no second helper process. The
# loop's own bound (a `Date.now()-t0 < <ms>` poll timeout, a `setTimeout(s,<ms>)` inter-poll backoff, or a
# bounded RETRY) can outlast --timeout, so a legitimately-waiting readiness loop would be cut short as a
# premature THREW. These regexes read the loop's declared budget out of the JS so the CDP/socket deadline
# can be floored to cover it. They match the canonical authoring forms in references/chain-patterns.md.
_LOOP_TIMEOUT_MS_RE = re.compile(r"Date\.now\(\)\s*-\s*\w+\s*<\s*(\d+)")  # POLL/REPEAT bound: `...< 60000`
_BACKOFF_MS_RE = re.compile(r"setTimeout\(\s*\w+\s*,\s*(\d+)\s*\)")        # inter-poll/retry backoff
_RETRY_ATTEMPTS_RE = re.compile(r"\+\+\w+\s*<\s*(\d+)")                    # bounded RETRY: `++attempt < 3`


def loop_budget_s(js: str) -> float:
    """Upper-bound the wall-clock a bounded in-JS POLL/REPEAT/RETRY chain may legitimately run, read from
    the loop's OWN declared bounds. Generic: it sums the largest declared loop timeout(s) and adds the
    backoff budget across the worst-case retry count, so a long-but-bounded readiness loop is never failed
    early. Returns 0 for a plain one-shot fetch (no loop markers) — callers keep their default then."""
    loop_ms = sum(int(m) for m in _LOOP_TIMEOUT_MS_RE.findall(js))
    backoff_ms = max((int(m) for m in _BACKOFF_MS_RE.findall(js)), default=0)
    attempts = max((int(m) for m in _RETRY_ATTEMPTS_RE.findall(js)), default=0)
    # backoff happens once per loop iteration; a bounded RETRY adds backoff*attempts on top of any timeout.
    total_ms = loop_ms + backoff_ms * max(attempts, 1 if backoff_ms else 0)
    return total_ms / 1000.0


def effective_timeout_s(arg_timeout: int, js: str) -> int:
    """The CDP evaluate timeout to actually use: the larger of the caller's --timeout and the in-JS loop
    budget plus a small margin. Never SHORTENS a caller's explicit timeout; only RAISES it so a long
    bounded readiness chain runs to its own predicate rather than being killed mid-poll."""
    budget = loop_budget_s(js)
    if budget <= 0:
        return arg_timeout
    return max(arg_timeout, int(budget) + 5)  # +5s margin for the final post-loop fetch/return


def evaluate_outcome(result: object, out_path: str | None, out_exists_nonempty: bool) -> tuple[int, JsonObj]:
    """Map the JS return value (+ whether --out got a non-empty file) to an exit code + a small report."""
    if not isinstance(result, dict):
        return FAIL, {"ok": False, "reason": "js did not return an object", "result": result}
    ok = result.get("ok") is True
    report = {k: v for k, v in result.items() if k not in ("dataBase64", "download")}
    report["ok"] = ok
    if out_path is not None and not out_exists_nonempty:
        return FAIL, {**report, "ok": False, "reason": "expected output file is missing or empty", "outPath": out_path}
    if out_path is not None:
        report["outPath"] = out_path
    return (OK if ok else FAIL), report


# ---- CDP + I/O (needs a real browser; covered by the integration-test gate) ----

def _origin(url: str) -> str:
    m = re.match(r"[a-z][a-z0-9+.\-]*://[^/]+", url or "")
    return m.group(0) if m else (url or "")


def pick_target(port: int, match: str | None, wait_s: float = 15.0) -> JsonObj:
    """Find the CDP page target to evaluate in, tolerating a browser that is still booting or a tab still
    navigating. Retries the debug endpoint until ``wait_s`` elapses; fails LOUD on an ambiguous match
    (terminal, no retry) or after the deadline. The wait is what stops a just-launched browser from being
    an instant ``connection refused`` (the bug from the first live run)."""
    deadline = time.monotonic() + max(0.0, wait_s)
    last = f"no CDP endpoint on :{port}"
    while True:
        try:
            data: list[JsonObj] = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5))
            pages = [t for t in data if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if match:
                hits = [t for t in pages if match in (t.get("url") or "")]
                if hits:
                    origins = {_origin(t.get("url") or "") for t in hits}
                    if len(origins) == 1:
                        return hits[0]  # same-origin tabs share cookies — any is safe; pick deterministically
                    raise LookupError(
                        f"{len(hits)} tabs match {match!r} across {len(origins)} origins — ambiguous; narrow --match")
                last = f"no open tab whose URL contains {match!r} yet"
            else:
                http_pages = [t for t in pages if (t.get("url") or "").startswith("http")]
                if len(http_pages) == 1:
                    return http_pages[0]
                last = "multiple/zero http tabs open — pass --match <url-substr> to target the right one"
        except OSError as exc:
            last = f"CDP endpoint not reachable on :{port} ({exc})"
        if time.monotonic() >= deadline:
            raise LookupError(last)
        time.sleep(0.5)


def evaluate_in_page(ws_url: str, js: str, timeout: int) -> JsonObj:
    # websocket-client exceptions are NOT OSError/TimeoutError subclasses — catch them here and re-raise
    # as standard types main() handles, so a handshake/send/recv failure (the TOCTOU cold-start window)
    # becomes a clean THREW with a reason line, never an uncaught traceback.
    from websocket import WebSocketException, WebSocketTimeoutException, create_connection
    try:
        ws = create_connection(ws_url, max_size=None)
        ws.settimeout(timeout + 5)
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
            "expression": js, "awaitPromise": True, "returnByValue": True, "timeout": timeout * 1000}}))
        while True:
            msg: JsonObj = json.loads(ws.recv())
            if msg.get("id") == 2:
                result: JsonObj = msg.get("result", {})
                return result
    except WebSocketTimeoutException as exc:
        raise TimeoutError("timed out waiting for the in-page evaluate result") from exc
    except WebSocketException as exc:
        raise OSError(f"CDP websocket failed: {exc}") from exc


_MAGIC = {".pdf": b"%PDF", ".png": b"\x89PNG", ".zip": b"PK\x03\x04", ".gz": b"\x1f\x8b"}


def looks_like_expected(out_path: str, data: bytes, content_type: str | None) -> bool:
    """Reject an HTML login/error page or a body that doesn't match the declared file type — so a
    cookie-gated download URL that returns an 'access denied' page is a FAILURE, not a false success
    (urllib carries no browser cookies; download.url MUST be self-authenticating, e.g. pre-signed S3)."""
    if not data:
        return False
    if content_type and "text/html" in content_type.lower():
        return False
    if data[:512].lstrip().lower().startswith((b"<!doctype", b"<html")):
        return False
    magic = _MAGIC.get(os.path.splitext(out_path)[1].lower())
    return not (magic and not data.startswith(magic))


def write_out(result: JsonObj, out_path: str, timeout: int) -> bool:
    """Write binary output to --out. Prefer a download URL the helper fetches (no base64 through CDP);
    fall back to small inline dataBase64. Returns True only if a non-empty, type-correct file was written."""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    dl = result.get("download") if isinstance(result, dict) else None
    if isinstance(dl, dict) and dl.get("url"):
        with urllib.request.urlopen(dl["url"], timeout=timeout) as r:
            content_type = r.headers.get("content-type")
            data = r.read()
        if not looks_like_expected(out_path, data, content_type):
            return False
    elif isinstance(result, dict) and result.get("dataBase64"):
        data = base64.b64decode(result["dataBase64"])
        if not looks_like_expected(out_path, data, None):
            return False
    else:
        return False
    with open(out_path, "wb") as f:
        f.write(data)
    return os.path.getsize(out_path) > 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run-in-page", add_help=True)
    ap.add_argument("--contract", type=int, required=True)
    ap.add_argument("--js", default=None, help="JS expression; omit to read from stdin")
    ap.add_argument("--vars-json", default="{}")
    ap.add_argument("--allow-mutation", action="store_true")
    ap.add_argument("--match", default=None, help="substring of the target tab's URL (correct-tab targeting)")
    ap.add_argument("--out", default=None, help="write binary output here")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--cdp-wait", type=float, default=15.0,
                    help="seconds to wait for the browser/tab to become ready before giving up")
    args = ap.parse_args(argv)

    if args.contract != CONTRACT_VERSION:
        print(json.dumps({"ok": False, "reason": f"contract mismatch: step wants {args.contract}, helper is {CONTRACT_VERSION}"}))
        return BAD_CONTRACT

    js_raw = args.js if args.js is not None else sys.stdin.read()
    if not js_raw.strip():
        print(json.dumps({"ok": False, "reason": "no JS (use --js or stdin)"}))
        return USAGE
    try:
        vars_obj = json.loads(args.vars_json)
        if not isinstance(vars_obj, dict):
            raise ValueError("--vars-json must be a JSON object")
        js = substitute_vars(js_raw, vars_obj)
    except ValueError as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}))
        return USAGE

    cls = classify(js)
    if cls in ("write", "unknown") and not args.allow_mutation:
        print(json.dumps({"ok": False, "class": cls, "reason": "refusing a write/unclassified fetch without --allow-mutation"}))
        return REFUSED_WRITE

    # A bounded POLL/REPEAT/RETRY chain may run longer than --timeout; floor the CDP deadline to its own
    # declared budget so a long readiness loop runs to its predicate, never a premature THREW (§5.3).
    timeout = effective_timeout_s(args.timeout, js)
    try:
        target = pick_target(args.port, args.match, args.cdp_wait)
        raw = evaluate_in_page(target["webSocketDebuggerUrl"], js, timeout)
    except (LookupError, TimeoutError, OSError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}))
        return THREW

    if raw.get("exceptionDetails"):
        details = raw["exceptionDetails"]
        detail = details.get("exception", {}).get("description") or details.get("text") or "js exception"
        print(json.dumps({"ok": False, "reason": detail}))
        return THREW

    result = raw.get("result", {}).get("value")
    out_ok = True
    if args.out is not None:
        try:
            out_ok = write_out(result if isinstance(result, dict) else {}, args.out, timeout)
        except OSError as exc:
            print(json.dumps({"ok": False, "reason": f"output write failed: {exc}"}))
            return FAIL

    code, report = evaluate_outcome(result, args.out, out_ok)
    report["class"] = cls
    print(json.dumps(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
