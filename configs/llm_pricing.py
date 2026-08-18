"""
LLM pricing table — token usage → USD.

Used by ``services.gather_service`` to settle the cost of a chat turn at the
moment it is written, so the stored figure never has to be recomputed later.

Why cost is frozen at write time
--------------------------------
Rates change. Recomputing historic usage with today's rates silently rewrites
past spend, and the numbers stop reconciling with anything. Every record
therefore stores BOTH the computed cost and the rates that produced it
(see ``cost_for`` → ``rate_input_per_mtok`` / ``rate_output_per_mtok``), so the
figure stays reproducible and auditable after a price change.

Editing rates
-------------
Bump ``PRICING_VERSION`` whenever a rate changes. Old records keep the rates
they were written with; only new records pick up the new table.

⚠️ These are Anthropic public list prices. Traffic actually goes through an
Intel-internal gateway (``key.gnaigpt_url``), so the real internal chargeback
rate may differ — correct the table below once the internal rate is confirmed.
Nothing else needs to change.
"""

from __future__ import annotations

from typing import Any, Optional

# Bump on every rate change (stored alongside each computed cost).
#
# Deliberately NOT bumped when MODEL_ALIASES changed on 2026-08-18: no rate
# moved, only the set of names that resolve to one. Bumping would imply the
# numbers were produced by a different rate table and make a re-priced record
# look inconsistent with one priced correctly the first time.
PRICING_VERSION = "2026-08-04"

# Multipliers applied to the INPUT rate for cached tokens. Anthropic bills a
# cache read at ~0.1x and a cache write at ~1.25x the normal input rate.
#
# NOTE: this application does not currently set `cache_control` anywhere, so
# prompt caching is off and these counters stay 0. The handling is here so that
# turning caching on later starts costing correctly with no further changes.
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = 1.25

# model id -> USD per 1M tokens.
# Keys are matched case-insensitively, exact first then longest-prefix, so a
# gateway that appends a suffix (e.g. "claude-sonnet-4-6-intel") still resolves.
RATES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00},
    "claude-haiku-4-5":  {"input": 1.00, "output": 5.00},
    "gpt-4.1":           {"input": 2.00, "output": 8.00},
}


# Alternate spellings that must resolve to the same rates. Add irregular ones
# here; the regular family/version transposition is derived automatically below.
#
# This is not cosmetic. The gateway in production reports
# "claude-4-6-sonnet" while the table is keyed "claude-sonnet-4-6", and the
# mismatch left 481,001 tokens across 26 invocations unpriced between the v6
# rollout and 2026-08-18. `unpriced_model` made that visible rather than
# reporting the spend as zero, but the right fix is for the name to resolve.
MODEL_ALIASES: dict[str, str] = {}


def _transposed(key: str) -> Optional[str]:
    """``claude-sonnet-4-6`` -> ``claude-4-6-sonnet``, or None if not that shape."""
    parts = key.split("-")
    if len(parts) >= 4 and parts[0] == "claude":
        return "-".join(["claude", *parts[2:], parts[1]])
    return None


def _build_aliases() -> dict[str, str]:
    """Derive aliases from the rate table so the two can never drift apart.

    Deriving beats a hand-written second table: adding a model to
    RATES_PER_MTOK gives it the transposed spelling for free, and a rate change
    is still a single edit in one place.
    """
    out = dict(MODEL_ALIASES)
    for canonical in RATES_PER_MTOK:
        alt = _transposed(canonical)
        if alt and alt not in RATES_PER_MTOK:
            out.setdefault(alt, canonical)
    return out


_ALIASES = _build_aliases()


def resolve_rates(model: str) -> Optional[dict[str, float]]:
    """
    Return ``{"input": x, "output": y}`` per 1M tokens for ``model``.

    Matching is case-insensitive: exact, then alias, then longest-prefix over
    both, so a gateway that transposes the family name or appends a suffix
    ("claude-4-6-sonnet-20260514") still resolves.

    Returns None for an unknown model — callers must then record the token
    counts but leave cost unset. Guessing a rate would put a wrong number in
    the ledger, which is worse than a missing one.
    """
    key = str(model or "").strip().lower()
    if not key:
        return None
    if key in RATES_PER_MTOK:
        return RATES_PER_MTOK[key]
    if key in _ALIASES:
        return RATES_PER_MTOK[_ALIASES[key]]
    # Longest-prefix match so the most specific entry wins.
    best: Optional[str] = None
    for known in (*RATES_PER_MTOK, *_ALIASES):
        if key.startswith(known) and (best is None or len(known) > len(best)):
            best = known
    if best is None:
        return None
    return RATES_PER_MTOK[_ALIASES.get(best, best)]


def cost_for(model: str, usage: Any) -> Optional[dict]:
    """
    Settle ``usage`` into USD for ``model``.

    ``usage`` is a mapping (or object) carrying any of ``input_tokens``,
    ``output_tokens``, ``cache_read_tokens``, ``cache_write_tokens``.

    Returns a dict with the per-bucket breakdown, the total, and the rates used
    — or None when the model is unknown.
    """
    rates = resolve_rates(model)
    if rates is None:
        return None

    def _get(name: str) -> int:
        if isinstance(usage, dict):
            v = usage.get(name)
        else:
            v = getattr(usage, name, None)
        try:
            return max(0, int(v or 0))
        except (TypeError, ValueError):
            return 0

    rate_in = float(rates["input"])
    rate_out = float(rates["output"])

    # `input_tokens` is the UNCACHED remainder — cached tokens are reported
    # separately and must be priced with their own multipliers, not folded in.
    input_cost = _get("input_tokens") / 1_000_000 * rate_in
    cache_cost = (
        _get("cache_read_tokens") / 1_000_000 * rate_in * CACHE_READ_MULTIPLIER
        + _get("cache_write_tokens") / 1_000_000 * rate_in * CACHE_WRITE_MULTIPLIER
    )
    output_cost = _get("output_tokens") / 1_000_000 * rate_out

    return {
        "input": round(input_cost, 6),
        "cache": round(cache_cost, 6),
        "output": round(output_cost, 6),
        "total": round(input_cost + cache_cost + output_cost, 6),
        "pricing_version": PRICING_VERSION,
        "rate_input_per_mtok": rate_in,
        "rate_output_per_mtok": rate_out,
    }
