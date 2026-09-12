from app.sector_leaderboard import build_sector_leaderboard, normalize_sector

def stock(identifier, score, sector="Technology", ticker=None, coverage=1): return {"globalInstrumentId": str(identifier), "ticker": ticker or f"T{identifier}", "sector": sector, "score": score, "evidenceCoverage": coverage}
def test_top_five_normalization_and_deterministic_duplicates():
    values=[stock(i, i, " technology " if i % 2 else "Technology") for i in range(7)]
    values += [stock("1", 99, "Technology", "WIN", 3), stock("x", 4, "Financial Services"), stock("y", 3, "Financials")]
    result=build_sector_leaderboard(values)
    tech=next(x for x in result["sectors"] if x["normalizedSector"] == "technology")
    assert len(tech["stocks"]) == 5 and tech["stocks"][0]["ticker"] == "WIN" and len({x["globalInstrumentId"] for x in tech["stocks"]}) == 5
    assert build_sector_leaderboard(reversed(values)) == result
def test_sector_aliases_and_empty_are_safe():
    assert normalize_sector(" Health Care ") == ("healthcare", "Healthcare")
    assert normalize_sector("Consumer Cyclical") == ("consumer-discretionary", "Consumer Discretionary")
    assert build_sector_leaderboard([{"globalInstrumentId":"x","score":1}]) == {"sectors": []}

def test_provider_neutral_eligibility_and_sector_sizes():
    values = [stock("nse", 8, "Technology", "NSE", 2), stock("sec", 7, "technology", "SEC", 1), stock("eodhd", 6, "Technology", "EOD", 1)]
    values += [stock("f", 9, "Financial Services"), stock("g", 8, "Financials"), {"globalInstrumentId":"missing-sector", "score":99}, {"globalInstrumentId":"missing-score", "sector":"Technology"}]
    result = build_sector_leaderboard(values)
    technology = next(group for group in result["sectors"] if group["normalizedSector"] == "technology")
    financials = next(group for group in result["sectors"] if group["normalizedSector"] == "financials")
    assert [item["globalInstrumentId"] for item in technology["stocks"]] == ["nse", "sec", "eodhd"]
    assert len(financials["stocks"]) == 2
