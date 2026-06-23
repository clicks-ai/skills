#!/usr/bin/env python3
# capture_cdp.py — record the demonstrated SEGMENT's HTTP traffic from the agent's own Chrome over the
# DevTools Protocol, into the `.o11y/<run>/cdp/network/` layout the engine (scripts/_engine/discover.mjs)
# consumes. No proxy, no MITM: it attaches to the live, already-authenticated tab via the debug port.
#
# CLEAN ONE-SHOT (preferred — record EXACTLY one segment, no timing window to fumble):
#   python capture_cdp.py --out .o11y/run --start   # detaches a recorder, returns immediately
#   …perform the WHOLE segment in the browser, once…
#   python capture_cdp.py --out .o11y/run --stop    # signals the recorder to flush + exit, prints count
# Blocking window (fallback): python capture_cdp.py --out .o11y/run --seconds 90
#
# A whole segment needs >=2 VARIED-input runs to separate CONST/INPUT/COMPUTED. Capture each into a
# SIBLING run dir — either name them yourself (`--out .o11y/run`, `--out .o11y/run2`, …) or pass a base
# with `--run-label` and let it pick the next free `run`/`run2`/`run3`. On --stop/--seconds the recorder
# emits `segment_inputs.json` binding each declared handoff ref to the concrete value it took THIS run
# (per CONTRACTS §2), so classification can confirm co-variation across runs.
#
# Prereqs: Chromium launched with --remote-debugging-port=9222 --remote-allow-origins=*; websocket-client.

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from types import FrameType
from typing import Any

from websocket import WebSocketTimeoutException, create_connection

JsonObj = dict[str, Any]  # a heterogeneous JSON-shaped record (CDP event / trace row / handoff)

_STOP = False


def _on_term(_signum: int, _frame: FrameType | None) -> None:
    global _STOP
    _STOP = True


def pick_page_target(port: int) -> JsonObj:
    data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5))
    pages: list[JsonObj] = [
        t for t in data if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]
    if not pages:
        sys.exit("no page target with a debugger URL; is a tab open?")
    pages.sort(key=lambda t: t.get("url", "").startswith("http"), reverse=True)
    return pages[0]


# ---- sibling run-dir selection (varied-input runs land in run, run2, run3, …) ----

def resolve_run_dir(base: str, label: str | None) -> str:
    """Pick the next free sibling run dir under `base` so each varied-input capture is self-contained.

    `label` names a per-run base (`base/label`, then `base/label2`, …); absent, the base IS the run dir.
    A dir is 'free' until it holds a recorded trace, so an in-progress --start/--stop pair reuses it."""
    if label is None:
        return base
    for n in range(1, 1000):
        cand = os.path.join(base, label if n == 1 else f"{label}{n}")
        if not os.path.exists(os.path.join(cand, "cdp", "network", "requests.jsonl")):
            return cand
    raise RuntimeError("exhausted sibling run dirs")


# ---- binary / streamed response bodies (CONTRACTS: record at least content-type+size+magic, never drop) ----

def _content_type(headers: JsonObj | None) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == "content-type":
            return str(v) if v is not None else None
    return None


def binary_body_record(rid: str, raw_b64: str, content_type: str | None) -> JsonObj:
    """A base64-encoded (binary/streamed) body is NOT introspectable as text, but it is load-bearing —
    record it at WHOLE-PAYLOAD granularity (type + size + magic) so it never silently auto-means a MISS.
    The full base64 is kept too, so a later stage can byte-compare or decode it when an extractor exists."""
    import base64

    try:
        data = base64.b64decode(raw_b64)
    except (ValueError, TypeError):
        data = b""
    return {
        "id": rid,
        "binary": True,
        "contentType": content_type,
        "bytes": len(data),
        "magic": data[:8].decode("latin-1") if data else "",
        "bodyBase64": raw_b64,
    }


class Conn:
    def __init__(self, ws_url: str) -> None:
        self.ws = create_connection(ws_url, max_size=None)
        self.ws.settimeout(1.0)
        self._id = 0

    def send(self, method: str, params: JsonObj | None = None) -> int:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        return self._id

    def recv(self) -> JsonObj | None:
        try:
            msg: JsonObj = json.loads(self.ws.recv())
            return msg
        except WebSocketTimeoutException:
            return None


def capture(port: int, out: str, max_seconds: int) -> int:
    """Record until SIGTERM (one-shot --stop) or max_seconds elapses, then write the engine layout."""
    net_dir = os.path.join(out, "cdp", "network")
    os.makedirs(net_dir, exist_ok=True)
    target = pick_page_target(port)
    print(f"attached to: {target.get('url', '?')}", file=sys.stderr)
    c = Conn(target["webSocketDebuggerUrl"])
    c.send("Network.enable", {"maxResourceBufferSize": 100 << 20, "maxTotalBufferSize": 200 << 20})

    req_events: dict[str, JsonObj] = {}
    resp_events: dict[str, JsonObj] = {}
    resp_bodies: dict[str, str] = {}
    resp_binaries: dict[str, JsonObj] = {}  # rid -> binary_body_record (CONTRACTS: don't drop binary bodies)
    resp_ctype: dict[str, str | None] = {}
    want_post: dict[int, str] = {}
    want_body: dict[int, str] = {}

    def on_reply(msg: JsonObj) -> None:
        res = msg.get("result", {})
        if msg["id"] in want_post:
            rid = want_post.pop(msg["id"])
            ev = req_events.get(rid)
            if ev and res.get("postData") is not None:
                ev["params"]["request"]["postData"] = res["postData"]
        elif msg["id"] in want_body:
            rid = want_body.pop(msg["id"])
            if res.get("body") is None:
                return
            if res.get("base64Encoded"):
                resp_binaries[rid] = binary_body_record(rid, res["body"], resp_ctype.get(rid))
            else:
                resp_bodies[rid] = res["body"]

    deadline = time.time() + max_seconds
    while not _STOP and time.time() < deadline:
        msg = c.recv()
        if msg is None:
            continue
        if "method" in msg:
            m, p = msg["method"], msg.get("params", {})
            if m == "Network.requestWillBeSent":
                rid = p.get("requestId")
                req_events[rid] = msg
                r = p.get("request", {})
                if r.get("hasPostData") and not r.get("postData"):
                    want_post[c.send("Network.getRequestPostData", {"requestId": rid})] = rid
            elif m == "Network.responseReceived":
                rid = p.get("requestId")
                resp_events[rid] = msg
                resp_ctype[rid] = _content_type((p.get("response") or {}).get("headers"))
            elif m == "Network.loadingFinished":
                rid = p.get("requestId")
                if rid in req_events:
                    want_body[c.send("Network.getResponseBody", {"requestId": rid})] = rid
        elif "id" in msg:
            on_reply(msg)

    drain = time.time() + 4
    while time.time() < drain and (want_post or want_body):
        msg = c.recv()
        if msg and "id" in msg:
            on_reply(msg)

    bodies_dir = os.path.join(net_dir, "bodies")
    with open(os.path.join(net_dir, "requests.jsonl"), "w") as f:
        for ev in req_events.values():
            f.write(json.dumps(ev) + "\n")
            rid = ev["params"]["requestId"]
            post = ev["params"].get("request", {}).get("postData")
            if post is not None:
                d = os.path.join(bodies_dir, rid)
                os.makedirs(d, exist_ok=True)
                json.dump({"id": rid, "body": post}, open(os.path.join(d, "request.json"), "w"))
    with open(os.path.join(net_dir, "responses.jsonl"), "w") as f:
        for ev in resp_events.values():
            f.write(json.dumps(ev) + "\n")
    for rid, body in resp_bodies.items():
        d = os.path.join(bodies_dir, rid)
        os.makedirs(d, exist_ok=True)
        json.dump({"id": rid, "body": body}, open(os.path.join(d, "response.json"), "w"))
    # Binary/streamed bodies: a metadata sidecar so the WHOLE-PAYLOAD record survives the layout.
    for rid, rec in resp_binaries.items():
        d = os.path.join(bodies_dir, rid)
        os.makedirs(d, exist_ok=True)
        json.dump(rec, open(os.path.join(d, "response.json"), "w"))

    print(
        f"wrote {len(req_events)} requests, {len(resp_events)} responses "
        f"({len(resp_binaries)} binary) to {net_dir}",
        file=sys.stderr,
    )
    return len(req_events)


# ---- segment_inputs.json (CONTRACTS §2 — bind each handoff ref to its concrete captured value) ----

def parse_json_ptr(ptr: str) -> list[str]:
    # RFC6901: ~1 -> /, ~0 -> ~; the leading "" segment of "/a/b" is dropped.
    return [seg.replace("~1", "/").replace("~0", "~") for seg in ptr.split("/")[1:]] if ptr else []


def apply_json_ptr(obj: object, ptr: str) -> object:
    cur = obj
    for seg in parse_json_ptr(ptr):
        if isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        elif isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def extract_value(extractor: str, paired: list[JsonObj], step_inputs: dict[str, object]) -> object:
    """Read the concrete value an extractor addresses. `json-ptr:/p` / `header:name` scan the trace's
    responses (first hit wins, capture order); `form-key`/`path-tmpl`/`multipart-part`/`binary-decoder`/
    `whole-payload` are PRIOR_SEGMENT shapes resolved at classify-time, not here -> None (left to bind later)."""
    kind, _, arg = extractor.partition(":")
    if kind == "json-ptr":
        for row in paired:
            v = apply_json_ptr(row.get("respBody"), arg)
            if v is not None:
                return v
        return None
    if kind == "header":
        want = arg.lower()
        for row in paired:
            for k, val in (row.get("respHeaders") or {}).items():
                if k.lower() == want:
                    return val
        return None
    return None


def load_paired(run: str) -> list[JsonObj]:
    path = os.path.join(run, "api-spec", "intermediate", "paired.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows: list[JsonObj] = [json.loads(line) for line in f if line.strip()]
    return rows


def build_segment_inputs(
    run: str,
    segments: JsonObj,
    step_inputs: dict[str, object],
    input_identity: JsonObj,
    golden: JsonObj | None,
    paired: list[JsonObj],
) -> JsonObj:
    """Bind every STEP_INPUT/PRIOR_UI/PRIOR_SEGMENT consume of every segment to the concrete value it took
    this run. STEP_INPUT comes from operator-supplied `step_inputs`; the rest are read out of the trace."""
    bindings: list[JsonObj] = []
    for region in segments.get("regions", []):
        if region.get("kind") != "ApiSegment":
            continue
        seg_id = region.get("id")
        for spec in region.get("consumes", []):
            ref = spec.get("ref")
            origin = spec.get("origin")
            extractor = spec.get("extractor", "")
            if origin == "STEP_INPUT":
                value = step_inputs.get(ref)
            else:  # PRIOR_UI / PRIOR_SEGMENT -> read from the captured trace
                value = extract_value(extractor, paired, step_inputs)
            bindings.append({
                "ref": ref,
                "segment_id": seg_id,
                "origin": origin,
                "value": value,
                "shape": spec.get("shape"),
                "extractor": extractor,
            })
    return {
        "schema": "segment_inputs/v1",
        "run": run,
        "input_identity": input_identity,
        "bindings": bindings,
        "golden": golden,
    }


def golden_record(path: str | None, tag: str | None, produces_ref: str | None) -> JsonObj | None:
    if path is None and tag is None and produces_ref is None:
        return None
    rec: JsonObj = {"path": path, "tag": tag, "bytes": None, "sha256": None, "produces_ref": produces_ref}
    if path and os.path.exists(path):
        import hashlib

        data = open(path, "rb").read()
        rec["bytes"] = len(data)
        rec["sha256"] = hashlib.sha256(data).hexdigest()[:16]
    return rec


def write_segment_inputs(out: str, args: argparse.Namespace) -> None:
    """Emit <out>/segment_inputs.json on --stop/--seconds when a segments.json was supplied. Silent no-op
    without --segments (the operator opted out of binding this run)."""
    if not args.segments:
        return
    if not os.path.exists(args.segments):
        print(f"warning: --segments {args.segments} not found; skipping segment_inputs.json", file=sys.stderr)
        return
    with open(args.segments) as f:
        segments = json.load(f)
    step_inputs: dict[str, object] = json.loads(args.inputs_json) if args.inputs_json else {}
    label = args.run_label or os.path.basename(out.rstrip("/")) or "run1"
    ambient: JsonObj = json.loads(args.ambient_json) if args.ambient_json else {}
    identity = {"label": label, "ambient": ambient}
    golden = golden_record(args.golden, args.golden_tag, args.produces_ref)
    si = build_segment_inputs(out, segments, step_inputs, identity, golden, load_paired(out))
    dest = os.path.join(out, "segment_inputs.json")
    with open(dest, "w") as f:
        json.dump(si, f, indent=2)
    print(f"wrote {len(si['bindings'])} handoff bindings -> {dest}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--out", required=True, help="run dir (base if --run-label); trace under <out>/cdp/network/")
    ap.add_argument("--run-label", default=None,
                    help="treat --out as a base and pick the next free sibling (label, label2, …)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", action="store_true", help="detach a recorder and return immediately")
    g.add_argument("--stop", action="store_true", help="signal the recorder to flush + exit")
    g.add_argument("--seconds", type=int, help="blocking window mode (no detach)")
    g.add_argument("--_capture", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--max-seconds", type=int, default=300, help="safety cap for --start mode")
    # segment_inputs.json (CONTRACTS §2): bind declared handoff refs to this run's concrete values.
    ap.add_argument("--segments", default=None, help="segments.json; on --stop/--seconds emit segment_inputs.json")
    ap.add_argument("--inputs-json", default=None, help="JSON object {ref: value} for STEP_INPUT consumes")
    ap.add_argument("--ambient-json", default=None, help="JSON object of ambient identity (tenant/session)")
    ap.add_argument("--golden", default=None, help="path to the artifact the UI produced this run")
    ap.add_argument("--golden-tag", default=None, help="type tag of the golden (e.g. pdf, csv, png)")
    ap.add_argument("--produces-ref", default=None, help="handoff ref the golden satisfies (terminal produces)")
    args = ap.parse_args()

    # --start mints/locks the run dir; --stop/--seconds reuse the same one a sibling-aware resolve returns.
    out = resolve_run_dir(args.out, args.run_label) if (args.run_label and not args._capture) else args.out
    pidfile = os.path.join(out, "cdp", "capture.pid")

    if args._capture:
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
        capture(args.port, args.out, args.max_seconds)
        return

    if args.seconds:
        capture(args.port, out, args.seconds)
        write_segment_inputs(out, args)
        return

    if args.start:
        os.makedirs(os.path.dirname(pidfile), exist_ok=True)
        log = open(os.path.join(out, "cdp", "capture.log"), "w")
        p = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--_capture",
             "--port", str(args.port), "--out", out, "--max-seconds", str(args.max_seconds)],
            start_new_session=True, stdout=log, stderr=log,
        )
        with open(pidfile, "w") as f:
            f.write(str(p.pid))
        time.sleep(1.5)  # let it attach + Network.enable before the action
        print(f"capture started (pid {p.pid}) into {out} — perform the WHOLE segment now, then run --stop")
        return

    # --stop
    if not os.path.exists(pidfile):
        sys.exit("no capture.pid — was --start run for this --out?")
    pid = int(open(pidfile).read().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(40):  # wait up to ~10s for it to flush + exit
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.25)
    os.remove(pidfile)
    reqs = os.path.join(out, "cdp", "network", "requests.jsonl")
    n = sum(1 for _ in open(reqs)) if os.path.exists(reqs) else 0
    print(f"capture stopped — {n} requests recorded -> {os.path.dirname(reqs)}")
    write_segment_inputs(out, args)


if __name__ == "__main__":
    main()
