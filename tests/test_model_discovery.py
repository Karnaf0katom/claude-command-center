"""Vendor-doc model discovery: parsers, supersession, discovered-rate merge."""

import json
import sqlite3
import tempfile
import unittest
from unittest import mock

import server
from ccc_server import model_discovery as md
from ccc_server.usage_db import pricing, schema

OVERVIEW = """
## Compare models

| Feature | Claude Opus 5.5 | Claude Sonnet 5.5 | Claude Haiku 4.5 |
| :-- | :-- | :-- | :-- |
| Description | Agentic coding | Speed and intelligence | Fastest |
| [Pricing](https://x) | $4 / input MTok, $20 / output MTok | $2 / input MTok, $10 / output MTok | $1 / input MTok, $5 / output MTok |
| [Default effort](https://x) | `medium` | `high` | Not supported |
| [Context window](https://x) | 1M tokens | 1M tokens | 200K tokens |
| Max output | 128K tokens | 128K tokens | 64K tokens |
| [Retirement](https://x) | Not sooner than September 22, 2027 | Not sooner than September 28, 2027 | Not sooner than October 15, 2026 |
| Claude API alias | `claude-opus-5-5` | `claude-sonnet-5-5` | `claude-haiku-4-5` |

## Next
"""

CLAUDE_PRICING = """
## Model pricing

| Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |
| :-- | :-- | :-- | :-- | :-- | :-- |
| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok<sup>2</sup> | $20 / MTok |
| Claude Mythos 5.1 ([limited availability](https://x)) | $10 / MTok | $12.50 / MTok | $20 / MTok | $0.25 / MTok | $50 / MTok |
| Claude Opus 4 ([retired](https://x)) | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.50 / MTok | $75 / MTok |
| Claude Sonnet 5.5 | $2 / MTok | $2.50 / MTok | $4 / MTok | $0.20 / MTok | $10 / MTok |

## Batch

| Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |
| :-- | :-- | :-- | :-- | :-- | :-- |
| Claude Sonnet 5.5 | $1 / MTok | $1.25 / MTok | $2 / MTok | $0.10 / MTok | $5 / MTok |
"""

OPENAI_PRICING = """
### Standard pricing data

| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-6-sol | $2.00 | $0.20 | $2.50 | $10.00 | $4.00 | $0.40 | $5.00 | $15.00 |
| gpt-5.5 (<272K context length) | $5.00 | $0.50 | - | $30.00 | $10.00 | $1.00 | - | $45.00 |

### Batch pricing data

| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-6-sol | $1.00 | $0.10 | $1.25 | $5.00 | $2.00 | $0.20 | $2.50 | $7.50 |
"""


class TestClaudeParsers(unittest.TestCase):
    def test_overview_extracts_limits_price_effort_and_dates(self):
        rows = {r["id"]: r for r in md.parse_claude_overview(OVERVIEW)}
        self.assertEqual(list(rows), ["opus-5-5", "sonnet-5-5", "haiku-4-5"])
        opus = rows["opus-5-5"]
        self.assertEqual((opus["input_per_mtok"], opus["output_per_mtok"]), (4.0, 20.0))
        self.assertEqual(opus["default_reasoning_effort"], "medium")
        self.assertEqual(rows["sonnet-5-5"]["default_reasoning_effort"], "high")
        self.assertNotIn("default_reasoning_effort", rows["haiku-4-5"])
        self.assertEqual(opus["max_context_tokens"], 1_000_000)
        self.assertEqual(rows["haiku-4-5"]["max_output_tokens"], 64_000)
        self.assertEqual(opus["retires_not_before"], "2027-09-22")
        self.assertEqual(opus["released_at"], "2026-09-22")
        self.assertEqual(rows["haiku-4-5"]["released_at"], "2025-10-15")

    def test_pricing_uses_first_table_and_skips_retired_and_gated(self):
        rates = md.parse_claude_pricing(CLAUDE_PRICING)
        self.assertEqual(sorted(rates), ["opus-5-5", "sonnet-5-5"])
        self.assertEqual(rates["sonnet-5-5"]["input"], 2.0)  # not the Batch table's 1.0
        self.assertEqual(rates["opus-5-5"]["cache_read"], 0.2)
        self.assertEqual(rates["opus-5-5"]["cache_write_1h"], 8.0)

    def test_effort_support(self):
        text = "Claude Opus 5.5 supports all five effort levels, and medium is the default."
        self.assertEqual(
            md.parse_claude_effort_support(text)["opus-5-5"],
            ["low", "medium", "high", "xhigh", "max"],
        )


class TestOpenAIParser(unittest.TestCase):
    def test_first_table_wins_and_context_suffix_stripped(self):
        rates = md.parse_openai_pricing(OPENAI_PRICING)
        self.assertEqual(rates["gpt-6-sol"]["input"], 2.0)
        self.assertEqual(rates["gpt-6-sol"]["cache_write"], 2.5)
        self.assertEqual(rates["gpt-5.5"]["output"], 30.0)
        self.assertNotIn("cache_write", rates["gpt-5.5"])


class TestSupersession(unittest.TestCase):
    def test_claude_older_version_of_same_family_is_flagged(self):
        entries = [{"id": i} for i in ("sonnet-5", "sonnet-5-5", "opus-5-5", "opus-4-8", "haiku-4-5")]
        md.mark_superseded("claude", entries)
        flagged = {e["id"]: e.get("superseded_by") for e in entries}
        self.assertEqual(flagged["sonnet-5"], "sonnet-5-5")
        self.assertEqual(flagged["opus-4-8"], "opus-5-5")
        self.assertIsNone(flagged["sonnet-5-5"])
        self.assertIsNone(flagged["haiku-4-5"])

    def test_codex_tier_families_and_vendor_declared_successor(self):
        entries = [{"id": i} for i in ("gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5", "gpt-6-astra")]
        entries[3]["upgrade_to"] = "gpt-5.6-sol"
        md.mark_superseded("codex", entries)
        flagged = {e["id"]: e.get("superseded_by") for e in entries}
        self.assertEqual(flagged["gpt-5.6-sol"], "gpt-6-sol")
        self.assertEqual(flagged["gpt-5.5"], "gpt-5.6-sol")
        self.assertIsNone(flagged["gpt-5.6-terra"])  # no newer terra exists
        self.assertIsNone(flagged["gpt-6-sol"])

    def test_unrecognized_ids_are_never_flagged(self):
        entries = [{"id": "gpt-reserve"}, {"id": "o3"}]
        md.mark_superseded("codex", entries)
        self.assertFalse(any(e.get("deprioritized") for e in entries))


class TestDiscoveredRates(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        schema.migrate(conn)
        return conn

    def _write(self, td, rows):
        path = f"{td}/discovered-rates.json"
        with open(path, "w") as fh:
            json.dump({"rates": rows}, fh)
        return path

    def test_new_model_backfills_and_price_change_adds_dated_row(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO price_rates (provider, pricing_key, effective_from, input_rate, cache_read_rate, "
            "cache_write_5m_rate, cache_write_1h_rate, output_rate) VALUES "
            "('anthropic','claude-opus-5-5','1970-01-01',5,0.5,6.25,10,25)"
        )
        rows = [
            {"provider": "anthropic", "pricing_key": "claude-opus-5-5", "input": 4.0, "cache_read": 0.2,
             "cache_write_5m": 5.0, "cache_write_1h": 8.0, "output": 20.0},
            {"provider": "anthropic", "pricing_key": "claude-sonnet-5-5", "input": 2.0, "cache_read": 0.2,
             "cache_write_5m": 2.5, "cache_write_1h": 4.0, "output": 10.0},
        ]
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, rows)
            self.assertEqual(pricing.load_discovered_rates(conn, path, today="2026-09-29"), 2)
            # Idempotent: nothing changed, nothing written.
            self.assertEqual(pricing.load_discovered_rates(conn, path, today="2026-09-30"), 0)
        got = {
            (k, f): i for k, f, i in conn.execute(
                "SELECT pricing_key, effective_from, input_rate FROM price_rates"
            )
        }
        self.assertEqual(got[("claude-opus-5-5", "1970-01-01")], 5)  # history kept
        self.assertEqual(got[("claude-opus-5-5", "2026-09-29")], 4.0)
        self.assertEqual(got[("claude-sonnet-5-5", "1970-01-01")], 2.0)


class TestCatalogIntegration(unittest.TestCase):
    def test_codex_cli_only_model_is_offered_and_older_tier_pruned(self):
        cache_rows = [
            {"id": "gpt-6-sol", "label": "GPT-6-Sol", "source": "codex-cache", "priority": 2,
             "default_reasoning_effort": "medium", "reasoning_efforts": ["low", "medium", "high"]},
            {"id": "gpt-5.6-sol", "label": "GPT-5.6-Sol", "source": "codex-cache", "priority": 4,
             "default_reasoning_effort": "low", "reasoning_efforts": ["low", "medium", "high"]},
        ]
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(server, "_codex_models_cache_records", return_value=cache_rows), \
             mock.patch.object(server, "_OPENAI_PRICING_CATALOG_FILE", server.Path(td) / "openai-pricing.json"), \
             mock.patch.object(server, "_harness_model_list_result", return_value={"available": False, "records": []}), \
             mock.patch.object(server, "_load_claude_model_catalog_records", return_value=[]), \
             mock.patch.object(server, "_observed_model_records", return_value=[]), \
             mock.patch.object(server, "_codex_configured_model", return_value=""), \
             mock.patch.object(server, "_antigravity_cli_configured_model", return_value=""):
            (server.Path(td) / "openai-pricing.json").write_text(json.dumps(
                {"pricing": {"gpt-6-sol": {"input": 2.0, "output": 10.0}}}
            ))
            payload = server._build_engine_model_catalog(force_refresh=True)
        server._MODEL_CATALOG_CACHE["ts"] = 0.0
        ids = payload["engines"]["codex"]
        self.assertIn("gpt-6-sol", ids)
        self.assertNotIn("gpt-5.6-sol", ids)
        sol = next(m for m in payload["catalog"]["codex"]["models"] if m["id"] == "gpt-6-sol")
        self.assertEqual(sol["cost_summary"], "$2.00 in / 1M, $10.00 out / 1M")
        self.assertEqual(sol["default_reasoning_effort"], "medium")


if __name__ == "__main__":
    unittest.main()
