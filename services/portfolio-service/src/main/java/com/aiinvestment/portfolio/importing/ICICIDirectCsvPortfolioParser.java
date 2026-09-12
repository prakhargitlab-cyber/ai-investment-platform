package com.aiinvestment.portfolio.importing;

import com.aiinvestment.shared.domain.broker.BrokerType;
import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVRecord;
import org.springframework.stereotype.Component;

import java.io.ByteArrayInputStream;
import java.io.InputStreamReader;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

@Component
public class ICICIDirectCsvPortfolioParser implements PortfolioImportParser {
    private static final Pattern FILENAME = Pattern.compile(
            "^(.+?)_PortFolioEqtSummary(?: \\(\\d+\\))?\\.csv$", Pattern.CASE_INSENSITIVE);
    private static final List<String> REQUIRED_HEADERS = List.of("Stock Symbol", "Company Name", "ISIN Code", "Qty",
            "Average Cost Price", "Current Market Price", "Value At Cost", "Value At Market Price",
            "Realized Profit / Loss", "Unrealized Profit/Loss", "Unrealized Profit/Loss %");

    @Override public BrokerType brokerType() { return BrokerType.ICICI_DIRECT; }
    @Override public boolean supported() { return true; }

    @Override
    public PortfolioImportParseResult parse(String filename, byte[] content, String requestedPortfolioName) {
        String account = deriveAccountName(filename, requestedPortfolioName);
        List<ImportedHolding> holdings = new ArrayList<>();
        List<String> issues = new ArrayList<>();
        int rows = 0;
        try (var reader = new InputStreamReader(new ByteArrayInputStream(content), StandardCharsets.UTF_8)) {
            var format = CSVFormat.DEFAULT.builder().setHeader().setSkipHeaderRecord(true)
                    .setIgnoreEmptyLines(true).setTrim(true).setAllowMissingColumnNames(true).build();
            var records = format.parse(reader);
            for (String header : REQUIRED_HEADERS) {
                if (!records.getHeaderMap().containsKey(header)) {
                    throw new IllegalArgumentException("Unsupported ICICI CSV: missing column " + header);
                }
            }
            for (CSVRecord record : records) {
                rows++;
                try {
                    String isin = normalizeIsin(record.get("ISIN Code"));
                    BigDecimal quantity = requiredNumber(record.get("Qty"), "Qty");
                    BigDecimal averageCost = requiredNumber(record.get("Average Cost Price"), "Average Cost Price");
                    String companyName = requiredText(record.get("Company Name"), "Company Name");
                    String symbol = nullableText(record.get("Stock Symbol"));
                    holdings.add(new ImportedHolding("ISIN:" + isin, isin, symbol, companyName, quantity, null, averageCost,
                            number(record.get("Current Market Price")), number(record.get("Value At Cost")),
                            number(record.get("Value At Market Price")), number(record.get("Realized Profit / Loss")),
                            number(record.get("Unrealized Profit/Loss")), number(record.get("Unrealized Profit/Loss %")),
                            null, null, "INR"));
                } catch (IllegalArgumentException exception) {
                    issues.add("Row " + record.getRecordNumber() + ": " + exception.getMessage());
                }
            }
        } catch (IllegalArgumentException exception) {
            throw exception;
        } catch (Exception exception) {
            throw new IllegalArgumentException("Unable to parse ICICI portfolio CSV", exception);
        }
        return new PortfolioImportParseResult(brokerType(), "ICICI_DIRECT_PORTFOLIO_EQT_SUMMARY_V1", account, rows,
                List.copyOf(holdings), List.copyOf(issues), List.of("Company Name", "ISIN", "Symbol", "Quantity",
                "Average Cost", "Imported Market Price"), null);
    }

    public String deriveAccountName(String filename, String requestedName) {
        Matcher matcher = FILENAME.matcher(filename);
        if (matcher.matches() && safeName(matcher.group(1))) return matcher.group(1).trim();
        if (safeName(requestedName)) return requestedName.trim();
        throw new IllegalArgumentException("A portfolio name is required because the filename does not identify an ICICI account");
    }

    static BigDecimal number(String raw) {
        if (raw == null || raw.isBlank()) return null;
        String value = raw.trim().replace(",", "").replaceAll("\\s+", "");
        boolean parentheses = value.startsWith("(") && value.endsWith(")");
        if (parentheses) value = value.substring(1, value.length() - 1);
        try {
            BigDecimal parsed = new BigDecimal(value);
            return parentheses ? parsed.negate() : parsed;
        } catch (NumberFormatException exception) {
            throw new IllegalArgumentException("Malformed numeric value");
        }
    }

    private static BigDecimal requiredNumber(String raw, String field) {
        BigDecimal value = number(raw);
        if (value == null) throw new IllegalArgumentException(field + " is required");
        return value;
    }

    private static String normalizeIsin(String raw) {
        String isin = requiredText(raw, "ISIN").replaceAll("\\s+", "").toUpperCase(Locale.ROOT);
        if (!isin.matches("[A-Z]{2}[A-Z0-9]{9}[0-9]")) throw new IllegalArgumentException("ISIN is malformed");
        return isin;
    }

    private static String requiredText(String raw, String field) {
        String value = nullableText(raw);
        if (value == null) throw new IllegalArgumentException(field + " is required");
        return value;
    }

    private static String nullableText(String raw) { return raw == null || raw.isBlank() ? null : raw.trim(); }
    private static boolean safeName(String value) {
        return value != null && value.trim().matches("[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}");
    }
}
