#!/usr/bin/env python3
# probe_auth.py — deterministically find which auth makes a captured request authenticate, in ONE bounded
# in-page pass. This replaces the agent's manual auth hunting (try credentials:include, then grep cookies
# / localStorage, then try each as a Bearer) that was the ~4-minute convergence killer.
#
# Auth material is an ordinary classified Value run through the same machine (DESIGN S5): a static cookie
# session, a readable token sent as Bearer, a per-request COMPUTED signature (a signing recipe re-run each
# call), or a PRODUCED token minted by a refresh/mint call that lives in R. The probe re-sources whichever
# of those reproduces the request and returns its case + recipe.
#
# Usage:  probe_auth.py --match <origin-substr> --request /tmp/req.json [--expect-status 200]
#                       [--refresh /tmp/refresh.json]
#   req.json     = {"method":"POST","url":"https://...","headers":{...incl. any signature/auth...},"body":"...or null"}
#   refresh.json = {"method":"POST","url":"https://.../token","headers":{...},"body":"...","token_ptr":"/access_token"}
#                  optional — a mint call whose response yields a fresh Bearer token (REFRESH-mint, PRODUCED).
#
# Output (stdout JSON):  {"working": true, "case": 1|2|4|5, "recipe": "<how auth was supplied>", ...}
#                  or    {"working": false, "case": 3, "recipe": "...keep UI", ...}
#
# Cases: 1 = cookie session (credentials:include, no header, static)  ·  2 = a readable cookie/localStorage
# value sent as Bearer  ·  4 = COMPUTED per-request signature (the captured request already carries a
# signature header; reproduced verbatim re-using the same in-page signing context)  ·  5 = PRODUCED token
# minted by a refresh call in R (mint, then Bearer)  ·  3 = no readable auth reproduced it -> keep the UI.
#
# SAFETY: it re-fires the request once per auth-placement (<=8). Failed-auth attempts are rejected by the
# server BEFORE the operation runs (no side effect); only the working auth executes it, once. Use only on
# a read or a human-approved consequence-free write — the same gate run-in-page enforces.

import argparse
import json
import re
import sys

from run_in_page import evaluate_in_page, pick_target

# header names that carry a per-request COMPUTED signature (HMAC/nonce/digest) rather than a static token
SIGNATURE_HEADER_RE = re.compile(r"(signature|hmac|x-sig|-sig$|^sig-|nonce|digest|checksum)", re.I)

PROBE_JS = r"""(async () => {
  const req = __REQ__;
  const expect = __EXPECT__;
  const refresh = __REFRESH__;        // {method,url,headers,body,token_ptr} or null
  const sigHeaders = __SIG_HEADERS__; // request-header names already carrying a per-request signature
  const authFail = /unauthenticat|authentication expired|not authenticat|unauthorized|invalid.{0,8}token|forbidden/i;
  const isAuth = /auth|token|session|sid|jwt|bearer|csrf|identity|__APP__/i;
  const readPtr = (obj, ptr) => {
    if (!ptr) return null;
    let cur = obj;
    for (const seg of ptr.split("/").filter(Boolean)) {
      if (cur == null || typeof cur !== "object") return null;
      cur = cur[seg.replace(/~1/g, "/").replace(/~0/g, "~")];
    }
    return (typeof cur === "string") ? cur : null;
  };
  const cands = [{recipe: "credentials:include (case 1, static cookie session)", c: 1, v: null}];
  // case 4 — the captured request already carries a per-request signature header: it is COMPUTED in-page;
  // re-fire it verbatim from this same authenticated context (the recipe = "re-run the page's signer").
  if (sigHeaders.length) {
    cands.push({recipe: "COMPUTED per-request signature header(s) [" + sigHeaders.join(", ") + "] (case 4)", c: 4, v: null});
  }
  document.cookie.split(";").forEach(s => {
    const i = s.indexOf("="); if (i < 0) return;
    const name = s.slice(0, i).trim(), val = s.slice(i + 1).trim();
    if (val && isAuth.test(name)) cands.push({recipe: "Bearer = readable cookie '" + name + "' (case 2)", c: 2, v: val});
  });
  try { for (const k of Object.keys(localStorage)) { if (isAuth.test(k)) { const v = localStorage.getItem(k); if (v && v.length < 4096) cands.push({recipe: "Bearer = readable localStorage['" + k + "'] (case 2)", c: 2, v: v}); } } } catch (e) {}
  // case 5 — mint a fresh token via the refresh call in R, then use it as Bearer (PRODUCED).
  if (refresh && refresh.url) {
    try {
      const mr = await fetch(refresh.url, {method: refresh.method || "POST", credentials: "include", headers: refresh.headers || {}, body: refresh.body || undefined});
      const mt = readPtr(await mr.json().catch(() => null), refresh.token_ptr);
      if (mt) cands.push({recipe: "Bearer = token minted by refresh call (case 5, PRODUCED)", c: 5, v: mt});
    } catch (e) {}
  }
  // Where does the app actually want auth? Try each readable token as Authorization:Bearer AND under the
  // OBSERVED auth-header name(s) the captured request used (e.g. X-Api-Key) — Bearer-only was too strict, a
  // custom-header token would otherwise read as "no readable auth -> keep UI".
  const obsAuthHeaders = Object.keys(req.headers || {}).filter(h => {
    const lh = h.toLowerCase(); return lh !== "cookie" && lh !== "authorization" && isAuth.test(h);
  });
  const placements = (v) => {
    const out = [{h: "authorization", val: "Bearer " + v}];
    for (const h of obsAuthHeaders) { out.push({h: h, val: v}); out.push({h: h, val: "Bearer " + v}); }
    return out;
  };
  const tried = [];
  let attempts = 0;
  for (const cand of cands) {
    if (attempts >= 8) break;
    const variants = cand.v ? placements(cand.v) : [{h: null, val: null}];
    for (const pl of variants) {
      if (attempts >= 8) break;
      attempts++;
      const headers = Object.assign({}, req.headers || {});
      if (pl.h) headers[pl.h] = pl.val;
      const recipe = cand.recipe + (pl.h && pl.h !== "authorization" ? " via header '" + pl.h + "'" : "");
      try {
        const r = await fetch(req.url, {method: req.method || "GET", credentials: "include", headers, body: req.body || undefined});
        const text = await r.text();
        tried.push({recipe: recipe, status: r.status});
        if (r.status === expect && !authFail.test(text)) {
          return {working: true, case: cand.c, recipe: recipe, status: r.status, tried};
        }
      } catch (e) { tried.push({recipe: recipe, error: String(e)}); }
    }
  }
  return {working: false, case: 3, recipe: "no readable auth reproduced the request -> keep UI", tried};
})()"""


def signature_headers(headers: dict[str, object]) -> list[str]:
    return [k for k in headers if SIGNATURE_HEADER_RE.search(k)]


def build_js(req: dict[str, object], expect_status: int, match: str, refresh: dict[str, object] | None) -> str:
    app = re.sub(r"[^A-Za-z0-9]", "", (match.split(".")[0] if "." in match else match)) or "x"
    headers = req.get("headers")
    sigs = signature_headers(headers if isinstance(headers, dict) else {})
    return (PROBE_JS
            .replace("__REQ__", json.dumps(req))
            .replace("__EXPECT__", str(int(expect_status)))
            .replace("__REFRESH__", json.dumps(refresh))
            .replace("__SIG_HEADERS__", json.dumps(sigs))
            .replace("__APP__", app))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="probe_auth")
    ap.add_argument("--match", required=True, help="substring of the target tab's URL")
    ap.add_argument("--request", required=True, help="json file: {method,url,headers,body}")
    ap.add_argument("--refresh", default=None, help="optional json file: a refresh/mint call {method,url,headers,body,token_ptr}")
    ap.add_argument("--expect-status", type=int, default=200)
    ap.add_argument("--port", type=int, default=9222)
    args = ap.parse_args(argv)

    req = json.load(open(args.request))
    refresh = json.load(open(args.refresh)) if args.refresh else None
    js = build_js(req, args.expect_status, args.match, refresh)
    try:
        target = pick_target(args.port, args.match)
        raw = evaluate_in_page(target["webSocketDebuggerUrl"], js, 30)
    except (LookupError, TimeoutError, OSError) as exc:
        print(json.dumps({"working": False, "case": 3, "reason": str(exc)}))
        return 2
    if raw.get("exceptionDetails"):
        print(json.dumps({"working": False, "case": 3, "reason": "probe threw in-page"}))
        return 2
    print(json.dumps(raw.get("result", {}).get("value", {}), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
