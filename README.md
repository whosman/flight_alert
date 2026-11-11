# flight_alert

Watches Google Flights every hour for cheap weekend round trips between Victoria (YYJ) and Calgary (YYC), in both directions, and sends a phone notification through [ntfy](https://ntfy.sh).

## What it checks
- Every weekend until `END_DATE`: leave Thursday, Friday or Saturday; return Sunday or Monday.
- For each trip, the round-trip fare and the sum of two one-way fares. The cheaper one counts.
- Both of Google's result lists ("top flights" and "other flights").

## Notifications
| Notice | When | Priority |
|---|---|---|
| Deal | A trip is under `MAX_PRICE` (default $200 CAD). Sent again if it drops another $10. | Max |
| Almost | A trip is under `NEAR_PRICE` (default $250). | Normal |
| Deal gone | A trip that was a deal went back up or sold out. | Normal |
| Daily check | Lowest price per weekend, first run after 8am Pacific. | Low |
| Failing | Over 20% of searches failed, or over half came back empty. Once a day. | High |
| Crashed / stopped | A run crashed, or the hourly chain could not start the next run. | High |

## How it runs
GitHub's cron schedule is unreliable, so each run starts the next one. The `next` job waits about an hour on the `hourly` environment's wait timer (no runner minutes used), then dispatches a new run. The cron schedule stays as a backup. If several chains ever overlap, only the newest run continues.

To restart the chain: `gh workflow run check.yml`

## Settings
Secret: `NTFY_TOPIC` (required). Optional email: `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`.

Repository variables:
```
gh variable set MAX_PRICE  --body 200
gh variable set NEAR_PRICE --body 250
gh variable set END_DATE   --body 2026-12-13
gh variable set DIGEST     --body 0   # turn off the daily check
```
