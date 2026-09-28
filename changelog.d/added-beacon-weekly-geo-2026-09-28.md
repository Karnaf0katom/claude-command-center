Added weekly/monthly install estimates and coarse geo to the anonymous
open beacon (schema v2): it now carries `first_this_week` /
`first_this_month` booleans computed locally from the install's own last
beacon date, still with no identifier. The Worker persists a country code
and first-level region name read from Cloudflare's edge geolocation
(never city, postal code, lat/long, ASN, or raw IP), and `/v1/stats` gains
weekly/monthly active-install counts and suppressed country/region
breakdowns (buckets under 3 beacons fold into "other"). The stats page's
overview adds these counts and a "Where installs are" card. Maintainers
can now self-exclude via `"dev": true` in `telemetry.json`, in addition to
`CCC_TELEMETRY_DEV_MODE=1`.
