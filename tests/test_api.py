"""HTTP-level tests for the FastAPI service."""

from __future__ import annotations

import json
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.main import app


def pairwise(points: list[int]) -> list[int]:
    return [b - a for i, a in enumerate(points) for b in points[i + 1 :]]


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unique_request(client: TestClient) -> None:
    points = [0, 2, 4, 7, 10]
    response = client.post(
        "/turnpike",
        json={"L": 10, "n": 5, "distances": pairwise(points)},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "unique", "solution": points}


def test_impossible_request(client: TestClient) -> None:
    response = client.post(
        "/turnpike", json={"L": 5, "n": 3, "distances": [3, 4, 5]}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "impossible"}


def test_ambiguous_request_returns_two_canonical_witnesses(
    client: TestClient,
) -> None:
    points = [0, 1, 4, 10, 12, 17]
    distances = pairwise(points)
    response = client.post(
        "/turnpike", json={"L": 17, "n": 6, "distances": distances}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ambiguous"
    first, second = body["solutions"]
    assert first < second
    for witness in body["solutions"]:
        assert witness[0] == 0 and witness[-1] == 17
        assert Counter(pairwise(witness)) == Counter(distances)
        # canonical: no witness is lexicographically above its mirror
        mirror = [17 - x for x in reversed(witness)]
        assert mirror[0] == 0
        assert witness <= mirror


def test_distance_count_mismatch_points_at_field(client: TestClient) -> None:
    response = client.post(
        "/turnpike", json={"L": 10, "n": 4, "distances": [10]}
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_distance_count"
    assert error["expected"] == 6 and error["actual"] == 1
    assert error["item"]["field"] == "distances"
    assert error["item"]["expected_size"] == 6


def test_out_of_range_distance_reports_first_item(client: TestClient) -> None:
    response = client.post(
        "/turnpike",
        json={"L": 5, "n": 3, "distances": [2, 6, 5]},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "distance_out_of_range"
    assert error["item"]["index"] == 1
    assert error["item"]["value"] == 6
    assert error["item"]["upper"] == 5


def test_zero_distance_reports_first_item(client: TestClient) -> None:
    response = client.post(
        "/turnpike", json={"L": 5, "n": 3, "distances": [0, 5, 5]}
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "distance_out_of_range"
    assert error["item"]["index"] == 0


def test_out_of_range_L_and_n_are_structured(client: TestClient) -> None:
    bad_L = client.post(
        "/turnpike",
        json={"L": 10**9 + 1, "n": 2, "distances": [1]},
    )
    assert bad_L.status_code == 422
    assert bad_L.json()["error"]["code"] == "invalid_request"

    bad_n = client.post(
        "/turnpike",
        json={"L": 5, "n": 1, "distances": []},
    )
    assert bad_n.status_code == 422
    assert bad_n.json()["error"]["code"] == "invalid_request"


def test_wrong_type_and_extra_field_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/turnpike",
        json={"L": 5, "n": 3, "distances": [1, 2.5, 5]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"

    response = client.post(
        "/turnpike",
        json={"L": 5, "n": 3, "distances": [1, 4, 5], "bogus": 1},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_repeated_requests_are_byte_identical(client: TestClient) -> None:
    points = [0, 1, 4, 10, 12, 17]
    payload = json.dumps(
        {"L": 17, "n": 6, "distances": pairwise(points)},
        separators=(",", ":"),
    )
    first = client.post("/turnpike", content=payload).content
    second = client.post("/turnpike", content=payload).content
    assert first == second
    # determinism holds even when duplicate inputs are shuffled
    distances = pairwise(points)
    payload2 = json.dumps(
        {"L": 17, "n": 6, "distances": list(reversed(distances))},
        separators=(",", ":"),
    )
    third = client.post("/turnpike", content=payload2).content
    assert third == first


def test_bounds_extremes_accepted(client: TestClient) -> None:
    response = client.post(
        "/turnpike",
        json={"L": 10**9, "n": 2, "distances": [10**9]},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "unique", "solution": [0, 10**9]}
