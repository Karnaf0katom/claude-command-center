import assert from "node:assert/strict";
import test from "node:test";

import worker from "./index.js";


test("download click stores only bounded server-side values", async () => {
  const writes = [];
  const env = {
    DB: {
      prepare(sql) {
        return {
          bind(...values) {
            writes.push({ sql, values });
            return { run: async () => ({ success: true }) };
          },
        };
      },
    },
  };
  const request = new Request("https://telemetry.example/v1/download", {
    method: "POST",
    headers: {
      "CF-Connecting-IP": "203.0.113.9",
      "User-Agent": "private test agent",
      Referer: "https://private.example/path",
      Cookie: "private=value",
    },
    body: "ignored private body",
  });

  const response = await worker.fetch(request, env);

  assert.equal(response.status, 204);
  assert.equal(writes.length, 1);
  assert.match(writes[0].sql, /INSERT INTO downloads/);
  assert.equal(writes[0].values.length, 3);
  assert.match(writes[0].values[0], /^\d{4}-\d{2}-\d{2}T/);
  assert.deepEqual(writes[0].values.slice(1), ["ccc.dmg", "landing-hero"]);
  assert.doesNotMatch(JSON.stringify(writes), /203\.0\.113\.9|private/);
});


test("download click remains opaque when D1 fails", async () => {
  const env = {
    DB: {
      prepare() {
        throw new Error("D1 unavailable");
      },
    },
  };
  const request = new Request("https://telemetry.example/v1/download", {
    method: "POST",
  });

  const response = await worker.fetch(request, env);

  assert.equal(response.status, 204);
  assert.equal(await response.text(), "");
});


test("stats exposes aggregate clicks without event rows", async () => {
  const queries = [];
  const env = {
    DB: {
      prepare(sql) {
        queries.push(sql);
        return {
          first: async () => ({
            total_opens: 3,
            total_pings: 2,
            distinct_installs: 1,
            total_downloads: 7,
          }),
          all: async () => ({
            results: sql.includes("FROM downloads")
              ? [{ day: "2026-07-15", download_clicks: 4 }]
              : [],
          }),
        };
      },
    },
  };

  const response = await worker.fetch(
    new Request("https://telemetry.example/v1/stats"),
    env,
  );
  const payload = await response.json();

  assert.equal(response.status, 200);
  assert.equal(payload.totals.total_downloads, 7);
  assert.deepEqual(payload.downloads_by_day, [
    { day: "2026-07-15", download_clicks: 4 },
  ]);
  assert.equal(payload.downloads, undefined);
  assert.equal(queries.filter(sql => /GROUP BY day ORDER BY day DESC LIMIT 90/.test(sql)).length, 3);
});

test("an unknown engine name never rejects the ping", async () => {
  const writes = [];
  const env = {
    DB: {
      prepare(sql) {
        return {
          bind(...values) {
            writes.push({ sql, values });
            return { run: async () => ({ success: true }) };
          },
        };
      },
    },
    IP_HASH_SECRET: "test-secret",
  };
  const body = {
    schema_version: 3,
    install_id: "00000000-0000-4000-8000-000000000002",
    version: "5.23.0",
    platform: "darwin",
    // `opencode` is known now; `warpdrive` stands in for the next engine
    // CCC learns to detect before this Worker is redeployed.
    engines: "claude,opencode,warpdrive",
    last_active_date: "2026-08-12",
    sessions_today: 1,
    active_seconds_today: 30,
    total_sessions_managed: 1,
  };
  const response = await worker.fetch(
    new Request("https://telemetry.example/v1/ping", {
      method: "POST",
      headers: { "Content-Type": "application/json", "CF-Connecting-IP": "203.0.113.9" },
      body: JSON.stringify(body),
    }),
    env,
  );

  assert.equal(response.status, 204);
  const ping = writes.find((w) => /INSERT INTO pings/.test(w.sql));
  assert.ok(ping, "ping row written");
  // Unknown name filtered out, known ones kept in client order.
  assert.equal(ping.values[4], "claude,opencode");
});


function fakeDb(writes) {
  return {
    prepare(sql) {
      return {
        bind(...values) {
          writes.push({ sql, values });
          return { run: async () => ({ success: true }) };
        },
      };
    },
  };
}

test("open beacon v1 stores NULL for the week/month flags and geo", async () => {
  const writes = [];
  const env = { DB: fakeDb(writes) };
  const body = { schema_version: 1, version: "5.24.0", platform: "darwin" };
  const request = new Request("https://telemetry.example/v1/open", {
    method: "POST",
    headers: { "CF-Connecting-IP": "203.0.113.9" },
    body: JSON.stringify(body),
  });
  request.cf = { country: "US", region: "California" };

  const response = await worker.fetch(request, env);

  assert.equal(response.status, 204);
  const open = writes.find((w) => /INSERT INTO opens/.test(w.sql));
  assert.ok(open, "open row written");
  // received_at, version, platform, ip_hash, is_dev, first_this_week,
  // first_this_month, country, region
  assert.equal(open.values[5], null);
  assert.equal(open.values[6], null);
});

test("open beacon v2 persists week/month flags and sanitized geo", async () => {
  const writes = [];
  const env = { DB: fakeDb(writes) };
  const body = {
    schema_version: 2,
    version: "5.24.0",
    platform: "darwin",
    first_this_week: true,
    first_this_month: false,
  };
  const request = new Request("https://telemetry.example/v1/open", {
    method: "POST",
    body: JSON.stringify(body),
  });
  request.cf = { country: "us", region: "California", city: "Cupertino", postalCode: "95014" };

  const response = await worker.fetch(request, env);

  assert.equal(response.status, 204);
  const open = writes.find((w) => /INSERT INTO opens/.test(w.sql));
  assert.equal(open.values[5], 1);
  assert.equal(open.values[6], 0);
  // country is upper-cased; only country + region persisted, never city/zip.
  assert.equal(open.values[7], "US");
  assert.equal(open.values[8], "California");
  assert.doesNotMatch(JSON.stringify(writes), /Cupertino|95014/);
});

test("open beacon v2 requires the week/month booleans", async () => {
  const env = { DB: fakeDb([]) };
  const request = new Request("https://telemetry.example/v1/open", {
    method: "POST",
    body: JSON.stringify({ schema_version: 2, version: "5.24.0", platform: "darwin" }),
  });

  const response = await worker.fetch(request, env);
  assert.equal(response.status, 400);
});

test("open beacon geo sanitizer drops malformed or missing cf fields", async () => {
  const writes = [];
  const env = { DB: fakeDb(writes) };
  const body = { schema_version: 1, version: "5.24.0", platform: "darwin" };
  const request = new Request("https://telemetry.example/v1/open", {
    method: "POST",
    body: JSON.stringify(body),
  });
  request.cf = { country: "United States", region: "a".repeat(200) };

  const response = await worker.fetch(request, env);

  assert.equal(response.status, 204);
  const open = writes.find((w) => /INSERT INTO opens/.test(w.sql));
  assert.equal(open.values[7], null);
  assert.equal(open.values[8], null);
});

test("open beacon with absent request.cf stores null geo instead of throwing", async () => {
  const writes = [];
  const env = { DB: fakeDb(writes) };
  const request = new Request("https://telemetry.example/v1/open", {
    method: "POST",
    body: JSON.stringify({ schema_version: 1, version: "5.24.0", platform: "darwin" }),
  });

  const response = await worker.fetch(request, env);

  assert.equal(response.status, 204);
  const open = writes.find((w) => /INSERT INTO opens/.test(w.sql));
  assert.equal(open.values[7], null);
  assert.equal(open.values[8], null);
});

test("stats exposes weekly/monthly active installs and suppressed geo buckets", async () => {
  const queries = [];
  const env = {
    DB: {
      prepare(sql) {
        queries.push(sql);
        return {
          first: async () => {
            if (sql.includes("weekly_new_installs")) {
              return { weekly_new_installs: 3, weekly_new_installs_all: 4, monthly_new_installs: 10, monthly_new_installs_all: 11 };
            }
            if (sql.includes("SUM(CASE WHEN country = 'US'")) {
              return { us: 5, intl: 2 };
            }
            return { total_opens: 1, total_pings: 1, distinct_installs: 1, total_downloads: 1 };
          },
          all: async () => {
            if (sql.includes("GROUP BY country")) {
              return { results: [
                { country: "US", beacons: 5 },
                { country: "DE", beacons: 2 },
                { country: "FR", beacons: 1 },
              ] };
            }
            if (sql.includes("GROUP BY region")) {
              return { results: [
                { region: "California", beacons: 4 },
                { region: "Ohio", beacons: 1 },
              ] };
            }
            return { results: [] };
          },
        };
      },
    },
  };

  const response = await worker.fetch(new Request("https://telemetry.example/v1/stats"), env);
  const payload = await response.json();

  assert.equal(response.status, 200);
  assert.equal(payload.totals.weekly_new_installs, 3);
  assert.equal(payload.totals.monthly_new_installs, 10);
  assert.deepEqual(payload.countries_7d, [
    { country: "US", beacons: 5 },
    { country: "other", beacons: 3 },
  ]);
  assert.deepEqual(payload.regions_7d, [
    { region: "California", beacons: 4 },
    { region: "other", beacons: 1 },
  ]);
  assert.deepEqual(payload.us_vs_intl_7d, { us: 5, intl: 2 });
});
