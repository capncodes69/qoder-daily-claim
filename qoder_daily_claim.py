#!/usr/bin/env python3
"""qoder_daily_claim.py — claim the daily 100-credit campaign, standalone.

For people OUTSIDE contools: one account, one PAT, one script. No contools
code, no database, no proxy pool — just the desktop app's own campaign
endpoints, called exactly the way the Qoder desktop client calls them.

How it works (reverse-engineered from Qoder's activity-iframe.js 0.0.704):
  1. POST https://openapi.qoder.sh/api/v1/jobToken/exchange
     body: {"personal_token": "<your PAT>"}       → jobToken
  2. GET  https://openapi.qoder.sh/sash/api/v1/me/campaigns
     with the DESKTOP headers (User-Agent "Qoder", Cosy-ClientType 10, and a
     machine identity) — the CLI's own headers (ClientType 5) are refused.
     The list carries the daily campaign: actionType CLAIM_BENEFIT,
     claimStatus CLAIMABLE.
  3. POST .../sash/api/v1/me/campaigns/{campaignId}/claim   (campaignId is the
     campaign's UUID `id`; a campaign *key* like act-2026… answers 400)
     → {"status":"CLAIMED","replayed":false}
  4. GET  .../sash/api/v2/me/usage → qoderUsage.addOnQuota is where the
     reward lands (the key is ABSENT until a claim pays out, and it can take
     a few seconds to appear — the script retries).

The claim is idempotent: re-running on a claimed day answers
{"status":"CLAIMED","replayed":true} — safe to run twice, safe in a cron.

Usage:
    python3 qoder_daily_claim.py <PAT>              # claim, print the result
    python3 qoder_daily_claim.py <PAT> --status    # only read status/credits
    python3 qoder_daily_claim.py <PAT> --proxy http://user:pass@host:port
    python3 qoder_daily_claim.py <PAT> --device seed@0   # stable machine id
                                                    (default: derived from the
                                                     PAT, so every run of the
                                                     same PAT is the same
                                                     machine — that matters:
                                                     the campaign platform
                                                     flags devices that jump)

Exit codes: 0 claimed/already-claimed, 1 nothing claimable, 2 API error.

Get a PAT: qoder.com → Settings → Personal Access Tokens → create.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import sys
import time
import urllib.error
import urllib.request
import uuid

API_BASE = "https://openapi.qoder.sh"
EXCHANGE_PATH = "/api/v1/jobToken/exchange"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
USAGE_PATH = "/sash/api/v2/me/usage"
CLAIM_ACTION = "CLAIM_BENEFIT"

# The desktop app's version. Bump this when Qoder ships a new release — the
# server may tie eligibility to the version string.
COSY_VERSION = "1.1.59"
MACHINE_OS = "x86_64_linux"
CLIENT_TYPE_DESKTOP = "10"

UA = "Qoder"  # plain, exactly like the desktop campaign webview


# ── machine identity (deterministic per seed) ────────────────────────────────

def _device_profile(seed: str) -> dict:
    """A stable, seed-derived Linux machine profile (hostname/mac/serial)."""
    h1 = hashlib.sha256((seed + "_host").encode()).hexdigest()
    h2 = hashlib.sha256((seed + "_uuid").encode()).hexdigest()
    h3 = hashlib.sha256((seed + "_mac").encode()).hexdigest()
    h4 = hashlib.sha256((seed + "_serial").encode()).hexdigest()
    stems = ["arch", "ubuntu", "debian", "fedora", "thinkpad", "precision"]
    mac = ":".join(["52", "54", "00", h3[0:2].upper(), h3[2:4].upper(), h3[4:6].upper()])
    return {
        "hostname": f"{stems[int(h1[:8], 16) % len(stems)]}-{h1[8:12]}",
        "product_uuid": str(uuid.UUID(h2[:32])),
        "mac": mac,
        "serial": h4[:12].upper(),
    }


def _machine_identity(seed: str) -> dict:
    """The Cosy-* credential triple a Qoder client presents for one machine."""
    profile = _device_profile(seed)
    # machine_id: what qodercli persists in ~/.qoder/qoder-umid — derived here
    # from the same profile so it is stable for the same seed.
    machine_id = hashlib.sha256(
        f"{profile['hostname']}|{profile['mac']}|{profile['serial']}".encode()
    ).hexdigest()[:32]
    machine_id = str(uuid.UUID(hex=machine_id))
    rng = random.Random(int.from_bytes(
        hashlib.sha256((seed + "_tok").encode()).digest()[:8], "big"))
    token_body = base64.urlsafe_b64encode(
        rng.randbytes(63)).decode("ascii").rstrip("=")
    return {
        "machine_id": machine_id,
        "machineToken": "P1gA" + token_body[:84],
        "machineType": "".join(rng.choices("0123456789abcdef", k=18)),
        "machineCode": ("".join(rng.choices("0123456789abcdef", k=8)) + "00"
                        + "".join(rng.choices("0123456789abcdef", k=8))),
        "hostname": profile["hostname"],
        "mac": profile["mac"],
    }


def campaign_headers(job_token: str, ident: dict) -> dict:
    """The desktop webview's headers — what the campaign platform answers to."""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {job_token}",
        "User-Agent": UA,
        "Cosy-Version": COSY_VERSION,
        "Cosy-ClientType": CLIENT_TYPE_DESKTOP,
        "Cosy-MachineOS": MACHINE_OS,
        "Cosy-MachineId": ident["machine_id"],
        "Cosy-MachineToken": ident["machineToken"],
        "Cosy-MachineType": ident["machineType"],
        "Cosy-MachineCode": ident["machineCode"],
    }


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _opener(proxy: str | None):
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def api(method: str, path: str, *, body=None, headers=None,
        timeout: int = 20, proxy: str | None = None, attempts: int = 3):
    """One call. A transport error retries with backoff; an HTTP error raises
    as `ApiError(status, text)` so the caller can tell the server's verdict
    from a dead network."""
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            with _opener(proxy).open(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}"), resp.status
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace")) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if i + 1 >= attempts:
                break
            time.sleep(1.5 * (i + 1))
    raise ApiError(0, f"transport: {last}")


class ApiError(Exception):
    def __init__(self, status: int, text: str):
        self.status = status
        self.text = text
        super().__init__(f"HTTP {status}: {text[:200]}")


# ── the flow ─────────────────────────────────────────────────────────────────

def exchange(pat: str, ident: dict, proxy: str | None) -> str:
    data, _ = api("POST", EXCHANGE_PATH, body={"personal_token": pat},
                  headers={
                      "Content-Type": "application/json",
                      "Accept": "application/json",
                      "User-Agent": f"qoder/{COSY_VERSION}",
                      "Cosy-Version": COSY_VERSION,
                      "Cosy-ClientType": "5",
                      "Cosy-MachineOS": MACHINE_OS,
                      "Cosy-MachineId": ident["machine_id"],
                      "Cosy-MachineToken": ident["machineToken"],
                      "Cosy-MachineType": ident["machineType"],
                      "Cosy-MachineCode": ident["machineCode"],
                  }, proxy=proxy)
    token = data.get("token") or data.get("jobToken") or data.get("job_token")
    if not token:
        raise ApiError(0, f"exchange: no jobToken in response: {str(data)[:200]}")
    return token


def campaigns(job_token: str, ident: dict, proxy: str | None) -> list[dict]:
    data, _ = api("GET", CAMPAIGNS_PATH,
                  headers=campaign_headers(job_token, ident), proxy=proxy)
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    out = []
    for c in inner.get("campaigns") or []:
        if not isinstance(c, dict):
            continue
        out.append({
            # `id` is the UUID the claim endpoint wants; `campaignKey` is only
            # a label. Some payloads omit `id` — those campaigns cannot be
            # claimed programmatically.
            "id": str(c.get("id") or c.get("campaignId") or ""),
            "key": str(c.get("campaignKey") or ""),
            "name": str(c.get("name") or c.get("campaignName") or ""),
            "actionType": str(c.get("actionType") or ""),
            "claimStatus": str(c.get("claimStatus") or ""),
            "benefit": c.get("benefit", {}).get("amount")
            if isinstance(c.get("benefit"), dict) else None,
            "reason": str(c.get("reason") or c.get("ineligibleReason") or ""),
            "deviceOccupiedByOthers": bool(c.get("deviceOccupiedByOthers")),
        })
    return out


def claim(job_token: str, campaign_id: str, ident: dict, proxy: str | None) -> dict:
    data, _ = api("POST", f"{CAMPAIGNS_PATH}/{campaign_id}/claim",
                  body={}, headers=campaign_headers(job_token, ident), proxy=proxy)
    return data if isinstance(data, dict) else {}


def usage(job_token: str, ident: dict, proxy: str | None) -> dict | None:
    data, _ = api("GET", USAGE_PATH,
                  headers=campaign_headers(job_token, ident), proxy=proxy)
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    qd = inner.get("qoderUsage") if isinstance(inner.get("qoderUsage"), dict) else inner
    return qd


def addon_quota(job_token: str, ident: dict, proxy: str | None,
                attempts: int = 5, delay: float = 2.0):
    """The add-on bucket appears a few seconds AFTER the claim answers —
    retry until it shows or the attempts run out. None = still not visible."""
    for i in range(attempts):
        qd = usage(job_token, ident, proxy)
        bucket = qd.get("addOnQuota") if isinstance(qd, dict) else None
        if isinstance(bucket, dict) and bucket.get("total"):
            return bucket
        if i + 1 < attempts:
            time.sleep(delay)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Claim the Qoder daily 100-credit campaign for one PAT.")
    ap.add_argument("pat", help="the account's Personal Access Token (pt-…)")
    ap.add_argument("--status", action="store_true",
                    help="only read plan/credits, do not claim")
    ap.add_argument("--proxy", default=None,
                    help="optional http(s) proxy URL user:pass@host:port")
    ap.add_argument("--device", default=None,
                    help="device seed (default: derived from the PAT, so every "
                         "run presents the same machine)")
    args = ap.parse_args()

    seed = args.device or f"qoder-{args.pat[:24]}"
    ident = _machine_identity(seed)
    print(f"[device] {ident['hostname']} · mac {ident['mac']} · "
          f"machine {ident['machine_id'][:8]}…")

    try:
        print("[1/4] exchanging PAT → jobToken …")
        job_token = exchange(args.pat, ident, args.proxy)
        print(f"      jobToken {job_token[:16]}…")

        print("[2/4] reading campaigns …")
        camps = campaigns(job_token, ident, args.proxy)
        if not camps:
            print("      no campaigns listed (the account may be ineligible, "
                  "or the platform is not showing any today)")
            return 1
        for c in camps:
            state = (f"{c['claimStatus']}"
                     + (f" · {c['reason']}" if c["reason"] else "")
                     + (" · DEVICE OCCUPIED BY ANOTHER" if c["deviceOccupiedByOthers"] else ""))
            cid = f" · id {c['id'][:8]}…" if c["id"] else " · no id"
            print(f"      - {c['key'] or c['name']}: {c['actionType']} · {state}{cid}")

        claimable = [c for c in camps
                     if c["actionType"] == CLAIM_ACTION
                     and c["claimStatus"].upper() != "CLAIMED"
                     and c["id"]]
        if args.status:
            qd = usage(job_token, ident, args.proxy) or {}
            print(f"[status] plan={qd.get('plan', '?')} "
                  f"credits={qd.get('creditsRemaining', '?')}/{qd.get('creditsTotal', '?')}")
            return 0
        if not claimable:
            already = [c for c in camps if c["claimStatus"].upper() == "CLAIMED"]
            if already:
                print("[done] already claimed today — nothing to do")
                return 0
            # A VIEW_DETAILS campaign is a promo banner: claiming one is
            # accepted (200 CLAIMED) but grants nothing — measured on a live
            # account, plan/credits unchanged before, after, and 30 s later.
            # The credit campaign rotates in and out through the day, so the
            # honest answer is "come back later", not an error.
            print("[done] no CLAIM_BENEFIT campaign on offer right now. A "
                  "VIEW_DETAILS entry is a promo banner — claiming it grants "
                  "nothing. The daily credit campaign rotates in through the "
                  "day; re-run this script later (or check --status).")
            return 1

        for c in claimable:
            print(f"[3/4] claiming {c['key'] or c['name']} "
                  f"(benefit {c['benefit'] or '?'}) …")
            verdict = claim(job_token, c["id"], ident, args.proxy)
            status = verdict.get("status", "?")
            replayed = bool(verdict.get("replayed"))
            print(f"      {status}"
                  + (" (already claimed earlier — replay)" if replayed else ""))

        print("[4/4] reading the add-on bucket …")
        bucket = addon_quota(job_token, ident, args.proxy)
        if bucket:
            print(f"      add-on credits: {bucket.get('remaining')}/"
                  f"{bucket.get('total')} (used {bucket.get('used')})")
            print("DONE — daily claim complete")
            return 0
        qd = usage(job_token, ident, args.proxy) or {}
        print(f"      add-on bucket not visible yet "
              f"(plan={qd.get('plan', '?')}, it can lag a few seconds)")
        print("DONE — claim sent; verify on the dashboard in a minute")
        return 0
    except ApiError as e:
        print(f"ERROR {e}", file=sys.stderr)
        if e.status == 401:
            print("the PAT was refused — expired or revoked. Create a new one "
                  "at qoder.com → Settings → Personal Access Tokens",
                  file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
