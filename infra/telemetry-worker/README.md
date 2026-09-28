# Telemetry Worker

Minimal Cloudflare Worker for CCC's anonymous open beacon (schema 1 and 2),
a legacy daily ping kept for old clients, a landing-page download-click
counter, and public aggregate stats. See
[`docs/telemetry.md`](../../docs/telemetry.md) for the full contract. The source
lives here so every persisted value is auditable.

## What it does

- Accepts `POST /v1/open` with `schema_version` 1 or 2. Schema 2 adds
  `first_this_week` / `first_this_month` (booleans, required when
  `schema_version` is 2); schema 1 rows store `NULL` for those columns.
  Both schemas accept an optional `dev` boolean.
- Reads coarse geo from Cloudflare's own `request.cf` at insert time — never
  from the request body — and persists only `country` (ISO-2) and `region`
  (first-level subdivision name) after format validation. Never city,
  postal code, lat/long, or ASN.
- Accepts `POST /v1/ping` for installs on builds from before 2026-09-28;
  current CCC builds no longer send it (see `docs/telemetry.md`).
- Accepts an empty `POST /v1/download`; it never receives the request object
  and writes only receive time, `ccc.dmg`, and `landing-hero`.
- Serves aggregate-only `GET /v1/stats`, additive-only: totals, 30-day daily
  buckets, weekly/monthly new-install counts derived from
  `first_this_week`/`first_this_month`, `countries_7d`/`regions_7d` (buckets
  under 3 beacons folded into `"other"`), `us_vs_intl_7d`, and site download
  clicks.
- Drops any unknown fields silently. Rejects requests where the listed
  fields fail type validation.
- **Drops the source IP** before writing anywhere durable. The Worker
  hashes it with a daily-rotating secret and stores only the hash.
- Appends a row to a Cloudflare D1 table.
- Returns `204 No Content` on success, `400` on shape errors, `405`
  on wrong method. Never returns row counts or any other state to the
  caller.

That's the entire surface.

## Status

Deployed at `telemetry.claude-command-center.workers.dev`. The immutable source
revision and deployment date are recorded in
[`docs/telemetry-public.md`](../../docs/telemetry-public.md).

## Deploying

The Worker is intentionally tiny (~40 LOC) and uses zero npm
dependencies — `wrangler deploy` is the only step.

```bash
cd infra/telemetry-worker
npm install -g wrangler                # one-time
wrangler login                         # one-time, opens browser
wrangler d1 create ccc-telemetry       # one-time, capture DB id
wrangler d1 execute ccc-telemetry --remote --file migrations/0001-downloads.sql
wrangler d1 execute ccc-telemetry --remote --file migrations/0002-pings-is-dev.sql
wrangler d1 execute ccc-telemetry --remote --file migrations/0003-opens-weekly-monthly-geo.sql
wrangler deploy
```

Migrations run in filename order and are additive (`ALTER TABLE ... ADD
COLUMN`) — safe to run against a live table. `0003` adds `first_this_week`,
`first_this_month`, `country`, and `region` to `opens`; existing rows read
back as `NULL` for all four, which the aggregate queries below treat as
schema-1 data.

The committed `wrangler.toml` binds the public D1 database. Database ids are
resource identifiers, not credentials; authentication remains in Wrangler's
local account configuration.

## Aggregating

The legacy ping table's aggregate query (data frozen at the 2026-09-28
retirement, kept for the "Opt-in pings" legacy tab):

```sql
SELECT
  substr(received_at, 1, 10) AS date,
  version,
  platform,
  COUNT(DISTINCT install_id) AS installs
FROM pings
WHERE received_at >= date('now', '-90 days')
GROUP BY date, version, platform
ORDER BY date DESC;
```

The open beacon carries no identifier, so its weekly/monthly "installs"
figures come from each client's own `first_this_week` / `first_this_month`
flag instead of a `COUNT(DISTINCT ...)`:

```sql
SELECT COUNT(*) AS weekly_new_installs
FROM opens
WHERE first_this_week = 1
  AND received_at >= date('now', '-6 days');
```

Geo breakdowns group by the sanitized `country` / `region` columns and are
suppressed below a minimum count before they ever reach `/v1/stats`:

```sql
SELECT country, COUNT(*) AS beacons
FROM opens
WHERE received_at >= date('now', '-6 days') AND country IS NOT NULL
GROUP BY country
ORDER BY beacons DESC;
```

No per-install or per-beacon rows are ever published; `/v1/stats` returns
aggregates only, with small geo buckets folded into `"other"`
(`suppressSmallBuckets` in `index.js`).

## Why this lives in the same repo

The Worker's privacy guarantees are only worth what the source backs
up. Keeping the code beside `server.py` means anyone auditing the
client can audit the server in one `git clone`. If we ever split this
out, the split itself is a breaking change to the trust contract and
should be documented in `telemetry-public.md` first.
