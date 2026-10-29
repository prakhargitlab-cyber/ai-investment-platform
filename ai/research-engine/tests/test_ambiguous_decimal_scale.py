"""Regression test for AMBIGUOUS_DECIMAL_SCALE: a PDF text layer can drop the
decimal point for some cells in a row while rendering it correctly for
others (observed on a real NSE filing -- see financial_projection.py for the
full rationale). The withheld row must never silently feed a mis-scaled bare
integer into a persisted fact (and hence into ROE/ranking), and a
normally-formatted row must be completely unaffected.

NOTE on the rule's shape: an earlier version of this check compared cell
*magnitudes* (bare-integer value vs. decimal-cell value, in various bands).
That was found during review to be unworkable in both directions at once:
a band wide enough to catch the real CORDELIA row (whose three affected
cells differ from its one intact decimal cell by ~20x-135x, not a fixed
ratio) also caught ordinary bare integers in unrelated, correctly-reported
rows, because different period columns in the same row can legitimately
differ by a similar amount. There is no way to tell "dropped decimal" from
"the company's numbers really did jump that much" by comparing magnitudes
alone. The current rule instead uses a purely textual signal: NSE
statements format every period column of a monetary row with the same
decimal precision, so a row that mixes a decimal-formatted cell with a
bare-integer-formatted cell is internally inconsistent on its face,
independent of what either value is. This trades away the ability to
distinguish "which bare integer is the suspicious one" (the whole row is
withheld, see test below) and it will also flag a row where one column is a
genuinely-reported whole number (e.g. an exact zero) sitting next to
decimal-formatted columns -- see the last test, which documents that
trade-off explicitly rather than hiding it.

Built on the existing, already-proven JUNE fixture (test_official_nse_financial_parsing),
with only the "Net Profit for the period after Tax" row's numbers swapped, so
everything else about statement/column recognition is unchanged and already
verified working.
"""
from decimal import Decimal

from test_di20e_financial_projection import projected, values
from test_official_nse_financial_parsing import JUNE

_PAT_ROW = 'Net Profit for the period after Tax 1,927.21 1,684.31 1,745.69 6,427.00'


def _with_pat_row(replacement: str) -> str:
    assert _PAT_ROW in JUNE
    return JUNE.replace(_PAT_ROW, replacement)


def test_row_with_a_lost_decimal_point_is_withheld_not_trusted():
    # Exact pattern observed on the CORDELIA July NSE filing: three bare
    # integers and one correctly-decimaled cell in the same profit row.
    text = _with_pat_row('Net Profit for the period after Tax 27347 7303 373.08 50183')
    _, result = projected(text)
    assert values(result, 'pat') == []
    assert any(d.startswith('AMBIGUOUS_DECIMAL_SCALE:profit_after_tax') for d in result.diagnostics)


def test_entire_row_is_withheld_including_the_one_correctly_decimaled_cell():
    # The block is row-level, not cell-level: even the one cell that already
    # carries a plausible decimal (373.08, the apparently-valid CORDELIA
    # entry) is withheld along with its mis-formatted siblings, because the
    # check has no way to single out which specific cell(s) are wrong.
    text = _with_pat_row('Net Profit for the period after Tax 27347 7303 373.08 50183')
    _, result = projected(text)
    assert values(result, 'pat') == []  # 373.08 is NOT persisted either.


def test_consistently_formatted_row_is_unaffected():
    # Same row, but every cell already carries a plausible decimal -- the
    # fix must leave an unambiguous row completely alone.
    _, result = projected(JUNE)
    assert values(result, 'pat') == [Decimal('1927.21'), Decimal('1684.31'), Decimal('1745.69'), Decimal('6427.00')]
    assert not any(d.startswith('AMBIGUOUS_DECIMAL_SCALE') for d in result.diagnostics)


def test_bare_integers_with_no_decimal_sibling_in_the_row_are_unaffected():
    # No cell in this row has a decimal point at all, so there is nothing to
    # be inconsistent with -- a genuinely whole-number-reported row must not
    # be penalized.
    text = _with_pat_row('Net Profit for the period after Tax 2700 700 370 5000')
    _, result = projected(text)
    assert values(result, 'pat') == [Decimal('2700'), Decimal('700'), Decimal('370'), Decimal('5000')]
    assert not any(d.startswith('AMBIGUOUS_DECIMAL_SCALE') for d in result.diagnostics)


def test_known_tradeoff_a_genuine_whole_number_cell_next_to_decimals_is_also_flagged():
    # Documented, accepted trade-off: the rule is purely about formatting
    # consistency, not magnitude, so a row where one column is a genuinely
    # whole-number result (e.g. an exact zero quarter) next to
    # decimal-formatted columns is ALSO withheld, even though nothing was
    # actually lost in extraction. This is intentional conservatism -- a
    # mixed-precision monetary row is unusual enough in NSE filings to be
    # worth a human looking at it either way -- but it is a real source of
    # occasional unnecessary withholding, not just of true positives.
    text = _with_pat_row('Net Profit for the period after Tax 0 1684.31 1745.69 6427.00')
    _, result = projected(text)
    assert values(result, 'pat') == []
    assert any(d.startswith('AMBIGUOUS_DECIMAL_SCALE:profit_after_tax') for d in result.diagnostics)
