"""FastAPI application: turnpike (restriction-map) reconstruction."""

from __future__ import annotations

from collections import Counter

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .cursor import CursorError, decode_cursor, encode_cursor, input_digest
from .schemas import (
    AmbiguousResponse,
    EnumerateRequest,
    EnumerateResponse,
    ImpossibleResponse,
    TurnpikeRequest,
    UniqueResponse,
)
from .turnpike import (
    AMBIGUOUS,
    IMPOSSIBLE,
    UNIQUE,
    TurnpikeEnumerator,
    reconstruct,
)

app = FastAPI(
    title="Turnpike Reconstruction Service",
    version="1.1.0",
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


def _validate_distances(length: int, n: int, distances: list[int]) -> None:
    """Shared semantic validation for both endpoints.

    Enforces the exact distance count ``n(n-1)/2`` and the per-item range
    ``1 <= d <= L``, pointing the structured error at the first bad item.
    """
    expected = n * (n - 1) // 2
    actual = len(distances)
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
    for index, value in enumerate(distances):
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
    _validate_distances(request.L, request.n, request.distances)

    result = reconstruct(request.L, request.distances)
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


@app.post(
    "/turnpike/enumerate",
    response_model=EnumerateResponse,
    responses={422: {"description": "Structured validation/cursor error"}},
)
def enumerate_solutions(request: EnumerateRequest) -> EnumerateResponse:
    """Pausable, resumable enumeration of *all* canonical mirror classes.

    Each request advances the shared DFS kernel by at most ``node_budget``
    placement attempts and returns at most ``page_size`` newly discovered
    solutions.  All continuation state lives in the authenticated cursor;
    the server stores nothing between requests.
    """
    _validate_distances(request.L, request.n, request.distances)

    original = Counter(request.distances)
    digest = input_digest(request.L, request.n, original)

    if request.cursor is None:
        search = TurnpikeEnumerator(request.L, request.n, original)
    else:
        try:
            state = decode_cursor(
                request.cursor, request.L, request.n, digest
            )
            search = TurnpikeEnumerator.from_state(
                state, request.L, request.n, original
            )
        except CursorError as exc:
            raise StructuredError(
                exc.code,
                exc.message,
                item={"field": "cursor"},
            ) from exc
        except ValueError as exc:
            # Authenticated envelope but an internally inconsistent snapshot:
            # refuse without revealing any state.
            raise StructuredError(
                "cursor_malformed",
                "cursor payload is corrupt",
                item={"field": "cursor"},
            ) from exc

    previously_emitted = search.emitted
    page, nodes_used, stop_reason = search.run(
        request.page_size, request.node_budget
    )

    if stop_reason == "complete":
        return EnumerateResponse(
            solutions=page,
            emitted=previously_emitted,
            nodes_used=nodes_used,
            complete=True,
            stop_reason="complete",
            total_classes=search.emitted,
            cursor=None,
        )

    try:
        cursor = encode_cursor(
            search.to_state(), request.L, request.n, digest
        )
    except CursorError as exc:
        raise StructuredError(
            exc.code, exc.message, item={"field": "cursor"}
        ) from exc
    return EnumerateResponse(
        solutions=page,
        emitted=previously_emitted,
        nodes_used=nodes_used,
        complete=False,
        stop_reason=stop_reason,
        total_classes=None,
        cursor=cursor,
    )
