"""Opt-in adapter smoke against an isolated database migrated by Java Flyway."""
import os
import pytest
from uuid import UUID
from urllib.parse import urlparse
from app.settings import Settings
from app.postgres_persistence import PostgresResearchPersistence
from test_recommendation_lifecycle import snapshot, publish


@pytest.mark.skipif(
    not os.environ.get("RECOMMENDATION_TEST_PG_DSN"),
    reason="Requires isolated Flyway-migrated PostgreSQL"
)
def test_postgres_publish_read_history_and_immutable_trigger():
    parsed = urlparse(os.environ["RECOMMENDATION_TEST_PG_DSN"])

    # Safety: allow only our dedicated disposable PostgreSQL instance
    assert parsed.scheme == "postgresql"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port == 55439
    assert parsed.path == "/recommendation_validation"
    assert parsed.username == "radar_test_admin"

    store = PostgresResearchPersistence(
        Settings(
            research_database_backend="postgres",
            research_database_host=parsed.hostname,
            research_database_port=parsed.port,
            research_database_name=parsed.path.lstrip("/"),
            research_database_user=parsed.username,
            research_database_password=parsed.password,
            research_database_schema="research",
            research_database_ssl_mode="disable",
        )
    )
    s = snapshot(87123)
    published = publish(store, [s])
    assert store.opportunity_current()['cycle_id'] == published['cycle_id']
    assert len(store.recommendation_history(UUID(int=87123))) == 1
    with pytest.raises(Exception, match='RECOMMENDATION_HISTORY_IS_IMMUTABLE'):
        with store._connection:
            store._connection.execute('UPDATE stock_recommendation_history SET payload = payload')
