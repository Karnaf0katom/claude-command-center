"""Vendor-doc model discovery: parse published tables into catalog metadata.

Pure functions only (no I/O), so every parser is unit-testable against a
markdown fixture. ``server.py`` owns fetching, caching and catalog wiring.

Sources (all public, machine-readable markdown):
  * Anthropic models overview: aliases, context/output limits, default effort,
    list pricing, retirement date.
  * Anthropic pricing page: cache read/write rates for every non-retired model.
  * Anthropic effort page: which models support the full effort range.
  * OpenAI pricing page: per-slug rates for the Codex model family.

A new model that a vendor publishes in these tables shows up in the catalog on
the next hourly refresh with no code change.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

ANTHROPIC_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _clean(value) -> str:
    value = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", str(value or ""))
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"[*_`]", "", value).strip()


def _table_rows(text: str) -> List[List[str]]:
    rows = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            rows.append([c.strip() for c in stripped.strip("|").split("|")])
    return rows


def _first_table(text: str, *needles: str) -> List[List[str]]:
    """Rows of the first contiguous table whose header row has every needle.

    Pricing pages repeat the same header for Batch/Flex/Priority tables, so
    reading past the first (Standard) table would overwrite it with other tiers.
    """
    block: List[List[str]] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            block.append([c.strip() for c in stripped.strip("|").split("|")])
            continue
        if block and all(
            n in " ".join(block[0]).lower() for n in needles
        ):
            return block
        block = []
    if block and all(n in " ".join(block[0]).lower() for n in needles):
        return block
    return []


def _money(cell) -> Optional[float]:
    m = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", str(cell or ""))
    return float(m.group(1)) if m else None


def _tokens(cell) -> Optional[int]:
    m = re.search(r"([0-9][0-9,.]*)\s*([KkMm])\b", str(cell or ""))
    if not m:
        return None
    number = float(m.group(1).replace(",", ""))
    return int(number * (1_000_000 if m.group(2).lower() == "m" else 1_000))


def _long_date(cell) -> Optional[date]:
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})", str(cell or ""))
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date()
    except ValueError:
        return None


def claude_alias_from_display_name(name: str) -> Optional[str]:
    """``Claude Sonnet 5.5`` -> ``sonnet-5-5`` (None for non-model rows)."""
    name = re.sub(r"\(.*?\)", "", _clean(name)).strip()
    m = re.fullmatch(r"Claude\s+([A-Za-z]+)\s+(\d+(?:\.\d+)?)", name)
    if not m:
        return None
    return f"{m.group(1).lower()}-{m.group(2).replace('.', '-')}"


def parse_claude_overview(markdown: str) -> List[dict]:
    """Rich records from the "Compare models" table (superset of the alias list)."""
    text = str(markdown or "")
    section = re.search(
        r"(?ims)^#{2,3}\s+(?:Latest models comparison|Compare models)\s*$([\s\S]*?)(?=^#{2,3}\s+|\Z)",
        text,
    )
    if not section:
        return []
    rows = _table_rows(section.group(1))
    if len(rows) < 3:
        return []
    header = rows[0]
    by_label = {}
    for row in rows[1:]:
        by_label.setdefault(_clean(row[0]).lower(), row)
    aliases = by_label.get("claude api alias")
    if not aliases:
        return []

    def cell(label, index):
        row = by_label.get(label)
        return _clean(row[index]) if row and index < len(row) else ""

    records = []
    for index in range(1, min(len(header), len(aliases))):
        alias = _clean(aliases[index])
        if not re.fullmatch(r"claude-[a-z0-9][a-z0-9.-]*", alias):
            continue
        context = cell("context window", index)
        context_key = context.lower().replace(",", "").replace(" ", "")
        pricing = cell("pricing", index)
        prices = re.findall(r"\$\s*([0-9]+(?:\.[0-9]+)?)", pricing)
        default_effort = cell("default effort", index).lower()
        retires = _long_date(cell("retirement", index))
        released = None
        if retires:
            try:
                released = retires.replace(year=retires.year - 1)
            except ValueError:  # Feb 29
                released = retires.replace(year=retires.year - 1, day=28)
        record = {
            "id": alias.removeprefix("claude-"),
            "label": alias.removeprefix("claude-"),
            "display_name": _clean(header[index]),
            "oneM": "1mtoken" in context_key or "1000000token" in context_key,
            "source": "anthropic-models-overview",
        }
        description = cell("description", index)
        if description:
            record["description"] = description
        if _tokens(context):
            record["max_context_tokens"] = _tokens(context)
        if _tokens(cell("max output", index)):
            record["max_output_tokens"] = _tokens(cell("max output", index))
        if len(prices) >= 2:
            record["input_per_mtok"] = float(prices[0])
            record["output_per_mtok"] = float(prices[1])
        if default_effort in ANTHROPIC_EFFORT_LEVELS:
            record["default_reasoning_effort"] = default_effort
        if retires:
            record["retires_not_before"] = retires.isoformat()
        if released:
            # The overview publishes "not sooner than <launch + 12 months>";
            # the docs carry no launch date, so this is an approximation.
            record["released_at"] = released.isoformat()
            record["released_at_source"] = "retirement-minus-12-months"
        records.append(record)
    return records


def parse_claude_pricing(markdown: str) -> Dict[str, dict]:
    """alias -> {input, cache_write_5m, cache_write_1h, cache_read, output} ($/MTok)."""
    rows = _first_table(markdown, "base input tokens", "output tokens")
    out: Dict[str, dict] = {}
    for row in rows[1:]:
        if len(row) < 6 or set(row[0]) <= set(":- "):
            continue
        name = _clean(row[0])
        lowered = name.lower()
        if "retired" in lowered or "limited availability" in lowered:
            continue
        alias = claude_alias_from_display_name(name)
        if not alias:
            continue
        vals = [_money(c) for c in row[1:6]]
        if any(v is None for v in vals):
            continue
        out[alias] = {
            "input": vals[0], "cache_write_5m": vals[1], "cache_write_1h": vals[2],
            "cache_read": vals[3], "output": vals[4],
        }
    return out


def parse_claude_effort_support(markdown: str) -> Dict[str, List[str]]:
    """alias -> full effort list, for models the effort page says take all five."""
    out: Dict[str, List[str]] = {}
    for m in re.finditer(
        r"Claude\s+([A-Za-z]+\s+\d+(?:\.\d+)?)\s+supports\s+all\s+five\s+effort\s+levels",
        str(markdown or ""),
    ):
        alias = claude_alias_from_display_name("Claude " + m.group(1))
        if alias:
            out[alias] = list(ANTHROPIC_EFFORT_LEVELS)
    return out


def parse_openai_pricing(markdown: str) -> Dict[str, dict]:
    """slug -> {input, cache_read, cache_write, output, long_input, long_output} ($/MTok)."""
    rows = _first_table(markdown, "short context input", "short context output")
    out: Dict[str, dict] = {}
    if not rows:
        return out
    cols = [c.lower() for c in rows[0]]

    def col(name):
        return cols.index(name) if name in cols else None

    idx = {
        "input": col("short context input"),
        "cache_read": col("short context cached input"),
        "cache_write": col("short context cache writes"),
        "output": col("short context output"),
        "long_input": col("long context input"),
        "long_output": col("long context output"),
    }
    for row in rows[1:]:
        if len(row) != len(cols) or set(row[0]) <= set(":- "):
            continue
        slug = re.sub(r"\s*\(.*?\)\s*$", "", _clean(row[0])).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", slug):
            continue
        rec = {k: (_money(row[i]) if i is not None else None) for k, i in idx.items()}
        if rec["input"] is None or rec["output"] is None:
            continue
        out[slug] = {k: v for k, v in rec.items() if v is not None}
    return out


# ---------------------------------------------------------------------------
# Family / supersession: never offer an older model of a family once a newer
# one exists (sonnet-5 once sonnet-5-5 ships).
# ---------------------------------------------------------------------------

def model_family_version(engine: str, model_id: str) -> Optional[Tuple[str, Tuple[int, ...]]]:
    """(family, version tuple) or None when the id has no recognizable scheme."""
    mid = str(model_id or "").strip().lower()
    if engine == "claude":
        m = re.match(r"^(?:claude-)?(fable|mythos|opus|sonnet|haiku)-(\d+(?:-\d+)?)(?![\d])", mid)
        if not m:
            return None
        return m.group(1), tuple(int(p) for p in m.group(2).split("-"))
    if engine == "codex":
        m = re.match(r"^gpt-(\d+(?:\.\d+)?)(?:-(.+))?$", mid)
        if not m:
            return None
        return m.group(2) or "", tuple(int(p) for p in m.group(1).split("."))
    return None


def mark_superseded(engine: str, entries: List[dict]) -> None:
    """Annotate catalog entries in place with ``deprioritized`` / ``superseded_by``.

    Rules, in order:
      1. Vendor-declared successor (``upgrade_to``, e.g. Codex CLI metadata).
      2. Same family, lower version than another entry in the list.
    Entries are never removed: an older model stays callable and stays in the
    catalog, it is just flagged so pickers can hide it.
    """
    present = {str(e.get("id") or "").lower(): e for e in entries}
    best: Dict[str, Tuple[Tuple[int, ...], dict]] = {}
    for entry in entries:
        fv = model_family_version(engine, entry.get("id"))
        if not fv:
            continue
        family, version = fv
        if family not in best or version > best[family][0]:
            best[family] = (version, entry)
    for entry in entries:
        successor = None
        declared = str(entry.get("upgrade_to") or "").strip()
        if declared and declared.lower() in present and declared.lower() != str(entry.get("id")).lower():
            successor = present[declared.lower()].get("id")
        if successor is None:
            fv = model_family_version(engine, entry.get("id"))
            if fv and fv[0] in best:
                top_version, top = best[fv[0]]
                if top_version > fv[1]:
                    successor = top.get("id")
        if successor:
            entry["deprioritized"] = True
            entry["superseded_by"] = successor


def rate_row(provider: str, pricing_key: str, rates: dict, note: str) -> dict:
    """A ``rates.json``-shaped row from a discovered $/MTok dict."""
    row = {
        "provider": provider,
        "pricing_key": pricing_key,
        "input": rates.get("input"),
        "cache_read": rates.get("cache_read"),
        "cache_write_5m": rates.get("cache_write_5m", rates.get("cache_write")),
        "cache_write_1h": rates.get("cache_write_1h"),
        "output": rates.get("output"),
        "source_note": note,
        "verified_at": None,
    }
    return row
