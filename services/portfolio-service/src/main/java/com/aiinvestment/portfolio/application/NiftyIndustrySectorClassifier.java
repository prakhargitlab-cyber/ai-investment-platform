package com.aiinvestment.portfolio.application;

import org.springframework.stereotype.Component;
import java.util.Map;

/** Exact, reviewable mapping from official Nifty industry labels to the UI taxonomy. */
@Component
public class NiftyIndustrySectorClassifier {
    private static final Map<String, String> SECTORS = Map.ofEntries(
            Map.entry("Automobile and Auto Components", "Consumer Discretionary"),
            Map.entry("Capital Goods", "Industrials"),
            Map.entry("Chemicals", "Materials"),
            Map.entry("Construction", "Industrials"),
            Map.entry("Construction Materials", "Materials"),
            Map.entry("Consumer Durables", "Consumer Discretionary"),
            Map.entry("Consumer Services", "Consumer Discretionary"),
            Map.entry("Fast Moving Consumer Goods", "Consumer Staples"),
            Map.entry("Financial Services", "Financials"),
            Map.entry("Healthcare", "Healthcare"),
            Map.entry("Information Technology", "Technology"),
            Map.entry("Media Entertainment & Publication", "Communication Services"),
            Map.entry("Metals & Mining", "Materials"),
            Map.entry("Oil Gas & Consumable Fuels", "Energy"),
            Map.entry("Power", "Utilities"),
            Map.entry("Realty", "Real Estate"),
            Map.entry("Telecommunication", "Communication Services"),
            Map.entry("Textiles", "Consumer Discretionary")
    );

    public String classify(String officialIndustry) {
        return officialIndustry == null ? null : SECTORS.get(officialIndustry.trim());
    }

    public Map<String, String> mappings() { return SECTORS; }
}
