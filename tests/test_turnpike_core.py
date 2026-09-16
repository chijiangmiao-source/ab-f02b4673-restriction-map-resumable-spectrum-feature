"""Unit tests for the turnpike reconstruction core."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from app.turnpike import (
    AMBIGUOUS,
    IMPOSSIBLE,
    UNIQUE,
    _canonical,
    _mirror,
    reconstruct,
)


def pairwise(points: list[int]) -> list[int]:
    """All pairwise distances, generated independently of the solver."""
    out: list[int] = []
    for i, left in enumerate(points):
        for right in points[i + 1 :]:
            out.append(right - left)
    return out


def assert_witness_reproduces(
    witness: list[int], length: int, distances: list[int]
) -> None:
    assert witness[0] == 0
    assert witness[-1] == length
    assert witness == sorted(witness)
    assert len(set(witness)) == len(witness)
    assert Counter(pairwise(witness)) == Counter(distances)


def test_n2_minimum_instance() -> None:
    result = reconstruct(5, [5])
    assert result == {"status": UNIQUE, "solution": [0, 5]}


def test_unique_small_instance() -> None:
    points = [0, 2, 4, 7, 10]
    result = reconstruct(10, pairwise(points))
    assert result["status"] is UNIQUE
    assert result["solution"] == points


def test_repeated_distances_are_preserved() -> None:
    # [0, 2, 4] gives the multiset {2, 2, 4}: duplicates matter.
    result = reconstruct(4, [2, 4, 2])
    assert result["status"] is UNIQUE
    assert result["solution"] == [0, 2, 4]


def test_missing_length_distance_is_impossible() -> None:
    assert reconstruct(10, [1, 1, 8]) == {"status": IMPOSSIBLE}


def test_contradictory_multiset_is_impossible_without_leakage() -> None:
    # Three points must have one distance equal to the sum of the other two;
    # 3,4,5 cannot be embedded on a line of length 5.
    result = reconstruct(5, [3, 4, 5])
    assert result == {"status": IMPOSSIBLE}
    assert "solution" not in result and "solutions" not in result


def test_nontriangular_count_is_impossible() -> None:
    points = [0, 2, 4, 7, 10]
    distances = pairwise(points) + [10]  # 11 is not n(n-1)/2 for any n
    assert reconstruct(10, distances) == {"status": IMPOSSIBLE}


def test_known_homometric_pair_is_ambiguous() -> None:
    # Classical non-congruent homometric sets.
    points = [0, 1, 4, 10, 12, 17]
    result = reconstruct(17, pairwise(points))
    assert result["status"] is AMBIGUOUS
    first, second = result["solutions"]
    assert first == [0, 1, 4, 10, 12, 17]
    assert second == [0, 1, 8, 11, 13, 17]
    assert first < second  # lexicographically smallest two
    for witness in result["solutions"]:
        assert_witness_reproduces(witness, 17, pairwise(points))


def test_mirror_images_are_the_same_solution() -> None:
    points = [0, 2, 4, 7, 10]
    reflected = _mirror(tuple(points), 10)
    a = reconstruct(10, pairwise(points))
    b = reconstruct(10, pairwise(list(reflected)))
    assert a == b
    assert a["solution"] == list(_canonical(tuple(points), 10))


def test_reflection_is_an_involution() -> None:
    t = (0, 2, 4, 7, 10)
    assert _mirror(_mirror(t, 10), 10) == t


@pytest.mark.parametrize("seed", range(40))
def test_random_instances_round_trip(seed: int) -> None:
    rng = random.Random(seed)
    length = rng.randint(2, 5000)
    n = rng.randint(2, 20)
    interior = rng.sample(range(1, length), min(n - 2, length - 1))
    points = sorted([0, length] + interior)
    distances = pairwise(points)

    result = reconstruct(length, distances)
    assert result["status"] in (UNIQUE, AMBIGUOUS)

    generated = points
    canonical = list(_canonical(tuple(generated), length))
    if result["status"] is UNIQUE:
        assert result["solution"] == canonical
        assert_witness_reproduces(result["solution"], length, distances)
    else:
        witnesses = result["solutions"]
        assert witnesses[0] < witnesses[1]
        assert canonical in witnesses
        for witness in witnesses:
            assert_witness_reproduces(witness, length, distances)


def test_dense_grid_n32() -> None:
    points = list(range(0, 63, 2))  # 32 points, L = 62
    assert len(points) == 32
    result = reconstruct(62, pairwise(points))
    assert result["status"] is UNIQUE
    assert result["solution"] == points


def test_n32_sparse_large_coordinates() -> None:
    rng = random.Random(1234)
    length = 10**9
    points = sorted([0, length] + rng.sample(range(1, length), 30))
    result = reconstruct(length, pairwise(points))
    assert result["status"] is UNIQUE
    assert_witness_reproduces(result["solution"], length, pairwise(points))
