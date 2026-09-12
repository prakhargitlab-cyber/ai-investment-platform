"""Pure, provider-neutral sector leaderboard projection."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable


def normalize_sector(value: str) -> tuple[str, str]:
    key = " ".join(str(value).casefold().split())
    aliases = {"financial services": ("financials", "Financials"), "financials": ("financials", "Financials"), "consumer cyclical": ("consumer-discretionary", "Consumer Discretionary"), "consumer discretionary": ("consumer-discretionary", "Consumer Discretionary"), "health care": ("healthcare", "Healthcare"), "healthcare": ("healthcare", "Healthcare")}
    return aliases.get(key, (key.replace(" ", "-"), value.strip().title()))


def build_sector_leaderboard(candidates: Iterable[dict]) -> dict:
    # Candidate selection happens by authoritative global identity before a
    # duplicate can occupy a ranked sector slot.
    winners: dict[str, dict] = {}
    for candidate in candidates:
        if not candidate.get("globalInstrumentId") or not candidate.get("sector") or candidate.get("score") is None:
            continue
        identity = str(candidate["globalInstrumentId"])
        prior = winners.get(identity)
        key = (-candidate["score"], -int(candidate.get("evidenceCoverage") or 0), str(candidate.get("ticker") or "").upper(), identity)
        if prior is None or key < (-prior["score"], -int(prior.get("evidenceCoverage") or 0), str(prior.get("ticker") or "").upper(), identity):
            winners[identity] = dict(candidate)
    grouped: dict[str, list[dict]] = defaultdict(list)
    labels: dict[str, str] = {}
    for candidate in winners.values():
        normalized, label = normalize_sector(candidate["sector"]); grouped[normalized].append(candidate); labels.setdefault(normalized, label)
    sectors=[]
    for normalized in sorted(grouped):
        stocks = sorted(grouped[normalized], key=lambda c: (-c["score"], -int(c.get("evidenceCoverage") or 0), str(c.get("ticker") or "").upper(), str(c["globalInstrumentId"])))[:5]
        sectors.append({"sector": labels[normalized], "normalizedSector": normalized, "stocks": stocks})
    return {"sectors": sectors}
