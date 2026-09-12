package com.aiinvestment.portfolio.importing;

import com.aiinvestment.shared.domain.broker.BrokerType;
import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVRecord;
import org.springframework.stereotype.Component;
import java.io.StringReader;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.regex.*;

@Component
public class HDFCSecuritiesCsvPortfolioParser implements PortfolioImportParser {
    private static final Pattern FILENAME = Pattern.compile(
            "^Invest Right Equity Portfolio_(\\d+)(?: \\(\\d+\\))?\\.csv$", Pattern.CASE_INSENSITIVE);
    private static final Pattern METADATA = Pattern.compile(
            "^Trading account:\\s*(\\d+)\\s*,\\s*Equity Portfolio Details as on\\s*(.+?)\\s*$",
            Pattern.CASE_INSENSITIVE);
    private static final DateTimeFormatter SNAPSHOT_TIME = DateTimeFormatter.ofPattern("dd/MM/uuuu HH:mm:ss");

    @Override public BrokerType brokerType() { return BrokerType.HDFC_SECURITIES; }
    @Override public boolean supported() { return true; }

    @Override
    public PortfolioImportParseResult parse(String filename, byte[] content, String requestedPortfolioName) {
        Matcher filenameMatch = FILENAME.matcher(filename);
        if (!filenameMatch.matches()) throw new IllegalArgumentException("Unsupported HDFC filename");
        String filenameAccount = filenameMatch.group(1);
        String text = new String(content, StandardCharsets.UTF_8).replace("\uFEFF", "");
        String[] lines = text.split("\\R", -1);
        if (lines.length < 4) throw new IllegalArgumentException("Unsupported HDFC CSV structure");
        Matcher metadataMatch = METADATA.matcher(lines[0].trim());
        if (!metadataMatch.matches()) throw new IllegalArgumentException("Unsupported HDFC account metadata");
        String metadataAccount = metadataMatch.group(1);
        if (!filenameAccount.equals(metadataAccount))
            throw new IllegalArgumentException("HDFC filename and metadata account references do not match");
        Instant snapshotAt;
        try {
            snapshotAt = LocalDateTime.parse(metadataMatch.group(2).trim(), SNAPSHOT_TIME)
                    .atZone(ZoneId.of("Asia/Kolkata")).toInstant();
        } catch (RuntimeException exception) {
            throw new IllegalArgumentException("HDFC snapshot timestamp is malformed");
        }
        if (!lines[1].isBlank()) throw new IllegalArgumentException("Unsupported HDFC CSV structure");

        List<ImportedHolding> holdings = new ArrayList<>();
        List<String> issues = new ArrayList<>();
        int rows = 0;
        try {
            String csv = String.join(System.lineSeparator(), Arrays.copyOfRange(lines, 2, lines.length));
            var records = CSVFormat.DEFAULT.builder().setHeader().setSkipHeaderRecord(true)
                    .setIgnoreEmptyLines(true).setTrim(true).build().parse(new StringReader(csv));
            Map<String, String> headers = normalizedHeaders(records.getHeaderMap().keySet());
            require(headers, "SYMBOL"); requireAny(headers, "QUANTITY", "QTY");
            requireAny(headers, "LONGTERMQUANTITY", "LONGTERMQTY", "LTQTY");
            requireAny(headers, "AVERAGEPRICE", "AVGPRICE", "AVERAGECOSTPRICE"); require(headers, "LTP");
            requireAny(headers, "INVESTMENTVALUE", "VALUEATCOST");
            requireAny(headers, "CURRENTVALUE", "MARKETVALUE", "VALUEATMARKETPRICE");
            for (CSVRecord record : records) {
                rows++;
                try {
                    String symbol = requiredText(value(record, headers, "SYMBOL"), "SYMBOL")
                            .replaceAll("\\s+", "").toUpperCase(Locale.ROOT);
                    BigDecimal quantity = requiredNumber(value(record, headers, "QUANTITY", "QTY"), "Quantity");
                    holdings.add(new ImportedHolding("SYMBOL:" + symbol, null, symbol, symbol, quantity,
                            number(value(record, headers, "LONGTERMQUANTITY", "LONGTERMQTY", "LTQTY")),
                            requiredNumber(value(record, headers, "AVERAGEPRICE", "AVGPRICE", "AVERAGECOSTPRICE"), "Average Price"),
                            requiredNumber(value(record, headers, "LTP"), "LTP"),
                            number(value(record, headers, "INVESTMENTVALUE", "VALUEATCOST")),
                            number(value(record, headers, "CURRENTVALUE", "MARKETVALUE", "VALUEATMARKETPRICE")),
                            number(value(record, headers, "REALIZEDPL", "REALISEDPL", "REALIZEDPROFITLOSS", "REALISEDPROFITLOSS")),
                            number(value(record, headers, "UNREALIZEDPL", "UNREALISEDPL", "UNREALIZEDPROFITLOSS", "UNREALISEDPROFITLOSS")),
                            number(value(record, headers, "UNREALIZEDPLPERCENT", "UNREALISEDPLPERCENT")),
                            number(value(record, headers, "TOTALPL", "TOTALPROFITLOSS")),
                            number(value(record, headers, "TODAYPL", "TODAYSPL", "TODAYSPROFITLOSS", "TODAYPROFITLOSS")), "INR"));
                } catch (IllegalArgumentException exception) {
                    issues.add("Row " + record.getRecordNumber() + ": " + exception.getMessage());
                }
            }
        } catch (IllegalArgumentException exception) { throw exception; }
        catch (Exception exception) { throw new IllegalArgumentException("Unable to parse HDFC portfolio CSV", exception); }
        return new PortfolioImportParseResult(brokerType(), "HDFC_INVEST_RIGHT_EQUITY_PORTFOLIO_V1",
                filenameAccount, rows, List.copyOf(holdings), List.copyOf(issues),
                List.of("Symbol", "Quantity", "Long-term Quantity", "Average Price", "LTP",
                        "Investment Value", "Current Value", "Unrealized P/L", "Realized P/L",
                        "Total P/L", "Today P/L"), snapshotAt);
    }

    private static Map<String, String> normalizedHeaders(Set<String> rawHeaders) {
        Map<String, String> headers = new HashMap<>();
        rawHeaders.forEach(header -> headers.put(normalizeHeader(header), header));
        return headers;
    }
    private static String normalizeHeader(String value) {
        return value == null ? "" : value.toUpperCase(Locale.ROOT).replace("%", "PERCENT").replaceAll("[^A-Z0-9]", "");
    }
    private static void require(Map<String, String> headers, String key) {
        if (!headers.containsKey(key)) throw new IllegalArgumentException("Unsupported HDFC CSV: missing column " + key);
    }
    private static void requireAny(Map<String, String> headers, String... keys) {
        if (Arrays.stream(keys).noneMatch(headers::containsKey))
            throw new IllegalArgumentException("Unsupported HDFC CSV: missing column " + keys[0]);
    }
    private static String value(CSVRecord record, Map<String, String> headers, String... keys) {
        return Arrays.stream(keys).map(headers::get).filter(Objects::nonNull).findFirst().map(record::get).orElse(null);
    }
    private static BigDecimal number(String raw) { return ICICIDirectCsvPortfolioParser.number(raw); }
    private static BigDecimal requiredNumber(String raw, String field) {
        BigDecimal value = number(raw); if (value == null) throw new IllegalArgumentException(field + " is required"); return value;
    }
    private static String requiredText(String raw, String field) {
        if (raw == null || raw.isBlank()) throw new IllegalArgumentException(field + " is required"); return raw.trim();
    }
}
