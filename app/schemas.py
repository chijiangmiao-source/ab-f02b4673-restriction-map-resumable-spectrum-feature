"""Request/response schemas for the turnpike endpoints.

Type-level constraints (bounds on ``L`` and ``n``, strict integers, no extra
fields) are enforced by Pydantic.  Cross-field constraints -- the exact
distance count ``n(n-1)/2`` and the per-item range ``1 <= d <= L`` -- are
checked in the endpoint so that the structured 422 error can point at the
*first* offending distance item.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_LENGTH: int = 10**9
MAX_CUTS: int = 32
MAX_PAGE_SIZE: int = 100
MAX_NODE_BUDGET: int = 1_000_000

Length = Annotated[int, Field(strict=True, ge=2, le=MAX_LENGTH)]
CutCount = Annotated[int, Field(strict=True, ge=2, le=MAX_CUTS)]
Distance = Annotated[int, Field(strict=True)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=MAX_PAGE_SIZE)]
NodeBudget = Annotated[int, Field(strict=True, ge=1, le=MAX_NODE_BUDGET)]


class TurnpikeRequest(BaseModel):
    """``POST /turnpike`` request body."""

    model_config = ConfigDict(extra="forbid")

    L: Length
    n: CutCount
    distances: list[Distance]


class ImpossibleResponse(BaseModel):
    status: Literal["impossible"]


class UniqueResponse(BaseModel):
    status: Literal["unique"]
    solution: list[int]


class AmbiguousResponse(BaseModel):
    status: Literal["ambiguous"]
    solutions: Annotated[list[list[int]], Field(min_length=2, max_length=2)]


class EnumerateRequest(BaseModel):
    """``POST /turnpike/enumerate`` request body.

    A first request omits ``cursor`` and starts the search from the root; a
    continuation request must repeat the same normalised ``L``, ``n`` and
    ``distances`` multiset (any permutation is the same request) and present
    the cursor returned by the previous page.  ``page_size`` and
    ``node_budget`` may change between requests.
    """

    model_config = ConfigDict(extra="forbid")

    L: Length
    n: CutCount
    distances: list[Distance]
    page_size: PageSize
    node_budget: NodeBudget
    cursor: str | None = None


class EnumerateResponse(BaseModel):
    """One page of the resumable enumeration.

    * ``solutions`` -- canonical classes discovered on *this* page, in the
      fixed DFS discovery order;
    * ``emitted`` -- number of classes already emitted on earlier pages;
    * ``nodes_used`` -- placement attempts consumed by this request;
    * ``complete`` / ``stop_reason`` -- search status;
    * ``total_classes`` -- present only once the search is complete;
    * ``cursor`` -- present only while the search is incomplete.
    """

    solutions: list[list[int]]
    emitted: Annotated[int, Field(ge=0)]
    nodes_used: Annotated[int, Field(ge=0)]
    complete: bool
    stop_reason: Literal["complete", "page_full", "budget_exhausted"]
    total_classes: Annotated[int | None, Field(ge=0)] = None
    cursor: str | None = None
