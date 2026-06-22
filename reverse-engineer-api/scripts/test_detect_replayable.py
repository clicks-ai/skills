#!/usr/bin/env python3
# Tests for detect_replayable.py's signed/anti-bot classifiers. Plain python3, no browser.
import types

import detect_replayable as d


def _sig(k: str) -> bool:
    return d.SIGNING_KEY_RE.search(k) is not None


def test_signing_re_matches_real_signatures() -> None:
    for k in ("signature", "x-signature", "request_signature", "hmac", "x-hmac", "body_sig", "x-sig",
              "checksum", "x-checksum", "digest", "content-digest", "x-mac"):
        assert _sig(k), f"{k!r} should be flagged as a server signature"


def test_signing_re_does_not_match_incidental_substrings() -> None:
    # the 'sign'/'mac' substring bug: these must NOT be flagged (false keep-UI)
    for k in ("design", "designation", "assignee", "cosigner", "macaddress", "macAddress"):
        assert not _sig(k), f"{k!r} must NOT be flagged (incidental substring)"


def test_signing_re_does_not_flag_bare_nonce() -> None:
    # a client-minted nonce / idempotency key is reproducible (COMPUTED) -> not a keep-UI bail
    for k in ("nonce", "x-nonce", "idempotency-key", "idempotencyKey", "request_id"):
        assert not _sig(k), f"{k!r} is a client-minted value, not a server signature"


def test_captcha_still_flagged() -> None:
    for k in ("g-recaptcha-response", "h-captcha", "cf-turnstile-response"):
        assert d.CAPTCHA_KEY_RE.search(k) is not None


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and isinstance(fn, types.FunctionType):
            fn()
            passed += 1
    print(f"ALL PASS ({passed} tests)")
