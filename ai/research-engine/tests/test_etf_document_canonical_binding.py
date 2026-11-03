"""Regression tests: reviewed documents remain bound by ISIN after fresh DB setup."""
from uuid import UUID

import pytest

from app.etf_official_document_sources import (
    AccessTier,
    ApprovedDocumentSource,
    ApprovedDocumentType,
    ApprovedSourceRegistry,
    bind_approved_sources_to_canonical_universe,
)
from app.etf_official_document_pipeline import EtfOfficialDocumentAcquisitionPipeline
from app.settings import Settings


OLD_ID = UUID("805e0ebc-ea95-4859-b739-bdfbf5db327e")
NEW_ID = UUID("11111111-1111-4111-8111-111111111111")
URL = "https://fund.example.com/reviewed.pdf"


def approved():
    return ApprovedSourceRegistry([ApprovedDocumentSource(
        instrument_id=OLD_ID, isin="INF204KB14I2", ticker="NIFTYBEES",
        issuer_name="Reviewed fund issuer", document_url=URL,
        document_host="fund.example.com",
        document_type=ApprovedDocumentType.FACTSHEET,
        approval_reference="review-2026-10-28",
        access_tier=AccessTier.REQUIRES_AUTHENTICATION,
    )])


def canonical(**changes):
    return dict(globalInstrumentId=str(NEW_ID), isin="INF204KB14I2",
                primarySymbol="NIFTYBEES", assetType="ETF",
                status="ACTIVE", primaryExchange="NSE") | changes


def test_fresh_install_rebinds_without_granting_new_access():
    old = approved()
    new = bind_approved_sources_to_canonical_universe(old, [canonical()])
    source = new.source_for_url(NEW_ID, URL)
    assert source is not None
    assert source.instrument_id == NEW_ID
    assert source.isin == "INF204KB14I2"
    assert source.approval_reference == "review-2026-10-28"
    assert source.access_tier is AccessTier.REQUIRES_AUTHENTICATION
    assert new.source_for_url(OLD_ID, URL) is None
    # The operator-reviewed registry remains immutable for another database.
    assert old.source_for_url(OLD_ID, URL) is not None


@pytest.mark.parametrize("change", [
    {"isin": "INF000000000"}, {"primarySymbol": "WRONG"},
])
def test_different_valid_identity_is_not_approved(change):
    source = bind_approved_sources_to_canonical_universe(
        approved(), [canonical(**change)])
    assert source.instrument_count() == 0


@pytest.mark.parametrize("change", [
    {"assetType": "EQUITY"}, {"status": "INACTIVE"},
    {"primaryExchange": "BSE"},
])
def test_nonactive_non_nse_or_nonequity_cannot_rebind(change):
    with pytest.raises(ValueError, match="CANONICAL_ETF_IDENTITY_UNVERIFIED"):
        bind_approved_sources_to_canonical_universe(approved(), [canonical(**change)])


@pytest.mark.parametrize("change", [
    {"globalInstrumentId": "not-a-uuid"},
    {"globalInstrumentId": None},
])
def test_bad_uuid_rejected(change):
    with pytest.raises(ValueError, match="CANONICAL_ETF_UUID_INVALID"):
        bind_approved_sources_to_canonical_universe(approved(), [canonical(**change)])


def test_ambiguous_canonical_rows_rejected():
    with pytest.raises(ValueError, match="CANONICAL_ETF_IDENTITY_AMBIGUOUS"):
        bind_approved_sources_to_canonical_universe(
            approved(), [canonical(), canonical(globalInstrumentId=str(UUID(int=99)))])


def test_factory_binds_existing_reviewed_sources_without_enabling_acquisition():
    pipeline = EtfOfficialDocumentAcquisitionPipeline.for_canonical_universe(
        approved(), [canonical()], Settings())
    source = pipeline._approved_sources.source_for_url(NEW_ID, URL)
    assert source is not None
    assert source.access_tier is AccessTier.REQUIRES_AUTHENTICATION
