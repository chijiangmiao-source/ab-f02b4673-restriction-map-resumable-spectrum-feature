"""Request/response schemas for the turnpike endpoint.

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

Length = Annotated[int, Field(strict=True, ge=2, le=MAX_LENGTH)]
CutCount = Annotated[int, Field(strict=True, ge=2, le=MAX_CUTS)]
Distance = Annotated[int, Field(strict=True)]


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
