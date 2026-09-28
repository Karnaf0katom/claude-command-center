Added weekly/monthly install estimates and coarse geo to the anonymous
open beacon (schema v2): it now carries `first_this_week` /
`first_this_month` booleans computed locally from the install's own last
beacon date, still with no identifier. The Worker persists a country code
and first-level region name read from Cloudflare's edge geolocation
(never city, postal code, lat/long, ASN, or raw IP). `/v1/stats` gains
`weekly_active_installs` / `monthly_active_installs` (plus `_prev` and
`_all` variants) bound to exact ISO-week and calendar-month boundaries, and
`geo_week`, one ISO week of country/region breakdown counted per install
(not per beacon) with buckets under 3 installs folded into `"other"`. The
stats page's overview adds these counts and a "Where installs are" card.
Maintainers can now self-exclude via `"dev": true` in `telemetry.json`, in
addition to `CCC_TELEMETRY_DEV_MODE=1`.
