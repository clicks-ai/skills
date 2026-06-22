#!/usr/bin/env bash
# End-to-end smoke test of the reverse-engineer-api skill (teaching-mode pipeline) against safe local
# pages. Asserts: capture works, the engine analysis runs WITHOUT generating any emit/noise files,
# secrets are redacted, the bail-to-UI classifier fires on a signed page, and the in-page fetch
# executor works. No cloud, no API key, no external network required for the replay check.
#
# Env overrides (defaults suit the real runtime; dev boxes pass their own):
#   CHROME=/path/to/chromium   PY=python3   NODE=node
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(git -C "$HERE" rev-parse --show-toplevel)"
SKILL="$ROOT"
CHROME="${CHROME:-chromium}"; PY="${PY:-python3}"; NODE="${NODE:-node}"
PORT=9222; HTTP_PORT=8771
WORK="$(mktemp -d)"; FAILS=0
pass(){ echo "  PASS  $1"; }
fail(){ echo "  FAIL  $1"; FAILS=$((FAILS+1)); }

cleanup(){ kill -9 "${HTTP:-0}" "${CH:-0}" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

"$PY" -m http.server "$HTTP_PORT" --bind 127.0.0.1 --directory "$HERE" >/dev/null 2>&1 & HTTP=$!

launch_chrome(){  # <page>  — leaves chromium running on $PORT (caller kills CH)
  "$CHROME" --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
    --remote-debugging-port="$PORT" --remote-allow-origins='*' \
    --user-data-dir="$WORK/prof-$RANDOM" "http://127.0.0.1:$HTTP_PORT/$1" >/dev/null 2>&1 & CH=$!
  for _ in $(seq 1 20); do curl -s -o /dev/null "http://127.0.0.1:$PORT/json" && break || sleep 0.5; done
}
free_chrome(){ kill -9 "$CH" 2>/dev/null; CH=; sleep 1; }   # free port 9222 before the next launch

capture(){ launch_chrome "$1"; "$PY" "$SKILL/scripts/capture_cdp.py" --port "$PORT" --seconds 10 --out "$WORK/$2"; free_chrome; }

echo "== capture (replayable page) =="
capture test_page.html run1
NET="$WORK/run1/cdp/network"
[ -s "$NET/requests.jsonl" ] && grep -q jsonplaceholder "$NET/requests.jsonl" && pass "capture: requests.jsonl has the POST" || fail "capture: requests.jsonl"
ls "$NET"/bodies/*/response.json >/dev/null 2>&1 && pass "capture: response body captured" || fail "capture: response body"

echo "== analyze (engine brain, NO emit) =="
"$PY" "$SKILL/scripts/analyze.py" --run "$WORK/run1" | grep -q '/posts' && pass "analyze: candidate endpoint surfaced" || fail "analyze: candidate endpoint"
[ -s "$WORK/run1/api-spec/intermediate/endpoints.with-schemas.jsonl" ] && pass "analyze: infer output present" || fail "analyze: infer output"
# The whole point of stopping before emit: none of these noise files should ever exist.
NOISE=0
for f in openapi.yaml openapi.json client.mjs index.html report.md confidence.json; do
  [ -e "$WORK/run1/api-spec/$f" ] && { echo "    unexpected: $f generated"; NOISE=1; }
done
[ "$NOISE" -eq 0 ] && pass "analyze: zero emit/noise files generated" || fail "analyze: emit files leaked"
if grep -rq secret123 "$WORK/run1/api-spec/samples" 2>/dev/null; then fail "analyze: secret NOT redacted"; else pass "analyze: secret redacted in samples"; fi

echo "== decide =="
"$PY" "$SKILL/scripts/detect_replayable.py" --run "$WORK/run1" >/dev/null; [ $? -eq 0 ] && pass "decide: replayable verdict" || fail "decide: replayable verdict"

echo "== run-in-page executor (offline, same-origin, correct-tab) =="
launch_chrome test_page.html
JS='(async()=>{const r=await fetch(location.href);return{ok:r.status===200,status:r.status};})()'
OUT="$(echo "$JS" | "$PY" "$SKILL/scripts/run_in_page.py" --contract 1 --match 127.0.0.1 --port "$PORT" 2>/dev/null)"
echo "$OUT" | grep -q '"status": 200' && pass "run-in-page: in-page fetch returns 200 (read, no --allow-mutation)" || fail "run-in-page: in-page fetch ($OUT)"
free_chrome

echo "== bail-to-UI (signed page) =="
capture test_page_signed.html run2
"$PY" "$SKILL/scripts/analyze.py" --run "$WORK/run2" >/dev/null 2>&1
"$PY" "$SKILL/scripts/detect_replayable.py" --run "$WORK/run2" >/dev/null; [ $? -eq 3 ] && pass "bail: signed body -> keep UI" || fail "bail: signed body"

echo "== $([ $FAILS -eq 0 ] && echo ALL PASS || echo "$FAILS FAILED") =="
exit $FAILS
