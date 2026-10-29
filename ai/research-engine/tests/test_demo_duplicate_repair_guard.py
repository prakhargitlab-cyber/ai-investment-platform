from unittest.mock import MagicMock

import pytest

from app.models import (
    DocumentStatus,
    ReliabilityLevel,
    SourceClassification,
    SourceMode,
    SourceType,
)
from app.repository import ResearchRepository


def demo_fixture():
    return dict(
        original_url="https://fixture.example/demo-duplicate",
        source_type=SourceType.NEWS,
        source_name="Demo fixture",
        publisher="Demo",
        content_type="text/html",
        body="<html><body>Demo duplicate ingestion fixture.</body></html>",
        reliability=ReliabilityLevel.LEVEL_C,
        source_mode=SourceMode.DEMO,
        source_classification=SourceClassification.REPUTABLE_NEWS,
    )


def test_demo_duplicate_does_not_attempt_durable_repair():
    """Duplicate DEMO ingestion must not call durable repair."""
    repo = ResearchRepository()

    repo._persist_official_document_repair = MagicMock(
        side_effect=AssertionError("DEMO attempted durable repair")
    )

    first = repo.ingest_fixture(**demo_fixture())
    duplicate = repo.ingest_fixture(**demo_fixture())

    repo._persist_official_document_repair.assert_not_called()
    assert duplicate.status == DocumentStatus.DUPLICATE
    assert duplicate.duplicate_of_document_id == first.document_id


@pytest.mark.asyncio
async def test_async_demo_duplicate_does_not_attempt_durable_repair():
    repo = ResearchRepository()
    first = repo.ingest_fixture(**demo_fixture())
    repo._persist_official_document_repair = MagicMock(
        side_effect=AssertionError("DEMO attempted durable repair")
    )
    duplicate = await repo._apply_prepared_ingested_document_async(
        first.model_copy(), document_status=DocumentStatus.PARSED)
    repo._persist_official_document_repair.assert_not_called()
    assert duplicate.status == DocumentStatus.DUPLICATE
    assert duplicate.duplicate_of_document_id == first.document_id


def test_repair_helper_rejects_demo_even_if_called_directly():
    repo = ResearchRepository()
    document = repo.ingest_fixture(**demo_fixture())
    repo._persistence = MagicMock()
    assert repo._persist_official_document_repair(document) is False
    repo._persistence.repair_official_document.assert_not_called()
