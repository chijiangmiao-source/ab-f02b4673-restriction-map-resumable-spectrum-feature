"""Turnpike (restriction-map) reconstruction.

Given the multiset of all pairwise distances between the cut points of a
linear DNA fragment (including the two end points 0 and L), reconstruct the
strictly increasing cut-point sequence.

The implementation is the classical turnpike backtracking:

* the largest remaining distance ``d`` must be ``y`` (measured from 0) or
  ``L - y`` (measured from L) for the next placed point;
* distance multiset deduction is done with explicit counts, and every branch
  is fully rollbackable;
* at the root the two candidates are mirror images of each other, so only the
  orientation that places the smallest interior coordinate is explored -- this
  removes the global reflection symmetry; emitted solutions are additionally
  normalised against their mirror image;
* no generic constraint solver is used and no coordinate of [0, L] is ever
  enumerated: candidates are derived only from remaining distances.

A completed placement is only accepted after *regenerating* the full distance
multiset from the witness and comparing it, count by count, with the input.
"""

from __future__ import annotations

from collections import Counter
from math import isqrt
from typing import Final, Mapping, Sequence

IMPOSSIBLE: Final = "impossible"
UNIQUE: Final = "unique"
AMBIGUOUS: Final = "ambiguous"


def _triangular_n(count: int) -> int:
    """Largest n with n(n-1)/2 <= count (exactness checked by the caller)."""
    disc = 1 + 8 * count
    root = isqrt(disc)
    return (1 + root) // 2


def _mirror(solution: tuple[int, ...], length: int) -> tuple[int, ...]:
    """Reflect a cut-point sequence about L/2."""
    interior = reversed(solution[1:-1])
    return (0, *(length - x for x in interior), length)


def _canonical(solution: tuple[int, ...], length: int) -> tuple[int, ...]:
    """Choose the lexicographically smaller member of the mirror class."""
    return min(solution, _mirror(solution, length))


def _regenerate(solution: Sequence[int]) -> Counter[int]:
    """Multiset of all pairwise distances of a candidate witness."""
    distances: Counter[int] = Counter()
    for i, left in enumerate(solution):
        for right in solution[i + 1 :]:
            distances[right - left] += 1
    return distances


def reconstruct(
    length: int, distances: Sequence[int]
) -> Mapping[str, object]:
    """Reconstruct cut points from the pairwise-distance multiset.

    Returns one of::

        {"status": "impossible"}
        {"status": "unique", "solution": [0, ..., L]}
        {"status": "ambiguous", "solutions": [[...], [...]]}

    where the two ambiguous witnesses are the lexicographically smallest two
    *distinct canonical* solutions (mirror classes collapsed).
    """
    original = Counter(distances)
    total = len(distances)
    n = _triangular_n(total)
    if n * (n - 1) // 2 != total:
        return {"status": IMPOSSIBLE}

    # The pair (0, L) is the only one at distance L.
    if original.get(length, 0) < 1:
        return {"status": IMPOSSIBLE}

    remaining = original.copy()
    remaining[length] -= 1
    if remaining[length] == 0:
        del remaining[length]

    placed: list[int] = [0, length]
    placed_set: set[int] = {0, length}
    canonical_solutions: set[tuple[int, ...]] = set()

    def largest_remaining() -> int:
        """Largest distance with a positive remaining count (0 if none)."""
        best = 0
        for value, count in remaining.items():
            if count and value > best:
                best = value
        return best

    def deduct(candidate: int) -> Counter[int] | None:
        """Check and remove all distances from candidate to placed points.

        Returns the removed multiset (for rollback) or None when any required
        copy is missing. No partial removal survives a failure.
        """
        needed: Counter[int] = Counter()
        for point in placed:
            needed[abs(candidate - point)] += 1

        for value, count in needed.items():
            if remaining.get(value, 0) < count:
                return None

        for value, count in needed.items():
            remaining[value] -= count
            if remaining[value] == 0:
                del remaining[value]
        return needed

    def restore(candidate: int, needed: Counter[int]) -> None:
        """Undo a deduct() and remove the candidate point."""
        placed_set.remove(candidate)
        placed.pop()
        for value, count in needed.items():
            remaining[value] = remaining.get(value, 0) + count

    def search() -> None:
        d = largest_remaining()

        if d == 0:
            if len(placed) == n:
                solution = tuple(sorted(placed))
                # Independent re-derivation: the witness must reproduce the
                # exact input distance counts.
                if _regenerate(solution) == original:
                    canonical_solutions.add(_canonical(solution, length))
            return

        if len(placed) == 2:
            # Root: candidates d and L-d are mirror images. Exploring the one
            # with the smaller coordinate fixes the canonical orientation, so
            # the reflected search is pruned entirely.
            candidates = (length - d,)
        else:
            # Interior-side placement first: it tends to produce the
            # lexicographically smaller witness earlier, but does not affect
            # completeness; both branches are explored.
            candidates = (length - d, d)

        seen: set[int] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate <= 0 or candidate >= length:
                continue
            if candidate in placed_set:
                continue

            needed = deduct(candidate)
            if needed is None:
                continue
            placed.append(candidate)
            placed_set.add(candidate)
            search()
            restore(candidate, needed)

    search()

    witnesses = sorted(canonical_solutions)
    if not witnesses:
        return {"status": IMPOSSIBLE}
    if len(witnesses) == 1:
        return {"status": UNIQUE, "solution": list(witnesses[0])}
    return {
        "status": AMBIGUOUS,
        "solutions": [list(witnesses[0]), list(witnesses[1])],
    }
