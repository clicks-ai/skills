#!/usr/bin/env python3
# Unit tests for run_in_page pure logic + the non-browser gate paths. Runs with plain `python` (no
# pytest needed) or under pytest. The CDP/browser path itself is covered by the integration-test gate.
import io
import json
import sys
import time
from contextlib import redirect_stdout

import run_in_page as r

UNUSED_PORT = 59999  # nothing listens here -> browser attempts fail fast with connection refused


def _run(argv: list[str]) -> int:
    with redirect_stdout(io.StringIO()):
        return r.main(argv)


# ---- substitute_vars ----
def test_substitute_replaces_and_json_encodes():
    out = r.substitute_vars('const id = {{invoice_id}}; const n = {{count}};', {"invoice_id": "a/b", "count": 3})
    assert out == 'const id = "a/b"; const n = 3;', out

def test_substitute_missing_var_raises():
    try:
        r.substitute_vars('x = {{missing}}', {})
    except ValueError as e:
        assert "missing" in str(e)
    else:
        raise AssertionError("expected ValueError for missing var")


# ---- classify ----
def test_classify_graphql_mutation_is_write():
    assert r.classify('fetch(u,{method:"POST",body:JSON.stringify({query:"mutation Gen($i:X){ gen(input:$i){ ok } }"})})') == "write"

def test_classify_graphql_query_is_read():
    assert r.classify('fetch(u,{method:"POST",body:JSON.stringify({query:"query Me{ me{ id } }"})})') == "read"

def test_classify_get_is_read():
    assert r.classify('fetch(u,{method:"GET",credentials:"include"})') == "read"

def test_classify_delete_is_write():
    assert r.classify('fetch(u,{method:"DELETE"})') == "write"

def test_classify_plain_post_is_unknown():
    assert r.classify('fetch(u,{method:"POST",body:"name=x"})') == "unknown"

def test_classify_persisted_query_is_unknown():
    assert r.classify('fetch(u,{method:"POST",body:JSON.stringify({extensions:{persistedQuery:{sha256Hash:"abc"}}})})') == "unknown"

def test_classify_no_method_is_read():
    assert r.classify('fetch(u,{credentials:"include"})') == "read"


# ---- evaluate_outcome ----
def test_outcome_ok_no_out():
    code, rep = r.evaluate_outcome({"ok": True, "status": 200}, None, True)
    assert code == r.OK and rep["ok"] is True

def test_outcome_ok_false():
    code, _ = r.evaluate_outcome({"ok": False}, None, True)
    assert code == r.FAIL

def test_outcome_out_missing_fails_even_if_ok():
    code, rep = r.evaluate_outcome({"ok": True}, "/agent/user-data/outputs/x.pdf", False)
    assert code == r.FAIL and "missing or empty" in rep["reason"]

def test_outcome_non_dict_result_fails():
    code, _ = r.evaluate_outcome("not-a-dict", None, True)
    assert code == r.FAIL


# ---- main() gate paths (no browser) ----
def test_main_contract_mismatch():
    assert _run(["--contract", "999", "--js", "x"]) == r.BAD_CONTRACT

def test_main_write_without_allow_mutation_refused():
    js = 'fetch(u,{method:"POST",body:JSON.stringify({query:"mutation M{ m{ ok } }"})})'
    assert _run(["--contract", "1", "--port", str(UNUSED_PORT), "--js", js]) == r.REFUSED_WRITE

def test_main_unknown_without_allow_mutation_refused():
    js = 'fetch(u,{method:"POST",body:"raw"})'
    assert _run(["--contract", "1", "--port", str(UNUSED_PORT), "--js", js]) == r.REFUSED_WRITE

def test_main_read_passes_gate_then_tries_browser():
    # a READ should get PAST the gate and fail at the (absent) browser -> THREW, proving the gate let it through
    js = 'fetch(u,{method:"GET"})'
    assert _run(["--contract", "1", "--port", str(UNUSED_PORT), "--cdp-wait", "0", "--js", js]) == r.THREW

def test_main_write_with_allow_mutation_passes_gate():
    js = 'fetch(u,{method:"POST",body:JSON.stringify({query:"mutation M{ m{ ok } }"})})'
    assert _run(["--contract", "1", "--allow-mutation", "--port", str(UNUSED_PORT), "--cdp-wait", "0", "--js", js]) == r.THREW


# ---- pick_target wait/timeout (race fix) ----
def test_pick_target_times_out_fast_without_browser():
    t0 = time.monotonic()
    try:
        r.pick_target(UNUSED_PORT, "next.waveapps.com", wait_s=0.0)
    except LookupError as e:
        assert "not reachable" in str(e) or "no open tab" in str(e)
    else:
        raise AssertionError("expected LookupError when no browser is up")
    assert time.monotonic() - t0 < 5, "wait_s=0 must fail fast, not block"

def _fake_json(tabs):
    return lambda *a, **k: io.BytesIO(json.dumps(tabs).encode())

def test_pick_target_same_origin_multi_picks_first():
    # multiple same-origin tabs share cookies -> deterministic pick (the API path must not evaporate)
    tabs = [
        {"type": "page", "webSocketDebuggerUrl": "ws://x/1", "url": "https://next.waveapps.com/123/invoices"},
        {"type": "page", "webSocketDebuggerUrl": "ws://x/2", "url": "https://next.waveapps.com/123/invoices/9/view"},
    ]
    orig = r.urllib.request.urlopen
    r.urllib.request.urlopen = _fake_json(tabs)
    try:
        assert r.pick_target(1, "next.waveapps.com", wait_s=5.0)["webSocketDebuggerUrl"] == "ws://x/1"
    finally:
        r.urllib.request.urlopen = orig

def test_pick_target_cross_origin_multi_is_ambiguous():
    # the substring spans two DIFFERENT origins -> refuse to guess the wrong one
    tabs = [
        {"type": "page", "webSocketDebuggerUrl": "ws://x/1", "url": "https://next.waveapps.com/a"},
        {"type": "page", "webSocketDebuggerUrl": "ws://x/2", "url": "https://app.waveapps.com/b"},
    ]
    orig = r.urllib.request.urlopen
    r.urllib.request.urlopen = _fake_json(tabs)
    try:
        r.pick_target(1, "waveapps.com", wait_s=5.0)
    except LookupError as e:
        assert "ambiguous" in str(e)
    else:
        raise AssertionError("expected cross-origin ambiguous LookupError")
    finally:
        r.urllib.request.urlopen = orig


# ---- classify fail-safe (write-gate bypasses) ----
def test_classify_variable_method_is_unknown():
    assert r.classify('const m="DELETE"; fetch(u,{method:m})') == "unknown"

def test_classify_minified_mutation_is_write():
    assert r.classify('fetch(u,{method:"POST",body:JSON.stringify({query:"mutation{invoiceDelete(id:1){ok}}"})})') == "write"

def test_classify_anonymous_mutation_is_write():
    assert r.classify('fetch(u,{body:JSON.stringify({query:"mutation($i:X){ del(input:$i){ ok } }"})})') == "write"


# ---- looks_like_expected (no false success on a cookie-gated / HTML download) ----
def test_looks_like_expected_rejects_html_content_type():
    assert r.looks_like_expected("/x/invoice.pdf", b"%PDF-1.7 but lying", "text/html; charset=utf-8") is False

def test_looks_like_expected_rejects_html_body():
    assert r.looks_like_expected("/x/invoice.pdf", b"<!DOCTYPE html><html>access denied</html>", None) is False

def test_looks_like_expected_rejects_wrong_magic():
    assert r.looks_like_expected("/x/invoice.pdf", b"this is not a pdf at all", None) is False

def test_looks_like_expected_accepts_real_pdf():
    assert r.looks_like_expected("/x/invoice.pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", "application/pdf") is True

def test_looks_like_expected_rejects_empty():
    assert r.looks_like_expected("/x/invoice.pdf", b"", "application/pdf") is False

def test_main_missing_js_is_usage():
    assert _run(["--contract", "1", "--js", "   "]) == r.USAGE

def test_main_missing_var_is_usage():
    assert _run(["--contract", "1", "--port", str(UNUSED_PORT), "--js", "x={{nope}}", "--vars-json", "{}"]) == r.USAGE


# ---- loop_budget_s / effective_timeout_s (long POLL/REPEAT/RETRY chains aren't cut short) ----
# Canonical JS forms mirror references/chain-patterns.md.
_POLL_JS = (
    "const t0 = Date.now(); let status;"
    'do { const rr = await fetch("/job/"+jobId); status = (await rr.json()).status;'
    'if (status === "COMPLETE") break;'
    "await new Promise(s => setTimeout(s, 2000)); } while (Date.now() - t0 < 60000);"
)
_REPEAT_JS = (
    "const items = []; let cursor = null;"
    'do { const rr = await fetch("/list?cursor="+(cursor ?? "")); const page = await rr.json();'
    "items.push(...page.items); cursor = page.next_cursor; } while (cursor);"
)
_RETRY_JS = (
    "let resp, attempt = 0;"
    'do { resp = await fetch(actUrl, {method:"POST"});'
    "if (![502,503,429].includes(resp.status)) break; } while (++attempt < 3);"
)
_ONESHOT_JS = 'const r = await fetch(u,{method:"GET"}); return { ok: r.ok };'

def test_loop_budget_oneshot_is_zero():
    # a plain one-shot fetch declares no loop bound -> 0 -> caller keeps its default --timeout
    assert r.loop_budget_s(_ONESHOT_JS) == 0.0

def test_loop_budget_poll_reads_declared_timeout():
    # 60000ms loop bound is the dominant term; backoff is per-iteration, not an extra retry count
    assert r.loop_budget_s(_POLL_JS) == 62.0  # 60s timeout + 2s backoff once

def test_loop_budget_repeat_unbounded_marker_is_zero_timeout():
    # a cursor loop with no Date.now() bound contributes no fixed timeout; budget stays 0 (no loop markers)
    assert r.loop_budget_s(_REPEAT_JS) == 0.0

def test_loop_budget_retry_multiplies_backoff_by_attempts():
    js = _RETRY_JS + " await new Promise(s => setTimeout(s, 1000));"
    assert r.loop_budget_s(js) == 3.0  # 1s backoff * 3 max_attempts, no Date.now() timeout

def test_effective_timeout_raises_for_long_poll():
    # default --timeout 30 must be raised to cover the 60s in-JS poll (+5s margin), else premature THREW
    assert r.effective_timeout_s(30, _POLL_JS) == 67  # int(62) + 5

def test_effective_timeout_never_shortens_caller_value():
    # a generous explicit --timeout is preserved even when the loop budget is smaller
    assert r.effective_timeout_s(300, _POLL_JS) == 300

def test_effective_timeout_oneshot_keeps_default():
    assert r.effective_timeout_s(30, _ONESHOT_JS) == 30

def test_main_long_poll_passes_gate_with_raised_timeout():
    # a long bounded READ poll still passes the read/write gate and reaches the (absent) browser -> THREW.
    # cdp-wait 0 keeps the test fast; the point is the gate + timeout-floor path doesn't reject the loop.
    assert _run(["--contract", "1", "--port", str(UNUSED_PORT), "--cdp-wait", "0", "--js", _POLL_JS]) == r.THREW


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
