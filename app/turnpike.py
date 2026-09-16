"""Turnpike (restriction-map) reconstruction -- shared search kernel.

Given the multiset of all pairwise distances between the cut points of a
linear DNA fragment (including the two end points 0 and L), reconstruct the
strictly increasing cut-point sequence.

The implementation is the classical turnpike backtracking:

* the largest remaining distance ``d`` must be ``y`` (measured from 0) or
  ``L - y`` (measured from L) for the next placed point;
* distance multiset deduction is done with explicit counts, and every branch
  is fully rollbackable;
* at the root the two candidates are mirror images of each other, so only the
  ``L - d`` orientation is explored -- this removes the global reflection
  symmetry; emitted solutions are additionally normalised against their
  mirror image, so every equivalence class is reported at most once;
* no generic constraint solver is used and no coordinate of [0, L] is ever
  enumerated: candidates are derived only from remaining distances.

A completed placement is only accepted after *regenerating* the full distance
multiset from the witness and comparing it, count by count, with the input.

The :class:`TurnpikeEnumerator` runs the same search as :func:`reconstruct` as
an explicit-stack machine whose state (placed points, remaining multiset,
branch frames, counters, seen classes) can be serialised into a serverless
cursor and resumed later.  A *node* is one placement attempt of a candidate
coordinate derived from a remaining distance; the attempt counts even when it
fails immediately because the coordinate is out of bounds, duplicates an
already placed point, or the remaining multiset cannot cover its distances.
Leaf acceptance and branch rollback are not attempts and never cost a node, so
a node budget can never be cut in the middle of an attempt's bookkeeping.
"""

from __future__ import annotations

from collections import Counter
from math import isqrt
from typing import Final, Mapping, Sequence

from .cursor import class_marker

IMPOSSIBLE: Final = "impossible"
UNIQUE: Final = "unique"
AMBIGUOUS: Final = "ambiguous"

# Stop reasons; fixed priority is complete > page_full > budget_exhausted.
COMPLETE: Final = "complete"
PAGE_FULL: Final = "page_full"
BUDGET_EXHAUSTED: Final = "budget_exhausted"

# A practically unlimited single run, used by the original /turnpike path.
_UNLIMITED: Final = 10**18


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


def _branch_candidates(
    length: int, d: int, is_root: bool
) -> tuple[int, ...]:
    """Public, deterministic DFS branch rule for largest remaining distance d.

    * root frame (only 0 and L placed): the single candidate ``L - d``; the
      ``d`` branch is its mirror image and is pruned;
    * deeper frames: ``L - d`` first, then ``d``;
    * a coordinate repeated inside the frame is attempted only once.
    """
    if is_root:
        return (length - d,)
    first = length - d
    second = d
    if second == first:
        return (first,)
    return (first, second)


class TurnpikeEnumerator:
    """Resumable, deterministic turnpike DFS over canonical mirror classes.

    The machine is advanced by :meth:`run`, which explores until a page fills,
    the per-request node budget is spent, or the whole search finishes.
    Canonical witnesses are emitted in DFS discovery order; that order depends
    only on the branch rule above, never on page or budget boundaries.
    """

    def __init__(self, length: int, n: int, original: Counter[int]) -> None:
        self.length = length
        self.n = n
        self.original = original

        self.placed: list[int] = [0, length]
        self.placed_set: set[int] = {0, length}
        # remaining[d] = positive remaining count of distance d.
        self.remaining: Counter[int] = original.copy()
        # Each frame is [d, index]: d == 0 marks a leaf frame, otherwise
        # candidates come from _branch_candidates(length, d, frame is root);
        # index is the number of branches already dispatched.
        self.frames: list[list[int]] = []
        # Canonical classes already discovered, identified by keyed markers
        # (compact so the cursor stays bounded): discovery order + membership.
        self.seen: list[str] = []
        self.seen_set: set[str] = set()
        # Witnesses discovered during this machine's lifetime (not resumed
        # state).  Used only by the single-shot reconstruct() entry point.
        self.found: list[tuple[int, ...]] = []
        self.emitted = 0

        if self.remaining.get(length, 0) >= 1:
            # The pair (0, L) is the only one guaranteed at distance L.
            self.remaining[length] -= 1
            if self.remaining[length] == 0:
                del self.remaining[length]
            self.frames.append(self._make_frame())
        # Missing distance L: no embedding exists; an empty stack means the
        # search is already complete with zero classes.

    # ------------------------------------------------------------------ frames

    def _largest_remaining(self) -> int:
        """Largest distance with a positive remaining count (0 if none)."""
        best = 0
        for value, count in self.remaining.items():
            if count and value > best:
                best = value
        return best

    def _is_root_frame(self, frame_position: int) -> bool:
        """Frame at stack position k was entered with k + 2 points placed."""
        return frame_position == 0

    def _make_frame(self) -> list[int]:
        """Build the decision frame [d, index] for the current multiset."""
        return [self._largest_remaining(), 0]

    def _frame_candidates(self, frame_position: int) -> tuple[int, ...]:
        frame = self.frames[frame_position]
        return _branch_candidates(
            self.length, frame[0], self._is_root_frame(frame_position)
        )

    # ------------------------------------------------------------- multiset IO

    def _deduct(self, candidate: int) -> Counter[int] | None:
        """Check and remove all distances from candidate to placed points.

        Returns the removed multiset (for rollback) or None when any required
        copy is missing. No partial removal survives a failure.
        """
        needed: Counter[int] = Counter()
        for point in self.placed:
            needed[abs(candidate - point)] += 1

        for value, count in needed.items():
            if self.remaining.get(value, 0) < count:
                return None

        for value, count in needed.items():
            self.remaining[value] -= count
            if self.remaining[value] == 0:
                del self.remaining[value]
        return needed

    def _restore(self, candidate: int, needed: Counter[int]) -> None:
        """Undo a _deduct() and remove the candidate point."""
        self.placed_set.remove(candidate)
        self.placed.pop()
        for value, count in needed.items():
            self.remaining[value] = self.remaining.get(value, 0) + count

    def _rollback_parent(self) -> None:
        """Child frame finished: undo the parent frame's current placement.

        The parent's dispatched candidate is ``candidates[index - 1]``; its
        index is left advanced so the parent moves to its next branch.
        """
        position = len(self.frames) - 1
        frame = self.frames[position]
        candidate = self._frame_candidates(position)[frame[1] - 1]
        # All deeper points have already been rolled back, so every remaining
        # placed point is a predecessor of the candidate; reverse the exact
        # deduction that dispatched the branch.
        needed: Counter[int] = Counter()
        for point in self.placed[:-1]:
            needed[abs(candidate - point)] += 1
        self._restore(candidate, needed)

    # ------------------------------------------------------------------- leaf

    def _accept_leaf(self, page: list[list[int]]) -> None:
        """Validate a complete placement and, if new, append it to the page."""
        if len(self.placed) != self.n:
            return
        solution = tuple(sorted(self.placed))
        # Independent re-derivation: the witness must reproduce the exact
        # input distance counts.
        if _regenerate(solution) != self.original:
            return
        canonical = _canonical(solution, self.length)
        marker = class_marker(self.length, canonical)
        if marker in self.seen_set:
            return
        self.seen_set.add(marker)
        self.seen.append(marker)
        self.found.append(canonical)
        self.emitted += 1
        page.append(list(canonical))

    # ------------------------------------------------------------------ run

    def run(
        self, page_size: int, node_budget: int
    ) -> tuple[list[list[int]], int, str]:
        """Advance the DFS.

        Returns ``(page_solutions, nodes_used, stop_reason)`` where
        ``nodes_used`` counts only attempts performed by this call.  The
        budget gate is evaluated only before an attempt, so deductions and
        rollbacks are never left half-applied.
        """
        page: list[list[int]] = []
        nodes_used = 0

        while self.frames:
            position = len(self.frames) - 1
            frame = self.frames[position]
            d, index = frame
            candidates = self._frame_candidates(position)

            if d == 0:
                # Leaf invocation of the recursive search.
                self._accept_leaf(page)
                self.frames.pop()
                if self.frames:
                    self._rollback_parent()
            elif index >= len(candidates):
                # All branches of this invocation explored.
                self.frames.pop()
                if self.frames:
                    self._rollback_parent()
            else:
                # Priority complete > page_full > budget.  The budget is
                # consulted only on an attempt boundary; the gate cannot fire
                # while an attempt's bookkeeping is in flight.
                if nodes_used >= node_budget:
                    return page, nodes_used, BUDGET_EXHAUSTED

                candidate = candidates[index]
                frame[1] = index + 1
                nodes_used += 1  # one placement attempt, whatever its fate

                if (
                    0 < candidate < self.length
                    and candidate not in self.placed_set
                ):
                    needed = self._deduct(candidate)
                else:
                    needed = None  # out of bounds or duplicate point

                if needed is not None:
                    self.placed.append(candidate)
                    self.placed_set.add(candidate)
                    self.frames.append(self._make_frame())

            # Fixed stop priority after every atomic action.
            if not self.frames:
                return page, nodes_used, COMPLETE
            if len(page) >= page_size:
                return page, nodes_used, PAGE_FULL

        return page, nodes_used, COMPLETE

    # ------------------------------------------------------- state serialise

    def to_state(self) -> dict[str, object]:
        """Canonical, JSON-friendly snapshot (input digest is bound by caller)."""
        return {
            "p": list(self.placed),
            "r": sorted((d, c) for d, c in self.remaining.items()),
            "f": [list(frame) for frame in self.frames],
            "e": self.emitted,
            "s": list(self.seen),
        }

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, object],
        length: int,
        n: int,
        original: Counter[int],
    ) -> TurnpikeEnumerator:
        """Rebuild a machine from a cursor snapshot.

        The caller has already authenticated the cursor and matched the input
        digest.  This validates the structural self-consistency of the
        snapshot and raises ``ValueError`` on any malformed state.
        """
        try:
            placed = state["p"]
            remaining_pairs = state["r"]
            frames_raw = state["f"]
            emitted = state["e"]
            seen_raw = state["s"]
            if not (
                isinstance(placed, list)
                and isinstance(remaining_pairs, list)
                and isinstance(frames_raw, list)
                and isinstance(emitted, int)
                and isinstance(seen_raw, list)
            ):
                raise ValueError("bad state field types")
            if not all(isinstance(x, int) for x in placed):
                raise ValueError("bad placed points")
            if placed[:2] != [0, length]:
                raise ValueError("bad placed endpoints")
            if len(placed) != len(set(placed)) or len(placed) > n:
                raise ValueError("placed points not unique")
            if any(not 0 < x < length for x in placed[2:]):
                raise ValueError("placed point out of range")

            remaining: Counter[int] = Counter()
            for pair in remaining_pairs:
                if (
                    not isinstance(pair, (list, tuple))
                    or len(pair) != 2
                    or not all(isinstance(x, int) for x in pair)
                ):
                    raise ValueError("bad remaining pair")
                distance, count = pair
                if distance < 1 or distance > length or count < 1:
                    raise ValueError("remaining pair out of range")
                remaining[distance] = count

            frames: list[list[int]] = []
            for frame in frames_raw:
                if (
                    not isinstance(frame, (list, tuple))
                    or len(frame) != 2
                    or not all(isinstance(x, int) for x in frame)
                ):
                    raise ValueError("bad frame shape")
                d, index = frame
                if d < 0 or index < 0:
                    raise ValueError("bad frame cursor")
                if d == 0:
                    if index != 0:
                        raise ValueError("leaf frame with branch index")
                elif not 1 <= d <= length:
                    raise ValueError("frame distance out of range")
                else:
                    candidates = _branch_candidates(
                        length, d, len(frames) == 0
                    )
                    if index > len(candidates):
                        raise ValueError("frame index past branches")
                frames.append([d, index])

            seen: list[str] = []
            seen_set: set[str] = set()
            for marker in seen_raw:
                if not isinstance(marker, str):
                    raise ValueError("bad seen marker")
                if not (1 <= len(marker) <= 64) or any(
                    char not in "0123456789abcdef" for char in marker
                ):
                    raise ValueError("bad seen marker encoding")
                if marker in seen_set:
                    raise ValueError("duplicate seen marker")
                seen_set.add(marker)
                seen.append(marker)
            if emitted != len(seen):
                raise ValueError("bad emitted count")

            # A suspended search always has at least the root frame.
            if not frames:
                raise ValueError("suspended state without frames")
            # Stack depth tracks placements: the root frame exists with
            # {0, L}; every pushed frame accompanies exactly one append.
            if len(placed) != len(frames) + 1:
                raise ValueError("frames do not match placed depth")
            # Cross-check the multiset bookkeeping: remaining must be exactly
            # the input multiset minus every distance among placed points.
            used_distances: Counter[int] = Counter()
            for i, left in enumerate(placed):
                for right in placed[i + 1 :]:
                    used_distances[abs(right - left)] += 1
            for distance, count in used_distances.items():
                if original.get(distance, 0) < count:
                    raise ValueError("placed points consume absent distance")
            rebuilt_remaining = original - used_distances
            if dict(rebuilt_remaining) != dict(remaining):
                raise ValueError("remaining multiset is inconsistent")
            # The top frame decides on the current multiset, so its distance
            # must be the largest remaining one (0 exactly when none remain).
            top_d = frames[-1][0]
            if top_d != (max(remaining) if remaining else 0):
                raise ValueError("top frame does not match remaining")
        except (KeyError, TypeError) as exc:
            raise ValueError("malformed state") from exc

        self = cls.__new__(cls)
        self.length = length
        self.n = n
        self.original = original
        self.placed = placed
        self.placed_set = set(placed)
        self.remaining = remaining
        self.frames = frames
        self.seen = seen
        self.seen_set = seen_set
        self.found = []
        self.emitted = emitted
        return self


def reconstruct(
    length: int, distances: Sequence[int]
) -> Mapping[str, object]:
    """Reconstruct cut points from the pairwise-distance multiset.

    Returns one of::

        {"status": "impossible"}
        {"status": "unique", "solution": [0, ..., L]}
        {"status": "ambiguous", "solutions": [[...], [...]]}

    where the two ambiguous witnesses are the lexicographically smallest two
    *distinct canonical* solutions (mirror classes collapsed).  The search
    drives the same resumable kernel used by ``POST /turnpike/enumerate``;
    there is no HTTP pagination involved.
    """
    original = Counter(distances)
    total = len(distances)
    n = _triangular_n(total)
    if n * (n - 1) // 2 != total:
        return {"status": IMPOSSIBLE}

    search = TurnpikeEnumerator(length, n, original)
    search.run(_UNLIMITED, _UNLIMITED)

    witnesses = sorted(search.found)
    if not witnesses:
        return {"status": IMPOSSIBLE}
    if len(witnesses) == 1:
        return {"status": UNIQUE, "solution": list(witnesses[0])}
    return {
        "status": AMBIGUOUS,
        "solutions": [list(witnesses[0]), list(witnesses[1])],
    }
