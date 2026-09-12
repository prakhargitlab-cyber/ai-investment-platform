package com.aiinvestment.portfolio.application;

import org.junit.jupiter.api.Test;
import java.util.Map;
import static org.assertj.core.api.Assertions.assertThat;

class NiftyIndustrySectorClassifierTest {
    private final NiftyIndustrySectorClassifier classifier = new NiftyIndustrySectorClassifier();

    @Test void current_official_domain_has_explicit_reviewed_outcomes() {
        Map<String,String> expected=Map.ofEntries(
            Map.entry("Automobile and Auto Components","Consumer Discretionary"),Map.entry("Capital Goods","Industrials"),
            Map.entry("Chemicals","Materials"),Map.entry("Construction","Industrials"),Map.entry("Construction Materials","Materials"),
            Map.entry("Consumer Durables","Consumer Discretionary"),Map.entry("Consumer Services","Consumer Discretionary"),
            Map.entry("Fast Moving Consumer Goods","Consumer Staples"),Map.entry("Financial Services","Financials"),
            Map.entry("Healthcare","Healthcare"),Map.entry("Information Technology","Technology"),
            Map.entry("Media Entertainment & Publication","Communication Services"),Map.entry("Metals & Mining","Materials"),
            Map.entry("Oil Gas & Consumable Fuels","Energy"),Map.entry("Power","Utilities"),Map.entry("Realty","Real Estate"),
            Map.entry("Telecommunication","Communication Services"),Map.entry("Textiles","Consumer Discretionary"));
        assertThat(classifier.mappings()).containsExactlyInAnyOrderEntriesOf(expected);
        expected.forEach((industry,sector)->assertThat(classifier.classify(industry)).isEqualTo(sector));
    }

    @Test void ambiguous_and_future_values_are_unmapped_without_fuzzy_matching() {
        assertThat(classifier.classify("Diversified")).isNull();
        assertThat(classifier.classify("Services")).isNull();
        assertThat(classifier.classify("Financial Services Platform")).isNull();
        assertThat(classifier.classify("Information Technology Services")).isNull();
        assertThat(classifier.classify("Future Industry")).isNull();
    }
}
