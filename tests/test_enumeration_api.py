"""HTTP-level tests for POST /turnpike/enumerate and cursor security."""

from __future__ import annotations

import json
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import cursor as cursor_module


def pairwise(points: list[int]) -> list[int]:
    return [b - a for i, a in enumerate(points) for b in points[i + 1 :]]


HOMOMETRIC = [0, 1, 4, 10, 12, 17]
HOM_DISTANCES = pairwise(HOMOMETRIC)
HOM_CLASSES = [
    [0, 1, 4, 10, 12, 17],
    [0, 1, 8, 11, 13, 17],
]


def enumerate_all(
    client: TestClient,
    length: int,
    n: int,
    distances: list[int],
    page_size: int = 100,
    node_budget: int = 1_000_000,
) -> tuple[list[list[int]], int, list[dict[str, object]]]:
    """Paginate to completion, returning classes, node total and raw pages."""
    cursor: str | None = None
    classes: list[list[int]] = []
    total_nodes = 0
    pages: list[dict[str, object]] = []
    while True:
        body = {
            "L": length,
            "n": n,
            "distances": distances,
            "page_size": page_size,
            "node_budget": node_budget,
        }
        if cursor is not None:
            body["cursor"] = cursor
        response = client.post("/turnpike/enumerate", json=body)
        assert response.status_code == 200, response.text
        page = response.json()
        pages.append(page)
        classes.extend(page["solutions"])
        total_nodes += page["nodes_used"]
        if page["complete"]:
            assert page["cursor"] is None
            assert page["total_classes"] == len(classes)
            break
        assert page["cursor"]
        assert page["total_classes"] is None
        cursor = page["cursor"]
    return classes, total_nodes, pages


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# --------------------------------------------------------------------- basics


def test_first_page_shape_and_completion(client: TestClient) -> None:
    response = client.post(
        "/turnpike/enumerate",
        json={
            "L": 17,
            "n": 6,
            "distances": HOM_DISTANCES,
            "page_size": 100,
            "node_budget": 1_000_000,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "solutions": HOM_CLASSES,
        "emitted": 0,
        "nodes_used": body["nodes_used"],
        "complete": True,
        "stop_reason": "complete",
        "total_classes": 2,
        "cursor": None,
    }
    assert body["nodes_used"] >= 1
    for witness in body["solutions"]:
        assert Counter(pairwise(witness)) == Counter(HOM_DISTANCES)


def test_multipage_ambiguous_page_size_one(client: TestClient) -> None:
    classes, total_nodes, pages = enumerate_all(
        client, 17, 6, HOM_DISTANCES, page_size=1
    )
    assert classes == HOM_CLASSES
    # page_full after each of the two classes, then an empty closing page once
    # the remaining subtree has been exhausted.
    assert [page["solutions"] for page in pages] == [
        [HOM_CLASSES[0]],
        [HOM_CLASSES[1]],
        [],
    ]
    assert [page["emitted"] for page in pages] == [0, 1, 2]
    assert all(page["nodes_used"] >= 0 for page in pages)
    assert total_nodes > 0


def test_witness_distances_are_regenerated_before_emission(
    client: TestClient,
) -> None:
    classes, _nodes, _pages = enumerate_all(client, 17, 6, HOM_DISTANCES)
    for witness in classes:
        assert Counter(pairwise(witness)) == Counter(HOM_DISTANCES)


def test_impossible_ends_complete_zero(client: TestClient) -> None:
    response = client.post(
        "/turnpike/enumerate",
        json={
            "L": 5,
            "n": 3,
            "distances": [3, 4, 5],
            "page_size": 10,
            "node_budget": 10,
        },
    )
    body = response.json()
    assert body["complete"] is True
    assert body["stop_reason"] == "complete"
    assert body["total_classes"] == 0
    assert body["solutions"] == []
    assert body["cursor"] is None
    assert body["nodes_used"] == 1


def test_mirror_symmetric_solution_single_class(client: TestClient) -> None:
    points = [0, 1, 3, 4]
    classes, _nodes, _ = enumerate_all(
        client, 4, 4, pairwise(points), page_size=1, node_budget=1
    )
    assert classes == [points]


def test_budget_exhaustion_suspends_and_resumes(client: TestClient) -> None:
    body = {
        "L": 17,
        "n": 6,
        "distances": HOM_DISTANCES,
        "page_size": 100,
        "node_budget": 1,
    }
    first = client.post("/turnpike/enumerate", json=body).json()
    assert first["complete"] is False
    assert first["stop_reason"] == "budget_exhausted"
    assert first["solutions"] == []
    assert first["nodes_used"] == 1
    assert first["cursor"]

    # Finish with a big budget; exactly one attempt was already paid for.
    classes, total_nodes, _ = enumerate_all(
        client, 17, 6, HOM_DISTANCES, page_size=100, node_budget=1
    )
    assert classes == HOM_CLASSES


def test_changing_page_size_and_budget_keeps_sequence(
    client: TestClient,
) -> None:
    plans = [(1, 1), (1, 100), (2, 3), (100, 1), (3, 2), (1, 1_000_000)]
    sequences = []
    node_totals = set()
    for page_size, budget in plans:
        classes, total_nodes, _pages = enumerate_all(
            client, 17, 6, HOM_DISTANCES, page_size, budget
        )
        sequences.append(classes)
        node_totals.add(total_nodes)
    assert all(sequence == HOM_CLASSES for sequence in sequences)
    assert len(node_totals) == 1  # paging cannot change total node consumption


# ----------------------------------------------------------- determinism


def test_replay_same_cursor_is_byte_identical(client: TestClient) -> None:
    body = json.dumps(
        {
            "L": 17,
            "n": 6,
            "distances": HOM_DISTANCES,
            "page_size": 1,
            "node_budget": 1_000_000,
        },
        separators=(",", ":"),
    )
    first = client.post("/turnpike/enumerate", content=body).content
    second = client.post("/turnpike/enumerate", content=body).content
    assert first == second

    cursor = json.loads(first)["cursor"]
    continuation = json.dumps(
        {
            "L": 17,
            "n": 6,
            "distances": HOM_DISTANCES,
            "page_size": 1,
            "node_budget": 1_000_000,
            "cursor": cursor,
        },
        separators=(",", ":"),
    )
    assert client.post(
        "/turnpike/enumerate", content=continuation
    ).content == client.post(
        "/turnpike/enumerate", content=continuation
    ).content


def test_reordered_distances_are_the_same_request(client: TestClient) -> None:
    body_a = {
        "L": 17,
        "n": 6,
        "distances": HOM_DISTANCES,
        "page_size": 1,
        "node_budget": 1_000_000,
    }
    body_b = dict(body_a)
    body_b["distances"] = list(reversed(HOM_DISTANCES))
    first = client.post("/turnpike/enumerate", json=body_a)
    second = client.post("/turnpike/enumerate", json=body_b)
    assert first.content == second.content

    # A cursor continues under any permutation of the same multiset.
    cursor = first.json()["cursor"]
    cont_a = dict(body_a, cursor=cursor)
    cont_b = dict(body_b, cursor=cursor)
    assert client.post(
        "/turnpike/enumerate", json=cont_a
    ).content == client.post("/turnpike/enumerate", json=cont_b).content


# ----------------------------------------------------------- cursor attacks


def _first_cursor(client: TestClient) -> str:
    return client.post(
        "/turnpike/enumerate",
        json={
            "L": 17,
            "n": 6,
            "distances": HOM_DISTANCES,
            "page_size": 1,
            "node_budget": 1_000_000,
        },
    ).json()["cursor"]


def _use_cursor(client: TestClient, cursor: object) -> dict[str, object]:
    response = client.post(
        "/turnpike/enumerate",
        json={
            "L": 17,
            "n": 6,
            "distances": HOM_DISTANCES,
            "page_size": 1,
            "node_budget": 1_000_000,
            "cursor": cursor,
        },
    )
    assert response.status_code == 422
    return response.json()["error"]


def test_cursor_tampering_is_rejected_without_leakage(client: TestClient) -> None:
    cursor = _first_cursor(client)
    head, payload, signature = cursor.split(".")
    variants = {
        "truncated": cursor[:-6],
        "flipped_signature": f"{head}.{payload}.{'A' if not signature.startswith('A') else 'B'}{signature[1:]}",
        "flipped_payload": f"{head}.{payload[:-1]}{'A' if payload[-1] != 'A' else 'B'}.{signature}",
        "wrong_shape": "tp1.abc",
        "unknown_version": f"tp9.{payload}.{signature}",
        "empty": "",
        "garbage": "not-a-cursor",
        "missing_signature": f"{head}.{payload}",
        "extra_segment": f"{head}.{payload}.{signature}.x",
    }
    for name, token in variants.items():
        error = _use_cursor(client, token)
        assert error["item"]["field"] == "cursor"
        assert error["code"] in {
            "cursor_malformed",
            "cursor_bad_signature",
            "cursor_unknown_version",
        }, (name, error)
        # No internals, stacks or key material ever escape.
        assert "Traceback" not in json.dumps(error)
        assert "secret" not in json.dumps(error).lower()


def test_unknown_version_distinct_code(client: TestClient) -> None:
    cursor = _first_cursor(client)
    _version, rest = cursor.split(".", 1)
    error = _use_cursor(client, f"tp999.{rest}")
    assert error["code"] == "cursor_unknown_version"


def test_cursor_bound_to_other_input_is_rejected(client: TestClient) -> None:
    cursor = _first_cursor(client)

    # Different L, still a legal n(n-1)/2 multiset in range.
    response = client.post(
        "/turnpike/enumerate",
        json={
            "L": 18,
            "n": 6,
            "distances": [18] + HOM_DISTANCES[1:],
            "page_size": 1,
            "node_budget": 1_000_000,
            "cursor": cursor,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cursor_input_mismatch"

    # Same L, same n, different multiset (all entries still in range).
    altered = list(HOM_DISTANCES)
    altered[0] = 17
    response = client.post(
        "/turnpike/enumerate",
        json={
            "L": 17,
            "n": 6,
            "distances": altered,
            "page_size": 1,
            "node_budget": 1_000_000,
            "cursor": cursor,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cursor_input_mismatch"


def test_oversized_cursor_rejected(client: TestClient) -> None:
    huge = "tp1." + "A" * (cursor_module.MAX_TOKEN_BYTES + 10)
    error = _use_cursor(client, huge)
    assert error["code"] == "cursor_too_large"


def test_cursor_secret_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient as TC

    monkeypatch.setenv("TURNPIKE_CURSOR_SECRET", "key-alpha")
    alpha = TC(app)
    cursor = _first_cursor(alpha)
    # Same key: replay works byte-identically (simulates a restart that keeps
    # the configured secret).
    alpha_again = TC(app)
    body = {
        "L": 17,
        "n": 6,
        "distances": HOM_DISTANCES,
        "page_size": 1,
        "node_budget": 1_000_000,
        "cursor": cursor,
    }
    ok = alpha_again.post("/turnpike/enumerate", json=body)
    assert ok.status_code == 200, ok.text

    # Rotated key: every old cursor must be refused.
    monkeypatch.setenv("TURNPIKE_CURSOR_SECRET", "key-beta")
    beta = TC(app)
    refused = beta.post("/turnpike/enumerate", json=body)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "cursor_bad_signature"


def test_restart_then_complete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cursor minted before a service restart remains usable afterwards as
    # long as the configured secret is unchanged.
    monkeypatch.setenv("TURNPIKE_CURSOR_SECRET", "restart-key")
    before = TestClient(app)
    cursor = _first_cursor(before)

    after = TestClient(app)  # fresh client against a "restarted" server
    response = after.post(
        "/turnpike/enumerate",
        json={
            "L": 17,
            "n": 6,
            "distances": HOM_DISTANCES,
            "page_size": 100,
            "node_budget": 1_000_000,
            "cursor": cursor,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["solutions"] == [HOM_CLASSES[1]]
    assert body["emitted"] == 1
    assert body["complete"] is True and body["total_classes"] == 2


# ----------------------------------------------------- shared request rules


def test_count_and_range_rules_apply(client: TestClient) -> None:
    response = client.post(
        "/turnpike/enumerate",
        json={"L": 10, "n": 4, "distances": [10], "page_size": 1, "node_budget": 1},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_distance_count"

    response = client.post(
        "/turnpike/enumerate",
        json={
            "L": 5,
            "n": 3,
            "distances": [2, 6, 5],
            "page_size": 1,
            "node_budget": 1,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "distance_out_of_range"


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_size", 0),
        ("page_size", 101),
        ("node_budget", 0),
        ("node_budget", 1_000_001),
        ("page_size", 1.5),
        ("cursor", 123),
    ],
)
def test_parameter_bounds_are_structured(
    client: TestClient, field: str, value: object
) -> None:
    body: dict[str, object] = {
        "L": 9,
        "n": 2,
        "distances": [9],
        "page_size": 5,
        "node_budget": 1,
        field: value,
    }
    response = client.post("/turnpike/enumerate", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_extra_field_rejected(client: TestClient) -> None:
    response = client.post(
        "/turnpike/enumerate",
        json={
            "L": 9,
            "n": 2,
            "distances": [9],
            "page_size": 5,
            "node_budget": 1,
            "bogus": 0,
        },
    )
    assert response.status_code == 422


# ------------------------------------------------- original endpoint contract


def test_original_turnpike_endpoint_unchanged(client: TestClient) -> None:
    # Unique / impossible / ambiguous byte shapes remain as before.
    unique = client.post(
        "/turnpike", json={"L": 10, "n": 5, "distances": pairwise([0, 2, 4, 7, 10])}
    )
    assert unique.json() == {"status": "unique", "solution": [0, 2, 4, 7, 10]}

    impossible = client.post(
        "/turnpike", json={"L": 5, "n": 3, "distances": [3, 4, 5]}
    )
    assert impossible.json() == {"status": "impossible"}

    ambiguous = client.post(
        "/turnpike", json={"L": 17, "n": 6, "distances": HOM_DISTANCES}
    )
    assert ambiguous.json() == {"status": "ambiguous", "solutions": HOM_CLASSES}
