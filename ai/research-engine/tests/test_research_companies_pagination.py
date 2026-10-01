"""Post-commit review of 8eaed27: pagination contract for
GET /api/v1/research/companies.

8eaed27 changed this endpoint from returning a raw array to a paginated
envelope. This test proves the contract items the review asked to verify:
deterministic ordering, page/pageSize validation and bounds, total/hasMore
correctness, and well-behaved out-of-range/empty-page handling -- using the
same TestClient(app) + demo repository already used by test_research_api.py.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app, repository


def test_deterministic_ordering_across_repeated_calls_and_pages():
    client = TestClient(app)
    first = client.get("/api/v1/research/companies", params={"page": 1, "page_size": 200}).json()
    second = client.get("/api/v1/research/companies", params={"page": 1, "page_size": 200}).json()
    assert [item["instrumentId"] for item in first["items"]] == [
        item["instrumentId"] for item in second["items"]
    ]


def test_default_call_matches_documented_backward_compatible_first_page():
    client = TestClient(app)
    no_args = client.get("/api/v1/research/companies").json()
    explicit_defaults = client.get(
        "/api/v1/research/companies", params={"page": 1, "page_size": 50}
    ).json()
    assert no_args == explicit_defaults
    assert no_args["page"] == 1
    assert no_args["pageSize"] == 50


def test_page_below_one_is_rejected():
    client = TestClient(app)
    response = client.get("/api/v1/research/companies", params={"page": 0})
    assert response.status_code == 422


def test_page_size_out_of_bounds_is_rejected():
    client = TestClient(app)
    assert client.get("/api/v1/research/companies", params={"page_size": 0}).status_code == 422
    assert client.get("/api/v1/research/companies", params={"page_size": 201}).status_code == 422
    # The boundary values themselves must be accepted.
    assert client.get("/api/v1/research/companies", params={"page_size": 1}).status_code == 200
    assert client.get("/api/v1/research/companies", params={"page_size": 200}).status_code == 200


def test_total_matches_repository_profile_count():
    client = TestClient(app)
    body = client.get("/api/v1/research/companies", params={"page_size": 200}).json()
    assert body["total"] == len(repository.list_profiles())


def test_has_more_is_true_mid_list_and_false_on_the_last_page():
    client = TestClient(app)
    total = len(repository.list_profiles())
    assert total >= 2, "demo fixture must have at least 2 profiles for this test to be meaningful"

    first_page = client.get(
        "/api/v1/research/companies", params={"page": 1, "page_size": 1}
    ).json()
    assert first_page["hasMore"] is True

    last_page_number = total
    last_page = client.get(
        "/api/v1/research/companies", params={"page": last_page_number, "page_size": 1}
    ).json()
    assert last_page["hasMore"] is False
    assert len(last_page["items"]) == 1


def test_out_of_range_page_returns_empty_items_not_an_error():
    client = TestClient(app)
    total = len(repository.list_profiles())
    response = client.get(
        "/api/v1/research/companies", params={"page": total + 1000, "page_size": 50}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["hasMore"] is False
    assert body["total"] == total


def test_pages_partition_the_full_list_without_gaps_or_duplicates():
    client = TestClient(app)
    total = len(repository.list_profiles())
    page_size = 1
    seen_ids: list[str] = []
    page = 1
    while True:
        body = client.get(
            "/api/v1/research/companies", params={"page": page, "page_size": page_size}
        ).json()
        seen_ids.extend(item["instrumentId"] for item in body["items"])
        if not body["hasMore"]:
            break
        page += 1
    assert len(seen_ids) == total
    assert len(set(seen_ids)) == total  # no duplicates across page boundaries
