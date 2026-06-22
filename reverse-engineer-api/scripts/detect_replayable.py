#!/usr/bin/env python3
# detect_replayable.py — bail-to-GUI classifier (SPEC FR5). Inspects the captured trace and decides
# whether the demonstrated workflow can be reproduced via direct API calls, or whether it depends on
# something only the live browser can produce (signed/HMAC'd bodies, per-request nonces, CAPTCHA
# tokens, active anti-bot challenges). On any such signal -> recommend falling back to the GUI path.
#
# Exit: 0 = replayable, 3 = bail-to-GUI. Operates on `<run>/api-spec/intermediate/paired.jsonl`.
#
# Usage:  python detect_replayable.py --run .o11y/<run> [--match <url-substr>]

import argparse
import json
import os
import re
import sys
from typing import Any

JsonObj = dict[str, Any]  # a heterogeneous JSON-shaped record (paired row)

# Keys carrying a SERVER-SIDE SIGNATURE over the request (HMAC/MAC/digest) — irreproducible without the
# signing secret, so a genuine keep-UI bail. Bounded to whole tokens so it does NOT match incidental
# substrings ("design", "assignee", "macAddress"). A bare nonce / idempotency-key is deliberately NOT here:
# those are client-minted and reproducible — the COMPUTED bucket handles them; only a signature OVER the
# payload bails.
SIGNING_KEY_RE = re.compile(r"(?:^|[^a-zA-Z])(signature|hmac|sig|mac|digest|checksum)(?:[^a-zA-Z]|$)", re.I)
CAPTCHA_KEY_RE = re.compile(r"(recaptcha|g-recaptcha-response|h-captcha|cf-turnstile|captcha)", re.I)
HIGH_ENTROPY_RE = re.compile(r"^[A-Za-z0-9+/_-]{32,}=*$")
ANTIBOT_HEADER_RE = re.compile(r"(cf-mitigated|cf-chl|x-datadome|akamai|perimeterx|x-px|incap)", re.I)


def load_paired(run: str) -> list[JsonObj]:
    path = os.path.join(run, "api-spec", "intermediate", "paired.jsonl")
    if not os.path.exists(path):
        sys.exit(f"no paired trace at {path}; run discover.mjs first")
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def pick_submit(rows: list[JsonObj], match: str | None) -> JsonObj | None:
    cands = [r for r in rows if r.get("method") in ("POST", "PUT", "PATCH", "DELETE")]
    if match:
        cands = [r for r in cands if match in r.get("url", "")]
    cands.sort(key=lambda r: len(json.dumps(r.get("reqBody")) or ""), reverse=True)
    return cands[0] if cands else None


def _walk_kv(obj: Any, out: list[tuple[str, str]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _walk_kv(v, out)
            else:
                out.append((str(k), str(v)))
    elif isinstance(obj, list):
        for v in obj:
            _walk_kv(v, out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--match", default=None)
    args = ap.parse_args()

    rows = load_paired(args.run)
    reasons: list[str] = []

    sub = pick_submit(rows, args.match)
    if sub:
        pairs: list[tuple[str, str]] = []
        _walk_kv(sub.get("reqBody"), pairs)
        for k, v in (sub.get("reqHeaders") or {}).items():
            pairs.append((k, str(v)))
        for k, v in pairs:
            if CAPTCHA_KEY_RE.search(k):
                reasons.append(f"CAPTCHA/Turnstile token in submit ({k}) — only the live browser can mint it")
            elif SIGNING_KEY_RE.search(k) and (HIGH_ENTROPY_RE.match(v) or len(v) >= 24):
                reasons.append(f"server-signature field in submit ({k}) — irreproducible without the signing secret")

    # active anti-bot challenge anywhere in the captured responses
    for r in rows:
        status = r.get("status")
        hdrs = r.get("respHeaders") or {}
        if status in (403, 429) and any(ANTIBOT_HEADER_RE.search(h) for h in hdrs):
            reasons.append(f"anti-bot challenge response observed ({status} on {r.get('path')})")
            break

    replayable = not reasons
    print(json.dumps({
        "replayable": replayable,
        "submit": f"{sub['method']} {sub['url']}" if sub else None,
        "reasons": reasons,
        "recommendation": "direct API replay" if replayable else "BAIL TO GUI",
    }, indent=2))
    sys.exit(0 if replayable else 3)


if __name__ == "__main__":
    main()
