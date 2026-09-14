"""Opt-in adapter smoke against an isolated database migrated by Java Flyway."""
import os
import pytest
from uuid import UUID
from app.settings import Settings
from app.postgres_persistence import PostgresResearchPersistence
from test_recommendation_lifecycle import snapshot, publish


@pytest.mark.skipif(not os.environ.get('RECOMMENDATION_TEST_PG_PORT'), reason='Requires disposable Flyway-migrated PostgreSQL')
def test_postgres_publish_read_history_and_immutable_trigger():
    store = PostgresResearchPersistence(Settings(research_database_host='127.0.0.1',
        research_database_port=int(os.environ['RECOMMENDATION_TEST_PG_PORT']),
        research_database_name='recommendation_validation', research_database_user='postgres',
        research_database_password='', research_database_schema='research', research_database_ssl_mode='disable'))
    s = snapshot(87123)
    published = publish(store, [s])
    assert store.opportunity_current()['cycle_id'] == published['cycle_id']
    assert len(store.recommendation_history(UUID(int=87123))) == 1
    with pytest.raises(Exception, match='RECOMMENDATION_HISTORY_IS_IMMUTABLE'):
        with store._connection:
            store._connection.execute('UPDATE stock_recommendation_history SET payload = payload')
