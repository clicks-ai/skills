#!/usr/bin/env python3
# Tests for check_chain.py — the G2 NO-FIXED-WAIT gate. Fixture strings only; no plan files, no browser.
# Runs under plain `python3` (no pytest) or under pytest. App-agnostic: the job/list shapes below are
# illustrative wire only (e.g. a status poll, a cursor page) — nothing here assumes any real app/protocol.
import io
import sys
from contextlib import redirect_stdout

import check_chain as c

# ---- fixtures (canonical authoring forms from CONTRACTS §5.3) ----

# The forbidden case: a bare numeric setTimeout gating the act, NO surrounding predicate loop.
BARE_SETTIMEOUT = r"""
run-in-page --contract 1 --allow-mutation --js '(async () => {
  const job = await fetch("/apply", {method:"POST",credentials:"include"}).then(r=>r.json());
  await new Promise(s => setTimeout(s, 8000));        // wait for it to be ready, then export
  const pdf = await fetch(`/job/${job.id}/pdf`, {credentials:"include"});
  return { ok: pdf.ok, status: pdf.status };
})()'
"""

# The correct case: a predicate-driven POLL loop. The inner setTimeout(2000) is the inter-poll backoff.
PROPER_POLL = r"""
run-in-page --contract 1 --allow-mutation --js '(async () => {
  const job = await fetch("/apply", {method:"POST",credentials:"include"}).then(r=>r.json());
  const t0 = Date.now(); let status;
  do {
    const r = await fetch(`/job/${job.id}`, {credentials:"include"});
    status = (await r.json()).status;
    if (status === "COMPLETE") break;
    await new Promise(s => setTimeout(s, 2000));      // inter-poll backoff, NOT a readiness wait
  } while (Date.now() - t0 < 60000);
  if (status !== "COMPLETE") return { ok:false, status, reason:"poll timed out" };
  const pdf = await fetch(`/job/${job.id}/pdf`, {credentials:"include"});
  return { ok: pdf.ok, status: pdf.status };
})()'
"""

# A shell `sleep` used as readiness in a bash chain.
SHELL_SLEEP = r"""
run-in-page --contract 1 --allow-mutation --js 'await fetch("/apply",{method:"POST"})'
sleep 8
run-in-page --contract 1 --js 'await fetch("/job/pdf")'
"""

# A proper REPEAT (cursor pagination) loop.
PROPER_REPEAT = r"""
run-in-page --contract 1 --js '(async () => {
  const items = []; let cursor = null;
  do {
    const r = await fetch(`/list?cursor=${cursor ?? ""}`, {credentials:"include"});
    const page = await r.json();
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return { ok: true, status: 200, count: items.length };
})()'
"""

# No async work at all — a single read, no gap, no wait. Must PASS.
SIMPLE_READ = r"""
run-in-page --contract 1 --js 'await fetch("/me",{credentials:"include"}).then(r=>r.json())'
"""

# A `sleep` only ever mentioned in a comment / string must NOT trip the gate (noise-stripping).
SLEEP_IN_COMMENT = r"""
run-in-page --contract 1 --js '(async () => {
  // we deliberately use no sleep here; readiness is via the poll below
  const t0 = Date.now(); let s;
  do { s = (await (await fetch("/job/1")).json()).status; if (s==="COMPLETE") break;
       await new Promise(r=>setTimeout(r,1500)); } while (Date.now()-t0 < 30000);
  return { ok: s==="COMPLETE", status: 200 };
})()'
"""

# Plan declares a poll gap, but the script covers it with NO loop and NO delay → out-of-band → BAIL-3.
NO_OBSERVATION = r"""
run-in-page --contract 1 --allow-mutation --js '(async () => {
  await fetch("/apply",{method:"POST",credentials:"include"});
  const pdf = await fetch("/job/pdf",{credentials:"include"});
  return { ok: pdf.ok, status: pdf.status };
})()'
"""


# ---- the two contract-mandated cases ----
def test_bare_settimeout_8000_fails() -> None:
    r = c.evaluate(BARE_SETTIMEOUT, None)
    assert r["verdict"] == "FAIL", r
    assert r["fixed_waits"] and r["fixed_waits"][0]["delay"] == "8000"


def test_proper_poll_loop_passes() -> None:
    r = c.evaluate(PROPER_POLL, None)
    assert r["verdict"] == "PASS", r
    assert r["fixed_waits"] == []  # the inner setTimeout is recognised as inter-poll backoff
    assert r["poll_in_script"] is True


# ---- the other readiness-wait shapes ----
def test_shell_sleep_fails() -> None:
    r = c.evaluate(SHELL_SLEEP, None)
    assert r["verdict"] == "FAIL"
    assert any(w["kind"] == "sleep" for w in r["fixed_waits"])


def test_hash_in_double_quoted_string_does_not_hide_wait() -> None:
    # regression: a '#' inside a double-quoted JS string (a CSS selector) must not be treated as a shell
    # comment that deletes the rest of the line — which would hide the following fixed setTimeout.
    src = r"""run-in-page --contract 1 --allow-mutation --js '(async () => {
  document.querySelector("#submit-btn"); await new Promise(s => setTimeout(s, 8000));
  const pdf = await fetch("/job/pdf", {credentials:"include"}); return { ok: pdf.ok };
})()'"""
    r = c.evaluate(src, None)
    assert r["verdict"] == "FAIL", r
    assert r["fixed_waits"] and r["fixed_waits"][0]["delay"] == "8000"


def test_subshell_sleep_is_detected() -> None:
    # regression: a fixed wait inside a subshell `(sleep 8)` must still be caught.
    src = 'curl /apply -X POST\n(sleep 8)\ncurl "/job/pdf" -o "$PROVE_OUT"\n'
    r = c.evaluate(src, None)
    assert r["verdict"] == "FAIL", r
    assert any(w["kind"] == "sleep" and w["delay"] == "8" for w in r["fixed_waits"])


def test_settimeout_with_comma_in_callback_is_detected() -> None:
    # regression: a callback containing commas must not let the fixed delay evade, and an internal numeric
    # arg must not be mistaken for it (only the LAST top-level arg counts).
    src = r"""run-in-page --contract 1 --allow-mutation --js '(async () => {
  setTimeout(() => poll(a, b), 8000);
  const pdf = await fetch("/job/pdf", {credentials:"include"}); return { ok: pdf.ok };
})()'"""
    r = c.evaluate(src, None)
    assert r["verdict"] == "FAIL", r
    assert any(w["delay"] == "8000" for w in r["fixed_waits"])


def test_promise_delay_outside_loop_fails() -> None:
    src = 'await new Promise(r => setTimeout(r, 5000)); await fetch("/act");'
    r = c.evaluate(src, None)
    assert r["verdict"] == "FAIL"
    assert any(w["kind"] in ("promise-delay", "setTimeout") for w in r["fixed_waits"])


# ---- REPEAT / continuation ----
def test_proper_repeat_passes() -> None:
    r = c.evaluate(PROPER_REPEAT, None)
    assert r["verdict"] == "PASS", r
    assert r["repeat_in_script"] is True


def test_repeat_gap_without_loop_fails() -> None:
    # plan says there is a continuation to follow, script has no accumulating loop
    plan = {"control_flow": {"polls": [], "repeats": [{"node": "n0"}]}}
    src = 'const page = await (await fetch("/list")).json(); return { ok:true, status:200 };'
    r = c.evaluate(src, plan)
    assert r["verdict"] == "FAIL"
    assert any("REPEAT" in x for x in r["reasons"])


# ---- no-gap / clean passes ----
def test_simple_read_passes() -> None:
    r = c.evaluate(SIMPLE_READ, None)
    assert r["verdict"] == "PASS"
    assert r["fixed_waits"] == []


def test_sleep_in_comment_is_ignored() -> None:
    r = c.evaluate(SLEEP_IN_COMMENT, None)
    assert r["verdict"] == "PASS", r
    assert r["fixed_waits"] == []


# ---- BAIL-3: async gap with no pollable observation ----
def test_plan_poll_gap_with_no_observation_bails() -> None:
    plan = {"control_flow": {"polls": [{"read": "n2"}], "repeats": []}}
    r = c.evaluate(NO_OBSERVATION, plan)
    assert r["verdict"] == "BAIL-3"
    assert r["bail"]["code"] == "BAIL-3"


def test_plan_poll_gap_covered_by_loop_passes() -> None:
    plan = {"control_flow": {"polls": [{"read": "n2"}], "repeats": []}}
    r = c.evaluate(PROPER_POLL, plan)
    assert r["verdict"] == "PASS", r


# ---- noise-stripping unit ----
def test_strip_noise_drops_comments_and_js_single_quotes() -> None:
    # comments and JS single-quoted data strings are collapsed; structure (calls/braces) survives.
    src = "a(); // sleep 9\nb(); /* sleep 9 */ c('sleep 9'); # sleep 9"
    out = c.strip_noise(src)
    assert "sleep 9" not in out
    assert "a()" in out and "b()" in out and "c(" in out


def test_loop_body_spans_balances_braces() -> None:
    src = 'do { if (x) { y(); } z(); } while (p);'
    spans = c.loop_body_spans(src)
    assert spans, "expected one loop span"
    s = spans[0]
    assert "while (p)" in src[s[0] : s[1]]  # the do/while predicate is inside the span


# ---- main() exit codes (full path through stdout) ----
def _run_main(src: str, tmp: str, plan_obj: dict | None = None) -> int:
    import json
    import os

    cmd = os.path.join(tmp, "command.sh")
    open(cmd, "w").write(src)
    argv = ["--command", cmd]
    if plan_obj is not None:
        pp = os.path.join(tmp, "plan.json")
        json.dump(plan_obj, open(pp, "w"))
        argv += ["--plan", pp]
    with redirect_stdout(io.StringIO()):
        return c.main(argv)


def test_main_exit_codes() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        assert _run_main(PROPER_POLL, d) == 0
        assert _run_main(BARE_SETTIMEOUT, d) == 1
        assert _run_main(NO_OBSERVATION, d, {"control_flow": {"polls": [{"read": "n2"}], "repeats": []}}) == 3


def test_main_missing_file_is_usage() -> None:
    with redirect_stdout(io.StringIO()):
        assert c.main(["--command", "/no/such/command.sh"]) == 5


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
