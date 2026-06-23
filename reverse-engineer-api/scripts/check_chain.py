#!/usr/bin/env python3
# check_chain.py — G2 gate (INV-2 NO-FIXED-WAIT). Scans a built command.sh for a FIXED readiness wait
# (a `sleep N`, a `setTimeout(<number>)`, or a numeric-literal Promise delay) used to gate the act, and
# confirms that every async gap is covered by a predicate-driven POLL and every continuation by a REPEAT.
#
# The discrimination that matters (CONTRACTS §5.3): a numeric `setTimeout` is LEGAL as the inter-poll
# backoff INSIDE a loop that also tests a readiness predicate; it is a FAIL only when it gates the act
# with no surrounding predicate loop. Async gaps are read from plan.json control_flow when supplied, and
# always confirmed against the script's own shape — a poll-shaped loop in the script is itself a signal.
#
# Exit: 0 = PASS (no fixed readiness wait; gaps covered) · 1 = FAIL (fixed wait or missing poll/repeat)
#       · 3 = BAIL-3 (an async gap with no pollable observation at all) · 5 = USAGE.
#
# Usage:  python check_chain.py --command command.sh [--plan plan.json]

import argparse
import json
import re
import sys

# setTimeout fixed delays are detected paren-aware (see _settimeout_waits) — a regex `[^,]*?` can't span a
# comma inside the callback (`setTimeout(() => poll(a,b), 8000)`) and would also mis-read an internal numeric
# arg (`bar(3,4)`). Only the LAST top-level argument being a numeric literal is a FIXED wait.
_SETTIMEOUT_OPEN_RE = re.compile(r"setTimeout\s*\(")
_NUMERIC_RE = re.compile(r"\s*\d+(?:\.\d+)?\s*")
# A shell `sleep` (readiness wait in a bash chain): `sleep 8`, `sleep 0.5`, `sleep "$X"` is NOT numeric.
SHELL_SLEEP_RE = re.compile(r"(?:^|[\n;&|(]|\b(?:then|do|else)\b)\s*sleep\s+(\d+(?:\.\d+)?)\b", re.M)

# A readiness predicate inside a loop: something the loop re-tests until it holds. These mirror the POLL
# authoring form (CONTRACTS §5.3) — a status/field/code/presence comparison driving the loop's exit.
PREDICATE_RE = re.compile(
    r"(===|!==|==|!=|\.includes\s*\(|\bstatus\b|\.status\b|\.ok\b|response-presence|"
    r"resource-presence|header-value|body-field|\bwhile\s*\(\s*[^)]*(?:cursor|next|has_more|hasMore|page)\b)",
    re.I,
)
# Loop constructs that can carry a predicate-driven readiness/continuation check.
LOOP_RE = re.compile(r"\b(?:do\s*\{|while\s*\(|for\s*\()")
# Continuation (pagination) signals — drive a REPEAT.
CONTINUATION_RE = re.compile(r"(cursor|next_cursor|nextCursor|has_more|hasMore|next_page|nextPage|\.next\b)", re.I)


def read_command(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# A built command.sh embeds the JS as a shell single-quoted `--js '<js>'` payload. The outer single
# quote is a TRANSPORT wrapper, not a data string — the JS inside is code we must scan. We neutralize
# that wrapper (and `--vars-json`'s) to spaces so the JS body survives, then strip JS/shell COMMENTS and
# JS single-quoted data strings (`'…'` inside JS) so a `sleep`/`setTimeout` written in prose or in a data
# literal never trips the regex. Double-quote and backtick (template-literal) bodies are kept: their
# `${…}` structure carries the loop shape, and call-syntax like `setTimeout(s,8000)` never lives in data.
_WRAP_RE = re.compile(r"--(?:js|vars-json)\s+'")


def _wrapper_spans(src: str) -> set[int]:
    # positions of the shell single-quotes that open/close a `--js '…'` payload (to be ignored as quotes)
    spans: set[int] = set()
    for m in _WRAP_RE.finditer(src):
        open_q = m.end() - 1
        # closing quote is the next single-quote not escaped by shell '\'' splicing (rare); take next '
        close_q = src.find("'", open_q + 1)
        while close_q != -1 and close_q + 1 < len(src) and src[close_q + 1] == "'":
            close_q = src.find("'", close_q + 2)  # tolerate '\'' shell-escape splices
        spans.add(open_q)
        if close_q != -1:
            spans.add(close_q)
    return spans


def strip_noise(src: str) -> str:
    wrappers = _wrapper_spans(src)
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None  # "'" = JS data string (collapse body); '"' or "`" = string (keep, no comments)
    while i < n:
        c = src[i]
        if quote == "'":  # JS single-quoted DATA string -> collapse its body to spaces
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == "'":
                quote = None
            i += 1
            continue
        if quote in ('"', "`"):  # double-quote / template literal -> KEEP body, but a # or // inside is data
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "#" and (i == 0 or src[i - 1] in " \t\n;&|("):  # shell comment only at a word boundary
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if i in wrappers:  # transport quote around a --js payload: keep structure, scan the JS
            out.append(" ")
            i += 1
            continue
        if c == "'":  # a JS data string -> collapse its body
            quote = "'"
            out.append(" ")
            i += 1
            continue
        if c in ('"', "`"):  # a kept string — comments inside it are not comments
            quote = c
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# Find balanced { … } loop bodies starting at each loop keyword, so we can ask "is this numeric delay
# INSIDE a predicate loop?" without a full JS parser. Handles `do { … } while(p)` and `while(p){ … }`.
def loop_body_spans(src: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for m in LOOP_RE.finditer(src):
        brace = src.find("{", m.start())
        if brace < 0:
            continue
        depth, j = 0, brace
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        end = j
        # extend to a trailing `while(...)` so the predicate of a do/while is inside the span
        tail = re.match(r"\s*while\s*\([^)]*\)", src[end + 1 :])
        if tail:
            end = end + 1 + tail.end()
        spans.append((m.start(), end))
    return spans


def _in_any_span(pos: int, spans: list[tuple[int, int]]) -> tuple[int, int] | None:
    for s in spans:
        if s[0] <= pos <= s[1]:
            return s
    return None


def _call_args(src: str, open_paren: int) -> list[str] | None:
    # split a call's args by TOP-LEVEL commas, paren/brace/bracket aware. src[open_paren] must be '('.
    # None if the call is unbalanced. (Also covers `new Promise(r => setTimeout(r, 8000))` via the inner call.)
    depth = 0
    args: list[str] = []
    cur: list[str] = []
    for i in range(open_paren, len(src)):
        c = src[i]
        if c in "([{":
            depth += 1
            if depth > 1:
                cur.append(c)
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(cur))
                return args
            cur.append(c)
        elif c == "," and depth == 1:
            args.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    return None


def _settimeout_waits(src: str) -> list[tuple[int, str]]:
    # setTimeout calls whose LAST top-level argument is a numeric literal — a FIXED delay (vs a variable
    # interval). Paren-aware, so a comma in the callback can't hide it and an internal numeric arg can't fake it.
    out: list[tuple[int, str]] = []
    for m in _SETTIMEOUT_OPEN_RE.finditer(src):
        args = _call_args(src, m.end() - 1)
        if args and _NUMERIC_RE.fullmatch(args[-1]):
            out.append((m.start(), args[-1].strip()))
    return out


# A numeric delay is a FIXED READINESS WAIT iff it is NOT inside a loop body that also carries a
# readiness predicate. Inside such a loop it is the legal inter-poll backoff (CONTRACTS §5.3).
def fixed_waits(src: str) -> list[dict[str, object]]:
    spans = loop_body_spans(src)
    hits: list[dict[str, object]] = []
    raw: list[tuple[str, int, str]] = [("setTimeout", pos, delay) for pos, delay in _settimeout_waits(src)]
    raw += [("sleep", m.start(), m.group(1)) for m in SHELL_SLEEP_RE.finditer(src)]
    for kind, pos, delay in raw:
        span = _in_any_span(pos, spans)
        backoff = span is not None and PREDICATE_RE.search(src[span[0] : span[1]]) is not None
        if not backoff:
            hits.append({"kind": kind, "delay": delay, "pos": pos, "readiness": True})
    return hits


# A poll loop in the script: a loop body that re-tests a readiness predicate (the script's own evidence
# that an async gap is being handled here).
def has_poll_loop(src: str) -> bool:
    for s in loop_body_spans(src):
        body = src[s[0] : s[1]]
        if PREDICATE_RE.search(body) and not CONTINUATION_RE.search(body):
            return True
    return False


def has_repeat_loop(src: str) -> bool:
    return any(CONTINUATION_RE.search(src[s[0] : s[1]]) for s in loop_body_spans(src))


def _plan_gaps(plan: dict[str, object]) -> tuple[int, int]:
    cf = plan.get("control_flow") or {}
    polls = cf.get("polls") if isinstance(cf, dict) else None
    repeats = cf.get("repeats") if isinstance(cf, dict) else None
    return (len(polls) if isinstance(polls, list) else 0, len(repeats) if isinstance(repeats, list) else 0)


def evaluate(src: str, plan: dict[str, object] | None) -> dict[str, object]:
    clean = strip_noise(src)
    waits = fixed_waits(clean)
    poll_in_script = has_poll_loop(clean)
    repeat_in_script = has_repeat_loop(clean)

    plan_polls, plan_repeats = _plan_gaps(plan) if plan else (0, 0)
    # An async gap exists if the plan declares a poll OR the script grew a poll-shaped loop on its own.
    poll_gap = plan_polls > 0 or poll_in_script
    repeat_gap = plan_repeats > 0 or repeat_in_script

    reasons: list[str] = []
    if waits:
        for w in waits:
            reasons.append(f"fixed readiness wait: {w['kind']}({w['delay']}) gates the act with no surrounding predicate loop")

    # Async gap present (from plan or script) but no covering POLL in the script → either a fixed wait was
    # used (already flagged) or NOTHING covers it. With no pollable observation at all → BAIL-3.
    missing_poll = poll_gap and not poll_in_script
    missing_repeat = repeat_gap and not repeat_in_script

    if missing_poll and not waits:
        # plan said there is an async gap, the script has neither a poll loop nor any delay primitive →
        # there is no repeatable observation being read at all.
        return {
            "ok": False,
            "verdict": "BAIL-3",
            "bail": {"code": "BAIL-3", "reason": "async gap declared but no pollable observation in command.sh"},
            "fixed_waits": waits,
            "poll_in_script": poll_in_script,
            "repeat_in_script": repeat_in_script,
            "reasons": ["async gap with no POLL and no readiness observation -> readiness is out-of-band"],
        }
    if missing_poll:
        reasons.append("async gap present but no predicate-driven POLL loop covers it")
    if missing_repeat:
        reasons.append("continuation signal present but no REPEAT loop accumulates it")

    ok = not reasons
    return {
        "ok": ok,
        "verdict": "PASS" if ok else "FAIL",
        "bail": None,
        "fixed_waits": waits,
        "poll_in_script": poll_in_script,
        "repeat_in_script": repeat_in_script,
        "poll_gap": poll_gap,
        "repeat_gap": repeat_gap,
        "reasons": reasons,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="check_chain")
    ap.add_argument("--command", required=True, help="path to the built command.sh")
    ap.add_argument("--plan", default=None, help="optional plan.json (reads control_flow for declared async gaps)")
    args = ap.parse_args(argv)

    try:
        src = read_command(args.command)
    except OSError as exc:
        print(json.dumps({"ok": False, "verdict": "USAGE", "reason": str(exc)}))
        return 5

    plan: dict[str, object] | None = None
    if args.plan:
        try:
            with open(args.plan, encoding="utf-8") as f:
                plan = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "verdict": "USAGE", "reason": f"bad plan: {exc}"}))
            return 5

    result = evaluate(src, plan)
    print(json.dumps(result, indent=2))
    if result["verdict"] == "BAIL-3":
        return 3
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
