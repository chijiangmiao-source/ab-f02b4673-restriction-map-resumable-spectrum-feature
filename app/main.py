"""FastAPI application: turnpike (restriction-map) reconstruction."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .schemas import (
    AmbiguousResponse,
    ImpossibleResponse,
    TurnpikeRequest,
    UniqueResponse,
)
from .turnpike import AMBIGUOUS, IMPOSSIBLE, UNIQUE, reconstruct

app = FastAPI(
    title="Turnpike Reconstruction Service",
    version="1.0.0",
    description=(
        "Reconstruct a strictly increasing restriction-cut sequence "
        "(including 0 and L) from the multiset of all pairwise distances."
    ),
)


class StructuredError(Exception):
    """A 422 whose body is a stable, machine-readable error object."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@app.exception_handler(StructuredError)
async def structured_error_handler(
    _request: Request, exc: StructuredError
) -> JSONResponse:
    error: dict[str, object] = {"code": exc.code, "message": exc.message}
    error.update(exc.details)
    return JSONResponse(status_code=422, content={"error": error})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Normalise schema-level failures (bad types, bounds, unknown fields,
    # malformed JSON) into the same envelope as semantic errors.
    problems = []
    for err in exc.errors():  # type: ignore[attr-defined]
        problems.append(
            {
                "location": [str(part) for part in err.get("loc", ())],
                "code": err.get("type", "value_error"),
                "message": err.get("msg", ""),
            }
        )
    first = problems[0] if problems else {
        "location": [],
        "code": "invalid_request",
        "message": "request could not be validated",
    }
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "invalid_request",
            "message": first["message"],
            "item": {
                "field": ".".join(first["location"]) or None,
            },
            "problems": problems,
        }},
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/turnpike",
    response_model=ImpossibleResponse | UniqueResponse | AmbiguousResponse,
    responses={422: {"description": "Structured validation/semantic error"}},
)
def solve(
    request: TurnpikeRequest,
) -> ImpossibleResponse | UniqueResponse | AmbiguousResponse:
    length = request.L
    expected = request.n * (request.n - 1) // 2
    actual = len(request.distances)
    if actual != expected:
        raise StructuredError(
            "invalid_distance_count",
            f"expected exactly n(n-1)/2 = {expected} distances, got {actual}",
            expected=expected,
            actual=actual,
            item={"field": "distances", "expected_size": expected},
        )

    # Range check is performed by the server (not the type system) so that the
    # first offending entry can be identified by its index.
    for index, value in enumerate(request.distances):
        if value < 1 or value > length:
            raise StructuredError(
                "distance_out_of_range",
                f"distance at index {index} is {value}, "
                f"must satisfy 1 <= distance <= L ({length})",
                item={
                    "field": "distances",
                    "index": index,
                    "value": value,
                    "lower": 1,
                    "upper": length,
                },
            )

    result = reconstruct(length, request.distances)
    status = result["status"]
    if status == IMPOSSIBLE:
        return ImpossibleResponse(status=IMPOSSIBLE)
    if status == UNIQUE:
        return UniqueResponse(
            status=UNIQUE, solution=list(result["solution"])  # type: ignore[arg-type]
        )
    return AmbiguousResponse(
        status=AMBIGUOUS,
        solutions=[list(s) for s in result["solutions"]],  # type: ignore[arg-type]
    )
