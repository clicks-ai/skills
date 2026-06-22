#!/usr/bin/env python3
# Tests for probe_auth.py pure logic + the mocked main() path (no live browser). Runs with plain `python`
# (no pytest needed) or under pytest. The real CDP/browser pass is covered by the integration gate; here we
# mock pick_target/evaluate_in_page so each auth case is driven deterministically from a fixture.
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

import probe_auth as p


def _req(d: str, req: dict) -> str:
    path = os.path.join(d, "req.json")
    open(path, "w").write(json.dumps(req))
    return path


# the in-page probe object that evaluate_in_page would return; pick the candidate matching `case`
def _fake_eval(case: int):
    def _e(ws_url: str, js: str, timeout: int) -> dict:
        if case == 3:
            return {"result": {"value": {"working": False, "case": 3,
                                         "recipe": "no readable auth reproduced the request -> keep UI", "tried": []}}}
        recipes = {1: "credentials:include (case 1, static cookie session)",
                   2: "Bearer = readable cookie 'sid' (case 2)",
                   4: "COMPUTED per-request signature header(s) [x-signature] (case 4)",
                   5: "Bearer = token minted by refresh call (case 5, PRODUCED)"}
        return {"result": {"value": {"working": True, "case": case, "recipe": recipes[case],
                                     "status": 200, "tried": [{"recipe": recipes[case], "status": 200}]}}}
    return _e


def _run_main(argv: list[str], case: int) -> tuple[int, dict]:
    orig_pick, orig_eval = p.pick_target, p.evaluate_in_page
    p.pick_target = lambda port, match, *a, **k: {"webSocketDebuggerUrl": "ws://x/1"}  # type: ignore
    p.evaluate_in_page = _fake_eval(case)  # type: ignore
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = p.main(argv)
    finally:
        p.pick_target, p.evaluate_in_page = orig_pick, orig_eval
    return code, json.loads(buf.getvalue())


# ---- signature_headers (case-4 detection: per-request COMPUTED signature carriers) ----
def test_signature_headers_detects_hmac_and_signature() -> None:
    hdrs = {"content-type": "application/json", "x-signature": "deadbeef", "X-Hmac-Nonce": "abc", "authorization": "Bearer z"}
    out = p.signature_headers(hdrs)
    assert out == ["x-signature", "X-Hmac-Nonce"], out


def test_signature_headers_ignores_ordinary_and_plain_auth() -> None:
    # a static Bearer token is case 2, NOT a per-request signature -> not a signature header
    assert p.signature_headers({"content-type": "application/json", "authorization": "Bearer z"}) == []


def test_signature_headers_matches_digest_and_suffix_sig() -> None:
    out = p.signature_headers({"Digest": "sha-256=...", "x-request-sig": "q", "x-foo": "1"})
    assert "Digest" in out and "x-request-sig" in out and "x-foo" not in out


def test_signature_headers_empty() -> None:
    assert p.signature_headers({}) == []


# ---- build_js (every placeholder filled; cases wired) ----
def test_build_js_substitutes_all_placeholders() -> None:
    req = {"method": "POST", "url": "https://api.example.com/x", "headers": {"x-signature": "s"}, "body": "b"}
    js = p.build_js(req, 200, "api.example.com", {"url": "https://api.example.com/token", "token_ptr": "/access_token"})
    # no placeholder survives substitution
    for ph in ("__REQ__", "__EXPECT__", "__REFRESH__", "__SIG_HEADERS__", "__APP__"):
        assert ph not in js, ph
    assert '"https://api.example.com/x"' in js
    assert "200" in js


def test_build_js_signature_header_threaded_for_case4() -> None:
    req = {"method": "POST", "url": "https://h/x", "headers": {"x-signature": "deadbeef", "content-type": "application/json"}}
    js = p.build_js(req, 200, "h", None)
    # the signature header name is injected so the in-page probe can flag case 4
    assert "x-signature" in js
    # case 4 push is present in the JS body (kept generic — the case-4 branch exists)
    assert "case 4" in js


def test_build_js_no_signature_emits_empty_sig_list() -> None:
    req = {"method": "GET", "url": "https://h/x", "headers": {"content-type": "application/json"}}
    js = p.build_js(req, 200, "h", None)
    assert "__SIG_HEADERS__" not in js
    # the injected sig-headers literal at its declaration site is the empty array (no case-4 candidate)
    assert "const sigHeaders = [];" in js


def test_build_js_refresh_null_when_absent() -> None:
    req = {"method": "GET", "url": "https://h/x", "headers": {}}
    js = p.build_js(req, 200, "h", None)
    # refresh placeholder replaced with JSON null
    assert "const refresh = null;" in js


def test_build_js_refresh_object_threaded_for_case5() -> None:
    req = {"method": "GET", "url": "https://h/x", "headers": {}}
    js = p.build_js(req, 200, "h", {"url": "https://h/token", "method": "POST", "token_ptr": "/access_token"})
    assert "https://h/token" in js and "access_token" in js
    assert "case 5" in js


def test_build_js_preserves_bounded_probe_and_return_shape() -> None:
    js = p.build_js({"method": "GET", "url": "https://h/x", "headers": {}}, 200, "h", None)
    # bounded <=6 probe and the working/case/recipe contract must survive the extend
    assert "cands.slice(0, 6)" in js
    assert "working: true" in js and "working: false" in js
    assert "case: cand.c" in js and "recipe: cand.recipe" in js
    # keep-UI fallback (case 3) intact
    assert "case: 3" in js


def test_build_js_app_sanitized_into_isauth_regex() -> None:
    js = p.build_js({"method": "GET", "url": "https://h/x", "headers": {}}, 200, "next.waveapps.com", None)
    # the app token is sanitized (alnum only) and spliced into the isAuth regex
    assert "next" in js and "__APP__" not in js


# ---- main() mocked-browser path: each case returns through verbatim ----
def test_main_case1_cookie_session() -> None:
    with tempfile.TemporaryDirectory() as d:
        rp = _req(d, {"method": "GET", "url": "https://h/x", "headers": {}, "body": None})
        code, out = _run_main(["--match", "h", "--request", rp], case=1)
        assert code == 0 and out["working"] is True and out["case"] == 1


def test_main_case2_bearer_cookie() -> None:
    with tempfile.TemporaryDirectory() as d:
        rp = _req(d, {"method": "GET", "url": "https://h/x", "headers": {}, "body": None})
        code, out = _run_main(["--match", "h", "--request", rp], case=2)
        assert code == 0 and out["case"] == 2


def test_main_case4_signature_recipe() -> None:
    with tempfile.TemporaryDirectory() as d:
        rp = _req(d, {"method": "POST", "url": "https://h/x", "headers": {"x-signature": "s"}, "body": "b"})
        code, out = _run_main(["--match", "h", "--request", rp], case=4)
        assert code == 0 and out["working"] is True and out["case"] == 4
        assert "signature" in out["recipe"].lower()


def test_main_case5_refresh_mint() -> None:
    with tempfile.TemporaryDirectory() as d:
        rp = _req(d, {"method": "GET", "url": "https://h/x", "headers": {}, "body": None})
        rf = os.path.join(d, "refresh.json")
        open(rf, "w").write(json.dumps({"method": "POST", "url": "https://h/token", "token_ptr": "/access_token"}))
        code, out = _run_main(["--match", "h", "--request", rp, "--refresh", rf], case=5)
        assert code == 0 and out["case"] == 5 and "PRODUCED" in out["recipe"]


def test_main_case3_keep_ui() -> None:
    with tempfile.TemporaryDirectory() as d:
        rp = _req(d, {"method": "GET", "url": "https://h/x", "headers": {}, "body": None})
        code, out = _run_main(["--match", "h", "--request", rp], case=3)
        assert code == 0 and out["working"] is False and out["case"] == 3


# ---- main() unreachable-browser path: keep-UI verdict, exit 2 ----
def test_main_no_browser_returns_case3_exit2() -> None:
    orig_pick = p.pick_target

    def _boom(port: int, match: str | None, *a: object, **k: object) -> dict:
        raise LookupError("CDP not reachable on port")

    p.pick_target = _boom  # type: ignore
    try:
        with tempfile.TemporaryDirectory() as d:
            rp = _req(d, {"method": "GET", "url": "https://h/x", "headers": {}, "body": None})
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = p.main(["--match", "h", "--request", rp])
            out = json.loads(buf.getvalue())
        assert code == 2 and out["working"] is False and out["case"] == 3
    finally:
        p.pick_target = orig_pick


def test_main_refresh_file_loaded_and_threaded() -> None:
    # the refresh fixture reaches build_js -> the emitted JS carries the mint url (proves wiring end-to-end)
    seen: dict[str, str] = {}
    orig_pick, orig_eval = p.pick_target, p.evaluate_in_page
    p.pick_target = lambda port, match, *a, **k: {"webSocketDebuggerUrl": "ws://x/1"}  # type: ignore

    def _capture(ws_url: str, js: str, timeout: int) -> dict:
        seen["js"] = js
        return {"result": {"value": {"working": True, "case": 5, "recipe": "case 5 PRODUCED", "status": 200, "tried": []}}}

    p.evaluate_in_page = _capture  # type: ignore
    try:
        with tempfile.TemporaryDirectory() as d:
            rp = _req(d, {"method": "GET", "url": "https://h/x", "headers": {}, "body": None})
            rf = os.path.join(d, "refresh.json")
            open(rf, "w").write(json.dumps({"method": "POST", "url": "https://h/refresh", "token_ptr": "/tok"}))
            with redirect_stdout(io.StringIO()):
                p.main(["--match", "h", "--request", rp, "--refresh", rf])
    finally:
        p.pick_target, p.evaluate_in_page = orig_pick, orig_eval
    assert "https://h/refresh" in seen["js"] and '"/tok"' in seen["js"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
