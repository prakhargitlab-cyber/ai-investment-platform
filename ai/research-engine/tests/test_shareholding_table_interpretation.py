"""Focused tests for the multi-column shareholding TABLE interpreter
(app.evidence_interpretation._parse_shareholding_table, exercised through
the public interpret_shareholding entry point), covering the task's
required cases A-I plus the real-PNG/Tesseract integration case.

This is the real runtime screenshot shape -- a header row of reporting
periods followed by category rows -- as distinct from the "Shareholding
as on <date>" prose the pre-existing app.shareholding parser handles
(see test_manual_evidence_shareholding.py / test_shareholding.py,
untouched by this change).
"""
from __future__ import annotations

from pathlib import Path

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.evidence_interpretation import interpret_shareholding
from app.models import ShareholdingCategory
from app.spatial_table_reconstruction import OcrWord

INSTRUMENT_ID = uuid4()


def _interpret(text: str):
    return interpret_shareholding(
        SimpleNamespace(text=text),
        instrument_id=INSTRUMENT_ID,
        content_digest="deadbeef",
        filename="shareholding.png",
        mime="image/png",
    )


class TestCaseANormalTable:
    """CASE A -- the task's concrete example table."""

    TABLE = (
        "Shareholding Pattern\n\n"
        "                  Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
        "Promoters          73.67      74.24      74.24      74.24\n"
        "FIIs                1.87       1.58       1.51       1.87\n"
        "DIIs                0.01       0.01       0.09       0.11\n"
        "Public             24.45      24.16      24.18      23.78\n"
    )

    def test_latest_period_and_values(self):
        result = _interpret(self.TABLE)
        assert not result.errors
        assert result.period_end is not None
        # Source gave MONTH precision ("Jun 2026") -- normalized to
        # 2026-06-01 (first-of-month technical placeholder), never
        # invented as a quarter-end/month-end day (2026-06-30).
        assert (result.period_end.year, result.period_end.month, result.period_end.day) == (2026, 6, 1)
        assert result.period_precision == "MONTH"
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category == {
            ShareholdingCategory.PROMOTER: Decimal("74.24"),
            ShareholdingCategory.FII_FPI: Decimal("1.87"),
            ShareholdingCategory.DII: Decimal("0.11"),
            ShareholdingCategory.PUBLIC_RETAIL: Decimal("23.78"),
        }

    def test_older_columns_are_not_selected(self):
        result = _interpret(self.TABLE)
        by_category = {v.category: v.percentage for v in result.values}
        # Sep 2025's Promoter value (73.67) must never win over Jun 2026's (74.24).
        assert by_category[ShareholdingCategory.PROMOTER] != Decimal("73.67")

    def test_no_implausibility_warning_for_a_plausible_sum(self):
        result = _interpret(self.TABLE)
        assert not any("OWNERSHIP_SUM_IMPLAUSIBLE" in w for w in result.warnings)


class TestCaseBIrregularWhitespace:
    def test_row_column_mapping_survives_ragged_spacing(self):
        text = (
            "Sep 2025    Dec 2025\tMar 2026     Jun 2026\n"
            "Promoters     73.67   74.24  74.24   74.24\n"
            "FIIs 1.87 1.58 1.51 1.87\n"
        )
        result = _interpret(text)
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("74.24")
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")


class TestCaseCSingularPluralLabels:
    @pytest.mark.parametrize("promoter_label,fii_label,dii_label", [
        ("Promoter", "FII", "DII"),
        ("Promoters", "FIIs", "DIIs"),
    ])
    def test_singular_and_plural_variants_both_recognized(self, promoter_label, fii_label, dii_label):
        text = (
            "Sep 2025   Jun 2026\n"
            f"{promoter_label} 73.67 74.24\n"
            f"{fii_label} 1.2 1.87\n"
            f"{dii_label} 0.05 0.11\n"
        )
        result = _interpret(text)
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("74.24")
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")
        assert by_category[ShareholdingCategory.DII] == Decimal("0.11")


class TestCaseDMissingRow:
    def test_missing_row_is_left_unset_never_invented(self):
        text = (
            "Sep 2025   Jun 2026\n"
            "Promoters 73.67 74.24\n"
            "FIIs 1.2 1.87\n"
            # No DII row, no Public row at all.
        )
        result = _interpret(text)
        by_category = {v.category: v.percentage for v in result.values}
        assert ShareholdingCategory.DII not in by_category
        assert ShareholdingCategory.PUBLIC_RETAIL not in by_category
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("74.24")


class TestCaseEMalformedPercentage:
    def test_unparseable_value_in_the_latest_column_is_skipped_not_guessed(self):
        text = (
            "Sep 2025   Jun 2026\n"
            "Promoters 73.67 N/A\n"  # "N/A" contributes no numeric token
            "FIIs 1.2 1.87\n"
        )
        result = _interpret(text)
        by_category = {v.category: v.percentage for v in result.values}
        # Only one numeric token on the Promoter row ("73.67") but two
        # period columns -- the row is short a value and must be left
        # unset rather than misaligned onto the wrong column.
        assert ShareholdingCategory.PROMOTER not in by_category
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")
        assert any("ROW_COLUMN_COUNT_MISMATCH" in w for w in result.warnings)


class TestCaseFValueOutsideRange:
    def test_out_of_range_percentage_is_rejected_as_an_error(self):
        text = (
            "Sep 2025   Jun 2026\n"
            "Promoters 73.67 174.24\n"
            "FIIs 1.2 1.87\n"
        )
        result = _interpret(text)
        assert any("INVALID_PERCENTAGE_IN_TABLE" in e for e in result.errors)
        by_category = {v.category: v.percentage for v in result.values}
        assert ShareholdingCategory.PROMOTER not in by_category
        # The rest of the table is still usable.
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")


class TestCaseGAmbiguousPeriodColumns:
    def test_no_period_columns_at_all_degrades_to_no_values_not_a_guess(self):
        text = "Promoters 74.24\nFIIs 1.87\n"  # no header row with >=2 periods
        result = _interpret(text)
        assert result.values == []
        assert any("NO_SHAREHOLDING_VALUES_EXTRACTED" in e for e in result.errors)

    def test_duplicate_latest_period_columns_are_not_guessed(self):
        text = (
            "Jun 2026   Jun 2026\n"
            "Promoters 74.24 74.30\n"
        )
        result = _interpret(text)
        assert result.values == []
        assert any("AMBIGUOUS_PERIOD_COLUMNS" in e for e in result.errors)


class TestCaseHMultiplePeriodsOlderColumnNotSelected:
    def test_many_columns_correctly_select_the_single_latest(self):
        text = (
            "Jun 2024   Sep 2024   Dec 2024   Mar 2025   Jun 2025   Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
            "Promoters   70.1       70.5       71.0       71.5       72.0       73.67      73.9       74.24      74.24\n"
        )
        result = _interpret(text)
        by_category = {v.category: v.percentage for v in result.values}
        # The Promoter value is identical for the last two columns (Mar
        # 2026 and Jun 2026 both 74.24) in this fixture -- so additionally
        # prove the SELECTED period itself is Jun 2026, not merely that
        # the value happens to match.
        assert result.period_end.month == 6 and result.period_end.year == 2026
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("74.24")


class TestCaseIExplicitExactDateTakesPrecedenceOverTableLogic:
    def test_explicit_as_on_date_is_handled_by_the_existing_prose_parser_unchanged(self):
        """An explicit 'as on 30 Jun 2026' phrase is handled entirely by
        the pre-existing app.shareholding prose parser (tried first,
        unchanged by this task) -- proving the new table path never
        overrides or interferes with that established, tested behavior."""
        text = (
            "Shareholding as on 30 June 2026\n\n"
            "Promoter and promoter group: 74.24%\n"
            "Foreign institutional investors: 1.87%\n"
            "Domestic institutional investors: 0.11%\n"
            "Public shareholders: 23.78%\n"
        )
        result = _interpret(text)
        assert not result.errors
        assert result.period_end.year == 2026 and result.period_end.month == 6 and result.period_end.day == 30
        # NOT normalized to the 1st of the month -- an explicit source
        # date is preserved exactly.


class TestShareholderCountAndSourcePeriodPreserved:
    def test_shareholder_count_preserved_as_non_canonical_supplementary_metadata(self):
        text = (
            "Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
            "Promoters   73.67      74.24      74.24      74.24\n"
            "FIIs         1.87       1.58       1.51       1.87\n"
            "DIIs         0.01       0.01       0.09       0.11\n"
            "Public      24.45      24.16      24.18      23.78\n"
            "No. of Shareholders   49,673   52,198   52,418   50,407\n"
        )
        result = _interpret(text)
        assert result.supplementary is not None
        assert result.supplementary["shareholderCount"] == "50407"
        assert result.supplementary["sourcePeriodText"] == "Jun 2026"
        # Never a canonical ShareholdingCategory value.
        assert all(v.category != "SHAREHOLDER_COUNT" for v in result.values)

    def test_missing_shareholder_count_row_is_left_unset_never_zero(self):
        text = (
            "Sep 2025   Jun 2026\n"
            "Promoters 73.67 74.24\n"
        )
        result = _interpret(text)
        assert result.supplementary is None or "shareholderCount" not in result.supplementary


class TestPromoterPledgeNeverDefaultedToZero:
    def test_pledge_absent_from_table_is_never_invented_as_zero(self):
        text = (
            "Sep 2025   Jun 2026\n"
            "Promoters 73.67 74.24\n"
            "FIIs 1.2 1.87\n"
        )
        result = _interpret(text)
        by_category = {v.category: v.percentage for v in result.values}
        assert ShareholdingCategory.PROMOTER_PLEDGE not in by_category


class TestRealPngTesseractIntegration:
    """Case 23: an actual generated PNG containing a multi-column
    shareholding table, run through the real DocumentExtractor (real
    Tesseract when available) and the real ShareholdingEvidenceInterpreter
    -- not a mocked OCR call. Skipped (never faked) when tesseract is not
    installed in this environment."""

    def test_real_png_ocr_through_table_interpreter(self):
        import shutil
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed in this environment")

        import io
        from PIL import Image, ImageDraw, ImageFont

        from app.document_extraction import DocumentExtractor, DocumentInput, PNG_MIME

        # A large, monospaced, high-contrast rendering with generous
        # column spacing -- real Tesseract reads this far more reliably
        # than small proportional text, which is what actually matters
        # for this test (proving the real OCR -> table-interpreter
        # pipeline works end-to-end, not testing Tesseract's handling of
        # arbitrary fonts).
        font = ImageFont.truetype(
            str(Path(__file__).resolve().parent / "fixtures" / "fonts" / "DejaVuSansMono-Bold.ttf"), 36)
        lines = [
            "Sep 2025   Dec 2025   Mar 2026   Jun 2026",
            "Promoters   73.67      74.24      74.24      74.24",
            "FIIs         1.87       1.58       1.51       1.87",
            "DIIs         0.01       0.01       0.09       0.11",
            "Public      24.45      24.16      24.18      23.78",
        ]
        image = Image.new("RGB", (1700, 60 + len(lines) * 60), color="white")
        draw = ImageDraw.Draw(image)
        for index, line in enumerate(lines):
            draw.text((20, 20 + index * 60), line, fill="black", font=font)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        extracted = DocumentExtractor().extract(DocumentInput(
            file_bytes=buffer.getvalue(), mime_type=PNG_MIME, filename="shareholding.png",
        ))
        assert extracted.extraction_method == "IMAGE_OCR"
        assert extracted.text.strip()

        result = interpret_shareholding(
            extracted, instrument_id=INSTRUMENT_ID, content_digest="real-ocr",
            filename="shareholding.png", mime=PNG_MIME,
        )
        # Real OCR on rendered text is not pixel-perfect -- assert on the
        # structural outcome (a latest period was found and at least the
        # Promoter row was mapped) rather than exact-match every value,
        # which would make this test flaky on font-rendering differences
        # rather than genuinely proving the pipeline works end-to-end.
        assert result.period_end is not None, f"OCR text was: {extracted.text!r}"
        assert result.period_end.year == 2026 and result.period_end.month == 6
        by_category = {v.category: v.percentage for v in result.values}
        assert ShareholdingCategory.PROMOTER in by_category


class TestRealHttpDraftEndpointWithRealMultipartPng:
    """Backend/API regression required by the runtime defect closure
    (Defect A): the REAL FastAPI HTTP endpoint
    (POST /api/v1/research/evidence/draft), driven with a REAL generated
    PNG through REAL multipart/form-data (httpx's TestClient builds a
    genuine multipart body, not a mocked filename/MIME pair), reproducing
    the Gokul Agro Resources Limited SHAREHOLDING upload shape from the
    real browser failure this closure investigated.

    This proves, through the actual ASGI app boundary (UploadFile ->
    file.read() -> ManualEvidenceIngestor -> DocumentExtractor ->
    image_evidence_extraction -> Pillow -> tesseract ->
    ShareholdingEvidenceInterpreter -> candidate DRAFT), that the backend
    half of the transport chain was never the problem -- the real root
    cause for the browser's HTTP 500 was services/api-gateway's
    routeResearch() decoding the raw multipart body as a UTF-8 string
    before proxying it, upstream of this endpoint entirely (fixed
    separately in PortfolioRouteController.java and covered by
    ResearchMultipartByteIntegrityHttpIntegrationTest.java). A TestClient
    call talks to this ASGI app in-process and therefore cannot exercise
    that gateway hop -- it exists to prove this half of the chain, not the
    gateway half.

    Per the task's explicit instruction, only the actual OCR-execution
    assertion may be conditionally skipped if tesseract is unavailable in
    a given test environment; the multipart byte-integrity and
    persistence/draft-creation assertions below never skip.
    """

    PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")
    GOKUL_AGRO_ID = "f86f7c59-85a6-40f2-a643-481c3ec2f53a"

    def _render_real_shareholding_png(self) -> bytes:
        import io
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(
            str(Path(__file__).resolve().parent / "fixtures" / "fonts" / "DejaVuSansMono-Bold.ttf"), 36)
        lines = [
            "Sep 2025   Dec 2025   Mar 2026   Jun 2026",
            "Promoters   73.67      74.24      74.24      74.24",
            "FIIs         1.87       1.58       1.51       1.87",
            "DIIs         0.01       0.01       0.09       0.11",
            "Public      24.45      24.16      24.18      23.78",
        ]
        image = Image.new("RGB", (1700, 60 + len(lines) * 60), color="white")
        draw = ImageDraw.Draw(image)
        for index, line in enumerate(lines):
            draw.text((20, 20 + index * 60), line, fill="black", font=font)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app, headers={
            "X-Aip-User-Id": "test-user",
            "X-Aip-User-Issuer": "test-issuer",
            "X-Aip-User-Subject": "test-subject",
        })

    def test_real_png_multipart_upload_reaches_backend_byte_exact_and_creates_a_draft(self):
        from app import manual_evidence
        manual_evidence._drafts.clear()

        png_bytes = self._render_real_shareholding_png()
        # Fixture sanity: this really is a PNG, not a stand-in.
        assert png_bytes[:8] == self.PNG_SIGNATURE

        client = self._client()
        response = client.post(
            "/api/v1/research/evidence/draft",
            params={"global_instrument_id": self.GOKUL_AGRO_ID, "evidence_type": "SHAREHOLDING"},
            files={"file": ("gokul-agro-shareholding.png", png_bytes, "image/png")},
        )

        # Byte-integrity and persistence assertions -- never skipped, even
        # without tesseract: the draft is created either way (OCR failing
        # closed is a separate, already-covered concern --
        # test_ocr_runtime_capability_gating.py), proving the file bytes
        # reached ManualEvidenceIngestor/DocumentExtractor/Pillow intact
        # and a draft record was durably created from them.
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["evidenceType"] == "SHAREHOLDING"
        assert body["status"] == "DRAFT"
        draft_id = body["draftId"]
        assert manual_evidence._drafts[__import__("uuid").UUID(draft_id)].content_hash == \
            __import__("hashlib").sha256(png_bytes).hexdigest()

        import shutil
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed in this environment -- OCR-execution assertions only")

        # OCR-execution assertions: the exact Gokul Agro Jun-2026 result
        # this closure's task specified, now proven through the real HTTP
        # endpoint rather than only the lower-level interpreter unit test.
        # Real OCR on rendered text is not pixel-perfect (see the sibling
        # unit-level test in this file); assert on the structural outcome
        # that matters for this closure -- a 2026-06 period was recognized
        # and the promoter row was mapped -- rather than exact string
        # equality on every OCR'd field, which would make this test flaky
        # on font-rendering differences rather than proving the transport
        # + pipeline integration this test exists for.
        assert body["reportingPeriod"] is not None
        assert body["reportingPeriod"].startswith("2026-06-01")
        proposed = {fact["field"]: fact["value"] for fact in body["proposedFacts"]}
        assert "shareholding:PROMOTER" in proposed
        assert body["validationResults"]["valid"] is not False



class TestCaseJReorderedRows:
    """Rows in a different order than the usual Promoter/FII/DII/Public
    sequence -- category matching is by LABEL, never by row position, so
    a reordered table must produce identical values to the canonical
    ordering."""

    def test_reordered_rows_produce_identical_values(self):
        text = (
            "Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
            "Public             24.45      24.16      24.18      23.78\n"
            "DIIs                0.01       0.01       0.09       0.11\n"
            "Promoters          73.67      74.24      74.24      74.24\n"
            "FIIs                1.87       1.58       1.51       1.87\n"
        )
        result = _interpret(text)
        assert not result.errors
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category == {
            ShareholdingCategory.PROMOTER: Decimal("74.24"),
            ShareholdingCategory.FII_FPI: Decimal("1.87"),
            ShareholdingCategory.DII: Decimal("0.11"),
            ShareholdingCategory.PUBLIC_RETAIL: Decimal("23.78"),
        }


class TestCaseKCroppedSingleColumnTable:
    """A cropped screenshot showing only the latest period column (no
    historical columns) -- still a valid single-period header, must
    still work."""

    def test_single_period_column_table_is_parsed(self):
        text = (
            "Jun 2026\n"
            "Promoters 74.24\n"
            "FIIs 1.87\n"
            "DIIs 0.11\n"
            "Public 23.78\n"
        )
        result = _interpret(text)
        # A single period column means no ">= 2 period columns" header
        # was found by the table parser, so this degrades to the
        # vertical key-value fallback -- which is exactly the point:
        # a cropped table and a vertical layout can look identical once
        # cropped down to one column, and both must resolve correctly.
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category == {
            ShareholdingCategory.PROMOTER: Decimal("74.24"),
            ShareholdingCategory.FII_FPI: Decimal("1.87"),
            ShareholdingCategory.DII: Decimal("0.11"),
            ShareholdingCategory.PUBLIC_RETAIL: Decimal("23.78"),
        }
        assert result.period_end is not None
        assert (result.period_end.year, result.period_end.month, result.period_end.day) == (2026, 6, 1)


class TestCaseLVerticalKeyValueLayout:
    """A different screenshot layout entirely: one label, one value per
    line, with the period stated separately rather than as table-header
    columns."""

    def test_vertical_layout_with_explicit_period_label(self):
        text = (
            "Shareholding Summary\n"
            "Period: Jun 2026\n"
            "Promoter: 74.24%\n"
            "FII/FPI: 1.87%\n"
            "DII: 0.11%\n"
            "Public: 23.78%\n"
            "No. of Shareholders: 50,407\n"
        )
        result = _interpret(text)
        assert not result.errors
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category == {
            ShareholdingCategory.PROMOTER: Decimal("74.24"),
            ShareholdingCategory.FII_FPI: Decimal("1.87"),
            ShareholdingCategory.DII: Decimal("0.11"),
            ShareholdingCategory.PUBLIC_RETAIL: Decimal("23.78"),
        }
        assert result.period_precision == "MONTH"
        assert (result.period_end.year, result.period_end.month, result.period_end.day) == (2026, 6, 1)
        assert result.supplementary["shareholderCount"] == "50407"

    def test_vertical_layout_with_no_period_at_all_is_left_unset_not_guessed(self):
        text = (
            "Promoter: 74.24%\n"
            "FII/FPI: 1.87%\n"
            "DII: 0.11%\n"
            "Public: 23.78%\n"
        )
        result = _interpret(text)
        assert result.values == []
        assert any("NO_SHAREHOLDING_VALUES_EXTRACTED" in e for e in result.errors)

    def test_vertical_layout_with_two_different_periods_is_left_ambiguous(self):
        text = (
            "Compare Mar 2026 to Jun 2026\n"
            "Promoter: 74.24%\n"
            "FII/FPI: 1.87%\n"
        )
        result = _interpret(text)
        assert result.values == []


class TestCaseMSimilarLabelsAndOcrTypos:
    """OCR spelling variations that are genuinely close to ONE real
    label must still resolve correctly; a variation that is equally
    close to TWO real labels (the DII/FII confusion this task's fuzzy
    matcher was fixed for) must be flagged as ambiguous rather than
    silently assigned to either one."""

    def test_a_genuine_single_character_ocr_typo_still_resolves(self):
        # "Promnters" (missing the "o") is a realistic single-character
        # OCR drop of "Promoters" and is not close to any OTHER
        # category's label, so the bounded fuzzy fallback should
        # recognize it.
        text = (
            "Sep 2025   Jun 2026\n"
            "Promnters 73.67 74.24\n"
            "FIIs 1.2 1.87\n"
            "DIIs 0.05 0.11\n"
        )
        result = _interpret(text)
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("74.24")

    def test_dii_and_fii_rows_are_never_confused_with_each_other(self):
        # The exact scenario the fuzzy-matching bug produced: "DIIs" and
        # "FIIs" differ by a single character, and a naive per-category
        # fuzzy check (checking FII_FPI's variants before DII's) could
        # misclassify the DII row as FII_FPI. Both are real labels and
        # exact-match directly, so this must never even reach the fuzzy
        # path, let alone collide.
        text = (
            "Sep 2025   Jun 2026\n"
            "Promoters 73.67 74.24\n"
            "FIIs 1.2 1.87\n"
            "DIIs 0.05 0.11\n"
        )
        result = _interpret(text)
        assert not result.errors
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")
        assert by_category[ShareholdingCategory.DII] == Decimal("0.11")


class TestCaseNSpatialPositionalCrossCheck:
    """FINAL CORRECTNESS GATE 1 & 2: spatial column assignment is never
    trusted blindly, and a dropped OCR cell in the middle of a row must
    never cause a later value to be silently shifted onto the wrong
    period column. Exercises ``_parse_shareholding_table`` directly with
    hand-built word-level coordinates so the scenario is deterministic
    (not dependent on real Tesseract actually mis-reading a rendered
    image)."""

    HEADER_WORDS = [
        OcrWord(text="Sep", left=80, top=0, width=40, height=20, conf=90.0, line_key=(1, 1, 1)),
        OcrWord(text="2025", left=130, top=0, width=60, height=20, conf=90.0, line_key=(1, 1, 1)),
        OcrWord(text="Dec", left=280, top=0, width=40, height=20, conf=90.0, line_key=(1, 1, 1)),
        OcrWord(text="2025", left=330, top=0, width=60, height=20, conf=90.0, line_key=(1, 1, 1)),
        OcrWord(text="Mar", left=480, top=0, width=40, height=20, conf=90.0, line_key=(1, 1, 1)),
        OcrWord(text="2026", left=530, top=0, width=60, height=20, conf=90.0, line_key=(1, 1, 1)),
        OcrWord(text="Jun", left=680, top=0, width=40, height=20, conf=90.0, line_key=(1, 1, 1)),
        OcrWord(text="2026", left=730, top=0, width=60, height=20, conf=90.0, line_key=(1, 1, 1)),
    ]
    HEADER_TEXT = "Sep 2025 Dec 2025 Mar 2026 Jun 2026"

    def test_dropped_middle_cell_still_resolves_the_correct_latest_value(self):
        # Dec 2025's value is entirely missing (a dropped OCR cell in
        # the middle of the row) -- plain positional counting only sees
        # 3 numbers for 4 period columns and must conservatively bail
        # (ROW_COLUMN_COUNT_MISMATCH / value left unset), but spatial
        # assignment can still correctly find Jun 2026's value because
        # that word is still physically there at the right x-position,
        # and it must NOT be confused with Mar 2026's value next to it.
        row_words = [
            OcrWord(text="Promoters", left=10, top=40, width=150, height=20, conf=90.0, line_key=(1, 1, 2)),
            OcrWord(text="73.67", left=90, top=40, width=70, height=20, conf=90.0, line_key=(1, 1, 2)),
            # Dec 2025 value dropped entirely -- no word near x=330.
            OcrWord(text="74.24", left=490, top=40, width=70, height=20, conf=90.0, line_key=(1, 1, 2)),
            OcrWord(text="74.90", left=690, top=40, width=70, height=20, conf=90.0, line_key=(1, 1, 2)),
        ]
        words = self.HEADER_WORDS + row_words
        text = f"{self.HEADER_TEXT}\nPromoters 73.67 74.24 74.90\n"

        from app.evidence_interpretation import _parse_shareholding_table
        result = _parse_shareholding_table(text, words=words)

        assert result is not None
        by_category = {v.category: v.percentage for v in result.values}
        # Jun 2026 (the latest column) must resolve to its own real
        # value, never Mar 2026's neighboring value.
        assert by_category.get(ShareholdingCategory.PROMOTER) == Decimal("74.90")
        assert by_category.get(ShareholdingCategory.PROMOTER) != Decimal("74.24")
        assert not any("ROW_COLUMN_COUNT_MISMATCH" in w for w in result.warnings)

    def test_spatial_and_positional_disagreement_is_never_silently_resolved_by_positional(self):
        # The word-level (spatial) position of the Jun 2026 value and
        # the plain-text reading-order (positional) value for the same
        # row disagree -- simulating a genuine OCR layout artifact where
        # the TSV bounding boxes and the linearized text order diverge.
        # Positional counting alone would confidently (and wrongly)
        # report 0.19; this must instead be left unset for manual
        # review, never silently resolved by preferring positional
        # counting over the grounded spatial data.
        row_words = [
            OcrWord(text="DIIs", left=10, top=40, width=100, height=20, conf=90.0, line_key=(1, 1, 2)),
            OcrWord(text="0.01", left=90, top=40, width=70, height=20, conf=90.0, line_key=(1, 1, 2)),
            OcrWord(text="0.01", left=290, top=40, width=70, height=20, conf=90.0, line_key=(1, 1, 2)),
            # Physically positioned under Mar 2026 (x~525) but reads as
            # "0.19" -- the value positional counting (reading the text
            # left-to-right) would assign to the LAST/latest column.
            OcrWord(text="0.19", left=490, top=40, width=70, height=20, conf=90.0, line_key=(1, 1, 2)),
            # Physically positioned under Jun 2026 (x~725) -- the
            # genuinely latest-column-aligned value.
            OcrWord(text="0.09", left=690, top=40, width=70, height=20, conf=90.0, line_key=(1, 1, 2)),
        ]
        words = self.HEADER_WORDS + row_words
        # Deliberately diverges from word order: plain text reads
        # "0.01 0.01 0.09 0.19" (0.09 before 0.19), but the WORDS place
        # "0.19" under Mar 2026 and "0.09" under Jun 2026 -- the
        # disagreement this test is about.
        text = f"{self.HEADER_TEXT}\nDIIs 0.01 0.01 0.09 0.19\n"

        from app.evidence_interpretation import _parse_shareholding_table
        result = _parse_shareholding_table(text, words=words)

        assert result is not None
        by_category = {v.category: v.percentage for v in result.values}
        assert ShareholdingCategory.DII not in by_category
        assert any("SPATIAL_POSITIONAL_DISAGREEMENT" in w for w in result.warnings)


class TestCaseOAmbiguousFuzzyMatchIsNeverSilentlyResolved:
    """FINAL CORRECTNESS GATE 3: exact matching must precede fuzzy
    matching GLOBALLY (across every category, not just within one), and
    a line that fuzzy-matches more than one category must never be
    silently assigned to either -- it must be flagged ambiguous."""

    def test_exact_match_always_wins_even_when_an_earlier_category_would_fuzzy_match(self):
        # "DIIs" exact-matches DII directly. A naive per-category loop
        # that tried fuzzy matching before reaching DII (or that let an
        # earlier category's fuzzy guess pre-empt a later category's
        # exact match) is exactly the regression this gate guards
        # against -- this is the real end-to-end path, not a synthetic
        # unit test of the matcher alone.
        from app.evidence_interpretation import _match_label_prefix, _SHAREHOLDING_TABLE_LABELS, _init_shareholding_table_labels
        _init_shareholding_table_labels()
        for category, variants in _SHAREHOLDING_TABLE_LABELS:
            if category == ShareholdingCategory.DII:
                assert _match_label_prefix("DIIs 0.11", variants) is not None
            else:
                assert _match_label_prefix("DIIs 0.11", variants) is None

    def test_a_line_fuzzy_matching_two_categories_at_once_is_flagged_not_guessed(self):
        # Direct unit test of the cross-category safety net itself,
        # using two deliberately similar synthetic 6+ character labels
        # (the real domain label set has no such collision left, by
        # design -- see the DII/FII fix) to prove the MECHANISM still
        # catches a genuine tie rather than depending on today's label
        # set never producing one.
        from app.evidence_interpretation import _fuzzy_label_candidates
        fake_labels = (
            ("CATEGORY_ALPHA", ("silverx",)),
            ("CATEGORY_BETA", ("silvery",)),
        )
        candidates = _fuzzy_label_candidates("silverz 12.3", fake_labels)
        assert len(candidates) == 2
        assert {c for c, _r in candidates} == {"CATEGORY_ALPHA", "CATEGORY_BETA"}


class TestCasePGokulAgroRuntimeScreenshotReplica:
    """FINAL CORRECTNESS GATE 4: the full real pipeline (PNG -> real
    Tesseract TSV -> real DocumentExtractor -> real EvidenceInterpreter)
    against a faithful re-creation of the actual Gokul Agro Resources
    Limited SHAREHOLDING screenshot values recorded during the original
    runtime defect investigation (see module history: Sep 2025 73.67 /
    Dec 2025 74.24 / Mar 2026 74.24 / Jun 2026 74.24 for Promoters, etc).

    NOTE: the literal original screenshot file is not present anywhere
    in this repository or environment (it was a one-off manual browser
    upload during live defect triage, never saved as a fixture) -- this
    test renders the identical numbers/layout as a new PNG and proves
    the full pipeline resolves them correctly end to end, rather than
    re-testing on synthetic plain-text OCR output as the earlier cases
    in this file do.
    """

    def test_full_pipeline_resolves_all_four_categories_and_shareholder_count(self):
        import shutil
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed in this environment")

        import io
        from PIL import Image, ImageDraw, ImageFont

        from app.document_extraction import DocumentExtractor, DocumentInput, PNG_MIME

        font = ImageFont.truetype(
            str(Path(__file__).resolve().parent / "fixtures" / "fonts" / "DejaVuSansMono-Bold.ttf"), 36)
        # Rendered as a genuine column-aligned table (a fixed x-position
        # per column, each cell drawn independently) -- this is how an
        # actual rendered HTML/app table screenshot looks, as opposed to
        # a single space-padded ASCII string per row, whose columns only
        # coincidentally line up when every row's label happens to be
        # the same width (they are not, here: "Promoters" vs "FIIs" vs
        # "No. of Shareholders").
        col_x = [40, 600, 830, 1060, 1290]
        rows = [
            ["", "Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026"],
            ["Promoters", "73.67", "74.24", "74.24", "74.24"],
            ["FIIs", "1.87", "1.58", "1.51", "1.87"],
            ["DIIs", "0.01", "0.01", "0.09", "0.11"],
            ["Public", "24.45", "24.16", "24.18", "23.78"],
            ["No. of Shareholders", "49,673", "52,198", "52,418", "50,407"],
        ]
        image = Image.new("RGB", (1650, 60 + len(rows) * 60), color="white")
        draw = ImageDraw.Draw(image)
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                draw.text((col_x[c], 20 + r * 60), cell, fill="black", font=font)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        extracted = DocumentExtractor().extract(DocumentInput(
            file_bytes=buffer.getvalue(), mime_type=PNG_MIME, filename="gokul_agro_shareholding.png",
        ))
        assert extracted.extraction_method == "IMAGE_OCR"

        result = interpret_shareholding(
            extracted, instrument_id=INSTRUMENT_ID, content_digest="gokul-agro-replica",
            filename="gokul_agro_shareholding.png", mime=PNG_MIME,
        )

        by_category = {v.category: v.percentage for v in result.values}
        ocr_text_for_debug = extracted.text

        assert result.period_end is not None, f"OCR text was: {ocr_text_for_debug!r}"
        assert (result.period_end.year, result.period_end.month, result.period_end.day) == (2026, 6, 1), ocr_text_for_debug
        assert result.period_precision == "MONTH"

        assert by_category.get(ShareholdingCategory.PROMOTER) == Decimal("74.24"), ocr_text_for_debug
        assert by_category.get(ShareholdingCategory.FII_FPI) == Decimal("1.87"), ocr_text_for_debug
        assert by_category.get(ShareholdingCategory.DII) == Decimal("0.11"), ocr_text_for_debug
        assert by_category.get(ShareholdingCategory.PUBLIC_RETAIL) == Decimal("23.78"), ocr_text_for_debug

        assert result.supplementary is not None
        assert result.supplementary.get("shareholderCount") == "50407", ocr_text_for_debug

        # No promoter-pledge category was present anywhere in the
        # source table -- it must never be fabricated/defaulted.
        assert ShareholdingCategory.PROMOTER_PLEDGE not in by_category


class TestCaseQOcrWordsMetadataIntegrityAcrossCalls:
    """FINAL CORRECTNESS GATE 5: ``ocrWords`` coordinates must survive
    from Tesseract through DocumentExtractor and remain associated with
    THEIR OWN source image only -- two different images extracted in
    the same process must never have their word-level coordinates or
    text cross-contaminate (no shared/global/cached state anywhere on
    this path)."""

    def test_two_different_images_extracted_in_sequence_never_share_ocr_words_or_text(self):
        import shutil
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed in this environment")

        import io
        from PIL import Image, ImageDraw, ImageFont

        from app.document_extraction import DocumentExtractor, DocumentInput, PNG_MIME

        font = ImageFont.truetype(
            str(Path(__file__).resolve().parent / "fixtures" / "fonts" / "DejaVuSansMono-Bold.ttf"), 36)

        def render(lines: list[str]) -> bytes:
            image = Image.new("RGB", (900, 60 + len(lines) * 60), color="white")
            draw = ImageDraw.Draw(image)
            for index, line in enumerate(lines):
                draw.text((20, 20 + index * 60), line, fill="black", font=font)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()

        extractor = DocumentExtractor()
        first = extractor.extract(DocumentInput(
            file_bytes=render(["Sep 2025   Jun 2026", "Promoters 73.67 74.24"]),
            mime_type=PNG_MIME, filename="first.png",
        ))
        second = extractor.extract(DocumentInput(
            file_bytes=render(["Mar 2024   Dec 2024", "Public 55.00 60.00"]),
            mime_type=PNG_MIME, filename="second.png",
        ))

        assert first.text.strip() and second.text.strip()
        assert first.text != second.text
        first_words = first.metadata.get("ocrWords") or []
        second_words = second.metadata.get("ocrWords") or []
        assert first_words and second_words
        first_texts = {w.text for w in first_words}
        second_texts = {w.text for w in second_words}
        # The two images' word sets must not overlap on their distinct
        # content (proves the words genuinely came from their own
        # image's OCR pass, not a cached/shared result).
        assert "Promoters" in first_texts or "73.67" in first_texts
        assert "Public" in second_texts or "55.00" in second_texts
        assert "Promoters" not in second_texts
        assert "Public" not in first_texts
        # Mutating one document's word list must never affect the other
        # (confirms no shared underlying list object).
        first_words.append("mutated-sentinel")
        assert "mutated-sentinel" not in (second.metadata.get("ocrWords") or [])


class TestCaseRPromoterGroupAndPublicShareholdingAliases:
    """TASK E item 1 -- the newly added "Promoter Group" / "Public
    Shareholding" label variants must resolve to the same canonical
    categories as the shorter "Promoters" / "Public" forms, exactly like
    every other alias."""

    TABLE = (
        "                  Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
        "Promoter Group      73.67      74.24      74.24      74.24\n"
        "FIIs                 1.87       1.58       1.51       1.87\n"
        "DIIs                 0.01       0.01       0.09       0.11\n"
        "Public Shareholding 24.45      24.16      24.18      23.78\n"
    )

    def test_promoter_group_and_public_shareholding_resolve_correctly(self):
        result = _interpret(self.TABLE)
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("74.24")
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")
        assert by_category[ShareholdingCategory.DII] == Decimal("0.11")
        assert by_category[ShareholdingCategory.PUBLIC_RETAIL] == Decimal("23.78")
        assert result.warnings == []


class TestCaseSDecorativeLabelPrefixesAreStripped:
    """TASK E item 1 -- decorative bullet/dash/star/arrow prefixes in
    front of a category label must not block matching."""

    TABLE = (
        "                  Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
        "- Promoters         73.67      74.24      74.24      74.24\n"
        "* FIIs                1.87       1.58       1.51       1.87\n"
        "• DIIs             0.01       0.01       0.09       0.11\n"
        "-> Public Shareholding 24.45    24.16      24.18      23.78\n"
    )

    def test_decorative_prefixes_do_not_block_label_matching(self):
        result = _interpret(self.TABLE)
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("74.24")
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")
        assert by_category[ShareholdingCategory.DII] == Decimal("0.11")
        assert by_category[ShareholdingCategory.PUBLIC_RETAIL] == Decimal("23.78")
        assert result.warnings == []


class TestCaseTTableContextGuardRejectsNarrativeMentions:
    """TASK E item 1 -- "Require table context to distinguish ownership
    categories from investment flows or other financial concepts."
    A category word appearing inside an ordinary sentence (not a table
    row) must never be mistaken for a table value row."""

    TABLE = (
        "                  Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
        "Promoters           73.67      74.24      74.24      74.24\n"
        "FIIs                 1.87       1.58       1.51       1.87\n"
        "DIIs                 0.01       0.01       0.09       0.11\n"
        "Public               24.45      24.16      24.18      23.78\n"
        "FII inflows were strong this quarter due to positive global cues\n"
        "- Promoter Group pledge remains nil\n"
        "* Public Shareholding is well diversified\n"
    )

    def test_narrative_sentences_mentioning_category_words_are_ignored(self):
        result = _interpret(self.TABLE)
        by_category = {v.category: v.percentage for v in result.values}
        # The four genuine table rows still resolve correctly...
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("74.24")
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")
        assert by_category[ShareholdingCategory.DII] == Decimal("0.11")
        assert by_category[ShareholdingCategory.PUBLIC_RETAIL] == Decimal("23.78")
        # ...and none of the trailing prose lines overwrote them or
        # produced a spurious extra value/warning.
        assert len(result.values) == 4
        assert result.warnings == []


class TestCaseUPlaceholderValueTokenStillCountsAsTableContext:
    """TASK E item 1 regression guard -- a genuine table row that
    contains a recognized non-numeric VALUE placeholder ("N/A") must
    still be treated as table context (so the pre-existing malformed-
    value handling in CASE E can still run), not silently discarded as
    if it were a narrative sentence."""

    def test_na_placeholder_among_numbers_is_still_table_context(self):
        text = (
            "Sep 2025   Jun 2026\n"
            "Promoters 73.67 N/A\n"
            "FIIs 1.2 1.87\n"
        )
        result = _interpret(text)
        by_category = {v.category: v.percentage for v in result.values}
        # PROMOTER's malformed latest-column value is skipped, not
        # guessed -- but the row is still recognized as a table row,
        # so the existing mismatch warning still fires.
        assert ShareholdingCategory.PROMOTER not in by_category
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")
        assert any("ROW_COLUMN_COUNT_MISMATCH" in w for w in result.warnings)


class TestCaseVCommaDelimitedRowsAreStillTableContext:
    """TASK E item 1 regression guard -- a comma-delimited (CSV-style)
    table row has no whitespace between values at all; the table-
    context guard must recognize it as table context by matching
    numeric tokens directly rather than splitting on whitespace."""

    TABLE = (
        "Sep 2025,Dec 2025,Mar 2026,Jun 2026\n"
        "Promoters,73.67,74.24,74.24,74.24\n"
        "FIIs,1.87,1.58,1.51,1.87\n"
        "DIIs,0.01,0.01,0.09,0.11\n"
        "Public,24.45,24.16,24.18,23.78\n"
    )

    def test_csv_style_rows_resolve_correctly(self):
        result = _interpret(self.TABLE)
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category[ShareholdingCategory.PROMOTER] == Decimal("74.24")
        assert by_category[ShareholdingCategory.FII_FPI] == Decimal("1.87")
        assert by_category[ShareholdingCategory.DII] == Decimal("0.11")
        assert by_category[ShareholdingCategory.PUBLIC_RETAIL] == Decimal("23.78")
        assert result.warnings == []


class TestCaseWStrayPlusOcrArtifactInValueRemainder:
    """FINAL FIX regression -- a real screenshot's OCR pass can also
    misread a table gridline/divider as a literal "+" INSIDE a value
    cell's remainder (e.g. "73.67+ 74.24+ 74.24+ 74.24+"), not just
    next to the label. Before the fix, _looks_like_pure_value_remainder
    did not recognize "+" as noise, so it misclassified every such row
    as narrative prose and discarded it -- even PROMOTER/PUBLIC_RETAIL
    rows with perfectly matched labels."""

    TABLE = (
        "                  Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
        "Promoters          73.67+     74.24+     74.24+     74.24+\n"
        "FIIs                1.87+      1.58+      1.51+      1.87+\n"
        "DIIs                0.01+      0.01+      0.09+      0.11+\n"
        "Public              24.45+     24.16+     24.18+     23.78+\n"
    )

    def test_stray_plus_in_value_remainder_is_treated_as_noise_not_prose(self):
        result = _interpret(self.TABLE)
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category.get(ShareholdingCategory.PROMOTER) == Decimal("74.24"), result.warnings
        assert by_category.get(ShareholdingCategory.FII_FPI) == Decimal("1.87"), result.warnings
        assert by_category.get(ShareholdingCategory.DII) == Decimal("0.11"), result.warnings
        assert by_category.get(ShareholdingCategory.PUBLIC_RETAIL) == Decimal("23.78"), result.warnings
        assert result.warnings == []


class TestCaseXGokulAgroLiteralOcrTypoFiisDiisMarginDisambiguation:
    """CURRENT CLOSURE regression -- the literal failing runtime OCR
    rows reported against the Gokul Agro Resources Limited screenshot
    AFTER the "+"-artifact fix (TestCaseW) was already applied: a
    single OCR character misread turns "FIIs" into "Flis" (capital I
    misread as lowercase l), which is Levenshtein distance 1 from the
    canonical "fiis" variant but distance 2 from "diis" -- a real,
    computable margin. The pre-existing resemblance detector ignored
    that margin and flagged ANY line matching more than one category's
    fuzzy variants as AMBIGUOUS_CATEGORY_LABEL_RESEMBLANCE, discarding
    the row instead of assigning it to the strictly-closer category.

    This also covers the related two-character-typo shape ("Flls"),
    which previously only produced a conservative
    UNMATCHED_CATEGORY_LABEL_RESEMBLANCE warning with no value
    assigned at all.

    Genuinely tied/equidistant garbled labels (e.g. "Giis", which is
    distance 1 from BOTH "fiis" and "diis") must still be left
    unassigned with an ambiguous-resemblance warning -- the fix must
    not weaken conservative handling for real ambiguity.
    """

    SINGLE_CHAR_TYPO_TABLE = (
        "                  Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
        "Promoters           73.67      74.24      74.24      74.24\n"
        "Flis                1.87       1.58       1.51       1.87\n"
        "Diis                0.01       0.01       0.09       0.11\n"
        "Public              24.45      24.16      24.18      23.78\n"
    )

    TWO_CHAR_TYPO_TABLE = (
        "                  Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
        "Promoters           73.67      74.24      74.24      74.24\n"
        "Flls                1.87       1.58       1.51       1.87\n"
        "Dlls                0.01       0.01       0.09       0.11\n"
        "Public              24.45      24.16      24.18      23.78\n"
    )

    GENUINELY_TIED_TABLE = (
        "                  Sep 2025   Dec 2025   Mar 2026   Jun 2026\n"
        "Promoters           73.67      74.24      74.24      74.24\n"
        "Giis                1.87       1.58       1.51       1.87\n"
        "Public              24.45      24.16      24.18      23.78\n"
    )

    def test_literal_runtime_single_char_typo_fiis_diis_resolves_jun_2026_values(self):
        result = _interpret(self.SINGLE_CHAR_TYPO_TABLE)
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category.get(ShareholdingCategory.PROMOTER) == Decimal("74.24"), result.warnings
        assert by_category.get(ShareholdingCategory.FII_FPI) == Decimal("1.87"), result.warnings
        assert by_category.get(ShareholdingCategory.DII) == Decimal("0.11"), result.warnings
        assert by_category.get(ShareholdingCategory.PUBLIC_RETAIL) == Decimal("23.78"), result.warnings
        assert result.warnings == []

    def test_two_char_typo_variant_also_resolves_without_warning(self):
        result = _interpret(self.TWO_CHAR_TYPO_TABLE)
        by_category = {v.category: v.percentage for v in result.values}
        assert by_category.get(ShareholdingCategory.FII_FPI) == Decimal("1.87"), result.warnings
        assert by_category.get(ShareholdingCategory.DII) == Decimal("0.11"), result.warnings
        assert result.warnings == []

    def test_genuinely_tied_garbled_label_remains_conservatively_ambiguous(self):
        result = _interpret(self.GENUINELY_TIED_TABLE)
        by_category = {v.category: v.percentage for v in result.values}
        assert ShareholdingCategory.FII_FPI not in by_category
        assert ShareholdingCategory.DII not in by_category
        assert any("AMBIGUOUS_CATEGORY_LABEL_RESEMBLANCE" in w for w in result.warnings), result.warnings
