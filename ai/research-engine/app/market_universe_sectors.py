"""Durable, provider-free canonical sector discovery."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from app.sector_leaderboard import normalize_sector


def canonical_sector_counts(
    instruments: Iterable[dict],
    sector_by_instrument: dict[str, str | None],
) -> list[dict[str, object]]:
    """Count unique durable instruments by the backend's canonical sector label."""
    identities_by_sector: dict[str, set[str]] = defaultdict(set)
    labels: dict[str, str] = {}

    for instrument in instruments:
        identity = str(instrument.get("globalInstrumentId") or "").strip()
        raw_sector = sector_by_instrument.get(identity)
        if not identity or not raw_sector or not str(raw_sector).strip():
            continue
        normalized, label = normalize_sector(str(raw_sector))
        identities_by_sector[normalized].add(identity)
        labels.setdefault(normalized, label)

    return [
        {"name": labels[key], "instrumentCount": len(identities_by_sector[key])}
        for key in sorted(identities_by_sector, key=lambda value: (labels[value].casefold(), labels[value]))
    ]
