#!/usr/bin/env python3
# teach_insert.py — mechanically perform the surgical insert that turns a mission-style UI-only step
# into an API-backed step, editing ONLY the target file.
#
# Why this exists: when the agent hand-edited the step it churned the UI (parameterised the login email),
# touched an unrelated step's file, and drifted the section names. This script removes that freedom: the
# agent supplies ONLY the `## API attempt` body; everything else is mechanical and guaranteed —
#   - the provenance header goes on top,
#   - `## API attempt` is inserted ABOVE the original instructions,
#   - the original `Instructions:` block is preserved BYTE-FOR-BYTE under `## UI instructions`,
#   - `method` is added to `Return value`,
#   - Mission / Inputs / Important are untouched,
#   - and NO other file is written (single-file by construction).
#
# The write is GATED: it refuses unless a verify_receipt.json proves the API actually equals the UI on an
# instance we did NOT build — a passing verdict AND a held-out instance distinct from the proof golden.
# Without this, "a file was produced" would masquerade as "the API reproduces the UI" (the documented miss).
#
# Usage:  teach_insert.py --step steps/<STEP>.md --header "<provenance, no comment markers>"
#                         --verify verify_receipt.json [--command <file>]
#         (or pipe the API body on stdin and omit --command)

from __future__ import annotations

import argparse
import json
import sys

# the top-level verdict prove_runner writes when the G3 proof passes (per-comparison "MATCH" is nested)
PROVEN_VERDICT = "PROVEN"


class GateError(Exception):
    pass


def check_receipt(receipt: object) -> None:
    # The two non-negotiables: the proof passed, and it was proved against a held-out instance — not the
    # very instance we replayed (same-instance similarity is the shared-state false-pass G3 was built to kill).
    if not isinstance(receipt, dict):
        raise GateError("verify receipt is not a JSON object")
    verdict = receipt.get("verdict")
    if verdict != PROVEN_VERDICT:
        raise GateError(
            f"verify receipt verdict is {verdict!r}, refusing to teach — only {PROVEN_VERDICT!r} ships an API step"
        )
    coverage = receipt.get("coverage")
    if not isinstance(coverage, dict):
        raise GateError("verify receipt missing coverage block — cannot confirm a held-out instance")
    if coverage.get("fresh_not_build_instance") is not True:
        raise GateError(
            "verify receipt coverage.fresh_not_build_instance is not true — the proof ran on the build "
            "instance, not a held-out one (the shared-state false-pass G3 exists to kill)"
        )


def transform(step_md: str, header: str, command: str) -> str:
    if "## API attempt" in step_md:
        raise ValueError("already has a '## API attempt' section — regenerate from a clean UI-only baseline")
    if "Instructions:" not in step_md:
        raise ValueError("not a mission-style UI step (no 'Instructions:' heading)")
    if "Return value:" not in step_md:
        raise ValueError("not a mission-style UI step (no 'Return value:' block)")

    before, after = step_md.split("Instructions:", 1)  # `after` = the numbered steps, kept verbatim
    # The run+branch wrapper is FIXED here (not left to the generator) so every taught step tells the
    # skill-blind executor to actually run the command first and only fall back to the UI on failure.
    api_block = (
        "## API attempt\n\n"
        "**Do this first — do not skip to the UI.** Replace each `{{...}}` below with the matching value "
        "from your inputs, then run the command.\n"
        "- **Exit code 0** → the output file is saved. Set Return value `method: api` and STOP.\n"
        "- **Any other exit code** → only then do the `## UI instructions` below. Do not investigate, read "
        "cookies, or click around first — just do the UI steps.\n\n"
        "```bash\n" + command.strip() + "\n```\n\n"
        "## UI instructions"
    )
    body = before + api_block + after

    # record which path ran: add `method` as the first Return value bullet (once)
    head, sep, tail = body.partition("Return value:")  # tail starts with "\n- ..."
    if "method:" not in tail.split("\n\n", 1)[0]:
        body = head + sep + '\n- method: "api" or "ui".' + tail

    return f"<!-- {header.strip()} -->\n" + body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="teach_insert")
    ap.add_argument("--step", required=True, help="path to the mission-style UI-only steps/<STEP>.md")
    ap.add_argument("--header", required=True, help="provenance line WITHOUT the <!-- --> markers")
    ap.add_argument("--verify", required=True, help="path to verify_receipt.json — the empirical proof gate")
    ap.add_argument("--command", default=None, help="file with the run-in-page command; omit to read stdin")
    args = ap.parse_args(argv)

    try:
        with open(args.verify) as f:
            receipt = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read verify receipt {args.verify}: {exc}", file=sys.stderr)
        return 3
    try:
        check_receipt(receipt)
    except GateError as exc:
        print(f"refusing to teach: {exc}", file=sys.stderr)
        return 3

    with open(args.step) as f:
        step_md = f.read()
    command = open(args.command).read() if args.command else sys.stdin.read()
    if not command.strip():
        print("error: empty command", file=sys.stderr)
        return 2

    try:
        out = transform(step_md, args.header, command)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with open(args.step, "w") as f:
        f.write(out)
    print(f"inserted ## API attempt into {args.step}")
    print("now verify ONLY this file changed:  git -C <skill> diff --name-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
