# qoder-daily-claim

Claim Qoder's **daily 100-credit campaign** from the command line — one script, one
Personal Access Token, no browser, no Qoder CLI required.

```
python3 qoder_daily_claim.py <PAT>
```

That's it. The script exchanges your PAT for a job token, lists the day's
campaigns, claims the `CLAIM_BENEFIT` one, and reads back the add-on credits
where the reward lands.

## What it does

1. `POST /api/v1/jobToken/exchange` — PAT → jobToken
2. `GET /sash/api/v1/me/campaigns` — the campaign list, called with the
   **desktop** headers (`Cosy-ClientType: 10`, plain `Qoder` UA, a machine
   identity) — the CLI's own headers are refused by the campaign platform
3. `POST .../campaigns/{id}/claim` — claims the daily campaign (the UUID `id`,
   never the `campaignKey`)
4. `GET /sash/api/v2/me/usage` — reads the add-on quota, retrying a few times
   because the reward lands a couple of seconds *after* the claim answers

The machine identity is **derived from your PAT**, so every run of the same
token presents the same machine — the campaign platform flags devices that
jump around (`deviceOccupiedByOthers`).

## Usage

```
python3 qoder_daily_claim.py <PAT>                # claim the daily campaign
python3 qoder_daily_claim.py <PAT> --status       # read plan/credits only
python3 qoder_daily_claim.py <PAT> --proxy http://user:pass@host:port
python3 qoder_daily_claim.py <PAT> --device seed@0
```

Requirements: Python 3.10+, nothing else (stdlib only).

## Getting a PAT

qoder.com → **Settings → Personal Access Tokens** → create. The token starts
with `pt-`.

## Safety

- **Idempotent** — run it twice and the second run answers
  `{"status":"CLAIMED","replayed":true}`. Safe in a cron.
- The script never stores or logs your PAT; it is used for the token exchange
  and then only the short-lived jobToken travels.

## Eligibility

Per the [official docs](https://docs.qoder.com/events/100credits), all
individual users are eligible — free included. **However**, measured over the
API this script uses, the daily campaign entry only appears in the campaign
list once the account has an **active trial or plan**: a fresh free account,
and an account whose trial expired, both see only the promo banner (claiming
that grants nothing — `200 CLAIMED`, zero credits awarded). If your free
account gets "no CLAIM_BENEFIT campaign": sign in once in the Qoder desktop
app — that starts the free Pro Trial (300 credits) — and the daily claim
becomes visible here too. The docs also note rewards must be claimed in the
desktop app's usage panel; this script drives the same claim endpoint that
panel uses.

The daily campaign resets at **10:00 UTC+8**; each reward is valid 30 days
and unexpired credits stack.

## Exit codes

| code | meaning |
|---|---|
| 0 | claimed, or already claimed today |
| 1 | nothing claimable (ineligible, or no campaign with a UUID id) |
| 2 | API error (401 = PAT expired/revoked) |

## Disclaimer

This is an unofficial tool that talks to Qoder's public API the same way the
official desktop client does. Use your own account and your own token.
