-- Beacon schema v2 (2026-09-28): two local "first this period" booleans
-- computed client-side from nothing but the install's own last-beacon
-- date (no identifier), plus coarse edge-computed geo read from
-- Cloudflare's request.cf at insert time (never client-supplied).
--
-- first_this_week / first_this_month let the stats page sum weekly and
-- monthly active installs without ever storing an install id: each is
-- true on exactly one beacon per install per period. v1 rows (all
-- beacons before this migration) stay NULL — the flag did not exist on
-- those clients and cannot be reconstructed after the fact.
--
-- country is the ISO-2 country code; region is the first-level
-- subdivision name (e.g. "California"). Both come only from
-- request.cf, sanitized and bounded in index.js — never city, postal
-- code, lat/long, ASN, or the raw IP. Historical rows stay NULL.
ALTER TABLE opens ADD COLUMN first_this_week INTEGER;
ALTER TABLE opens ADD COLUMN first_this_month INTEGER;
ALTER TABLE opens ADD COLUMN country TEXT;
ALTER TABLE opens ADD COLUMN region TEXT;
