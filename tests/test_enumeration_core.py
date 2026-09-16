"""Tests for the resumable enumeration search kernel."""

from __future__ import annotations

import random
from collections import Counter
from itertools import combinations

import pytest

from app.cursor import decode_cursor, encode_cursor, input_digest
from app.turnpike import (
    BUDGET_EXHAUSTED,
    COMPLETE,
    PAGE_FULL,
    TurnpikeEnumerator,
)


def pairwise(points: list[int]) -> list[int]:
    return [b - a for i, a in enumerate(points) for b in points[i + 1 :]]


def oracle_classes(length: int, points: tuple[int, ...]) -> set[tuple[int, ...]]:
    """Brute-force canonical mirror class of a single point set."""
    mirror = (0,) + tuple(length - x for x in reversed(points[1:-1])) + (length,)
    return {min(points, mirror)}


def drain(
    length: int,
    distances: list[int],
    n: int,
    page_size: int,
    budget: int,
    restart: bool = False,
) -> tuple[list[tuple[int, ...]], int, int]:
    """Drive the kernel to completion, resuming through serialised cursors.

    Returns (discovery_sequence, total_nodes, total_classes).
    """
    original = Counter(distances)
    digest = input_digest(length, n, original)
    search = TurnpikeEnumerator(length, n, original)
    sequence: list[tuple[int, ...]] = []
    total_nodes = 0
    while True:
        page, used, reason = search.run(page_size, budget)
        total_nodes += used
        sequence.extend(tuple(s) for s in page)
        if reason == COMPLETE:
            return sequence, total_nodes, search.emitted
        assert reason in (PAGE_FULL, BUDGET_EXHAUSTED)
        assert search.frames  # suspended mid-search
        if restart:
            # Simulate a full stateless restart: serialise, decode, rebuild.
            token = encode_cursor(search.to_state(), length, n, digest)
            state = decode_cursor(token, length, n, digest)
            search = TurnpikeEnumerator.from_state(
                state, length, n, Counter(distances)
            )
        else:
            search = TurnpikeEnumerator.from_state(
                search.to_state(), length, n, Counter(distances)
            )


HOMOMETRIC = [0, 1, 4, 10, 12, 17]
HOM_DISTANCES = pairwise(HOMOMETRIC)
HOM_CLASSES = [
    (0, 1, 4, 10, 12, 17),
    (0, 1, 8, 11, 13, 17),
]


def test_ambiguous_instance_multipage_with_page_size_one() -> None:
    sequence, nodes, total = drain(17, HOM_DISTANCES, 6, page_size=1, budget=1_000_000)
    assert sequence == HOM_CLASSES
    assert total == 2
    # Page size 1 forces two page_full suspensions and one empty completion.
    assert nodes > 0


def test_ambiguous_instance_single_page() -> None:
    sequence, _nodes, total = drain(17, HOM_DISTANCES, 6, 100, 1_000_000)
    assert sequence == HOM_CLASSES and total == 2


def test_emitted_count_tracks_pages() -> None:
    original = Counter(HOM_DISTANCES)
    search = TurnpikeEnumerator(17, 6, original)
    emitted_seen: list[int] = []
    pages: list[list[list[int]]] = []
    while True:
        before = search.emitted
        page, _used, reason = search.run(1, 1_000_000)
        emitted_seen.append(before)
        pages.append(page)
        if reason == COMPLETE:
            break
        search = TurnpikeEnumerator.from_state(
            search.to_state(), 17, 6, original
        )
    assert [w for page in pages for w in page] == [list(c) for c in HOM_CLASSES]
    # 0 before first, 1 before second, 2 before the empty closing page.
    assert emitted_seen == [0, 1, 2]
    assert pages[-1] == []


def test_page_size_and_budget_only_move_boundaries() -> None:
    configs = [
        (100, 1_000_000),
        (1, 1),
        (1, 2),
        (2, 3),
        (3, 7),
        (1, 1_000_000),
        (50, 1),
        (2, 1),
        (1, 5),
        (7, 4),
    ]
    results = [drain(17, HOM_DISTANCES, 6, ps, b, restart=True) for ps, b in configs]
    base = results[0][0]
    for sequence, _nodes, total in results:
        assert sequence == base  # identical concatenated discovery sequence
        assert len(sequence) == len(set(sequence)) == total == 2
    # Total node consumption is a property of the search, not the paging.
    node_totals = {nodes for _seq, nodes, _t in results}
    assert node_totals == {results[0][1]}


def test_budget_exactly_exhausted_completes_on_last_attempt() -> None:
    # Measure the full search's node count with an unlimited budget, then run
    # again with that exact budget: the last request must both spend the whole
    # budget and report completion (priority: complete > budget_exhausted).
    original = Counter(HOM_DISTANCES)
    probe = TurnpikeEnumerator(17, 6, original)
    _page, exact_nodes, reason = probe.run(100, 10**12)
    assert reason == COMPLETE

    search = TurnpikeEnumerator(17, 6, Counter(HOM_DISTANCES))
    page, used, reason = search.run(100, exact_nodes)
    assert reason == COMPLETE
    assert used == exact_nodes
    assert [tuple(s) for s in page] == HOM_CLASSES

    # One fewer node: the search suspends instead.
    search = TurnpikeEnumerator(17, 6, Counter(HOM_DISTANCES))
    page, used, reason = search.run(100, exact_nodes - 1)
    assert reason == BUDGET_EXHAUSTED
    assert used == exact_nodes - 1


def test_budget_never_splits_an_attempt() -> None:
    # Budget 1 repeatedly always performs exactly one attempt per request and
    # the state stays internally consistent across every boundary.
    sequence, nodes, total = drain(17, HOM_DISTANCES, 6, 100, 1, restart=True)
    assert sequence == HOM_CLASSES and total == 2 and nodes >= 2


def test_node_accounts_for_every_failed_attempt_kind() -> None:
    # [3,4,5] on L=5: root d=5 gives the single candidate L-d=0, which is
    # immediately rejected as out of bounds: exactly one attempt, no witness.
    search = TurnpikeEnumerator(5, 3, Counter([3, 4, 5]))
    page, used, reason = search.run(10, 10)
    assert reason == COMPLETE
    assert used == 1
    assert page == [] and search.emitted == 0

    # Missing distance L needs no attempt at all.
    search = TurnpikeEnumerator(10, 3, Counter([1, 1, 8]))
    _page, used, reason = search.run(10, 10)
    assert reason == COMPLETE and used == 0 and search.emitted == 0


def test_stop_priority_page_full_beats_budget() -> None:
    # page_size=1 with budget to spare must stop as page_full, even though a
    # page boundary and remaining budget coexist.
    search = TurnpikeEnumerator(17, 6, Counter(HOM_DISTANCES))
    page, used, reason = search.run(1, 1_000_000)
    assert reason == PAGE_FULL and len(page) == 1


def test_impossible_completes_with_total_zero() -> None:
    sequence, nodes, total = drain(5, [3, 4, 5], 3, 10, 10, restart=True)
    assert sequence == [] and total == 0


def test_self_symmetric_class_emitted_once() -> None:
    # {0,1,3,4} on L=4 is its own mirror image.
    points = [0, 1, 3, 4]
    sequence, _nodes, total = drain(4, pairwise(points), 4, 1, 1, restart=True)
    assert sequence == [(0, 1, 3, 4)] and total == 1


def test_mirror_pair_is_one_class() -> None:
    points = [0, 2, 4, 7, 10]
    reflected = [0, 3, 6, 8, 10]
    a, _, ta = drain(10, pairwise(points), 5, 1, 1, restart=True)
    b, _, tb = drain(10, pairwise(reflected), 5, 1, 1, restart=True)
    assert a == b and ta == tb == 1
    assert a == [(0, 2, 4, 7, 10)]


def test_n2_costs_no_nodes() -> None:
    sequence, nodes, total = drain(9, [9], 2, 10, 1)
    assert sequence == [(0, 9)] and total == 1 and nodes == 0


@pytest.mark.parametrize("seed", range(60))
def test_paged_matches_unpaged_and_witnesses_verify(seed: int) -> None:
    rng = random.Random(seed)
    length = rng.randint(5, 80)
    n = rng.randint(2, min(10, length + 1))
    points = sorted([0, length] + rng.sample(range(1, length), n - 2))
    distances = pairwise(points)

    configs = [(100, 10**12), (1, 1), (3, 5), (1, 7), (2, 1_000_000)]
    runs = [
        drain(length, distances, n, ps, b, restart=True) for ps, b in configs
    ]
    base_sequence, base_nodes, base_total = runs[0]
    for sequence, nodes, total in runs:
        assert sequence == base_sequence
        assert nodes == base_nodes
        assert total == base_total
        assert len(sequence) == len(set(sequence))
        for witness in sequence:
            assert witness[0] == 0 and witness[-1] == length
            assert Counter(pairwise(list(witness))) == Counter(distances)
            mirror = (0,) + tuple(
                length - x for x in reversed(witness[1:-1])
            ) + (length,)
            assert witness <= mirror


def test_completeness_against_full_oracle() -> None:
    # Enumerate every subset for small boards and compare the paged kernel's
    # class set with an independent brute-force grouping.
    for length in range(4, 13):
        groups: dict[tuple[int, tuple[int, ...]], set[tuple[int, ...]]] = {}
        for k in range(0, min(length - 1, 6) + 1):
            for interior in combinations(range(1, length), k):
                points = (0,) + interior + (length,)
                signature = (
                    len(points),
                    tuple(sorted(pairwise(list(points)))),
                )
                groups.setdefault(signature, set()).update(
                    oracle_classes(length, points)
                )
        for (n, distances_sorted), classes in groups.items():
            sequence, _nodes, total = drain(
                length, list(distances_sorted), n, 1, 3, restart=True
            )
            assert set(sequence) == classes
            assert len(sequence) == len(classes) == total


def test_resumed_state_is_always_consistent() -> None:
    # Suspending at every possible boundary must rebuild cleanly: the strict
    # cross-field validation in from_state exercises every cursor.
    sequence, _nodes, total = drain(
        62, pairwise(list(range(0, 63, 2))), 32, 7, 11, restart=True
    )
    assert total == 1 and len(sequence) == 1
