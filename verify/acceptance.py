#!/usr/bin/env python3
"""One-shot acceptance check against a running turnpike API.

Exits 0 only if every check passes. Exercises:

* ``/healthz``;
* one unique, one ambiguous and one provably impossible reconstruction;
* both structured error shapes (count mismatch, out-of-range first item);
* byte-for-byte identical responses for repeated and reordered requests;
* independent regeneration of every witness distance multiset.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def call(path: str, body: dict[str, object]) -> tuple[int, bytes]:
    data = json.dumps(body, separators=(",", ":")).encode()
    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def pairwise(points: list[int]) -> list[int]:
    return [b - a for i, a in enumerate(points) for b in points[i + 1 :]]


def expect_valid_witness(
    witness: list[int], length: int, distances: list[int]
) -> bool:
    return (
        len(witness) >= 2
        and witness[0] == 0
        and witness[-1] == length
        and witness == sorted(set(witness))
        and Counter(pairwise(witness)) == Counter(distances)
    )


def main() -> int:
    # health
    with urllib.request.urlopen(BASE_URL + "/healthz", timeout=10) as response:
        check("healthz", response.status == 200)

    # unique
    unique_points = [0, 2, 4, 7, 10]
    unique_ds = pairwise(unique_points)
    status, raw = call(
        "/turnpike", {"L": 10, "n": 5, "distances": unique_ds}
    )
    body = json.loads(raw)
    check("unique status 200", status == 200, f"{status} {raw!r}")
    check(
        "unique solution",
        body == {"status": "unique", "solution": unique_points},
        str(body),
    )

    # impossible: no half-work may leak
    status, raw = call("/turnpike", {"L": 5, "n": 3, "distances": [3, 4, 5]})
    body = json.loads(raw)
    check("impossible status 200", status == 200, f"{status}")
    check(
        "impossible body has no partial work",
        body == {"status": "impossible"},
        str(body),
    )

    # ambiguous: two real, canonical, independently verified witnesses
    hom = [0, 1, 4, 10, 12, 17]
    hom_ds = pairwise(hom)
    status, raw = call(
        "/turnpike", {"L": 17, "n": 6, "distances": hom_ds}
    )
    body = json.loads(raw)
    witnesses = body.get("solutions", [])
    ok = (
        status == 200
        and body.get("status") == "ambiguous"
        and len(witnesses) == 2
        and witnesses[0] < witnesses[1]
        and all(expect_valid_witness(w, 17, hom_ds) for w in witnesses)
        and witnesses[0] != witnesses[1]
        and all(w <= [17 - x for x in reversed(w)] for w in witnesses)
    )
    check("ambiguous returns two verified canonical witnesses", ok, str(body))

    # structured error: count mismatch
    status, raw = call(
        "/turnpike", {"L": 10, "n": 4, "distances": [10]}
    )
    body = json.loads(raw)
    error = body.get("error", {})
    check(
        "count mismatch is a structured 422",
        status == 422
        and error.get("code") == "invalid_distance_count"
        and error.get("expected") == 6
        and error.get("actual") == 1
        and error.get("item", {}).get("field") == "distances",
        f"{status} {raw!r}",
    )

    # structured error: first out-of-range item
    status, raw = call(
        "/turnpike", {"L": 5, "n": 3, "distances": [2, 6, 5]}
    )
    body = json.loads(raw)
    item = body.get("error", {}).get("item", {})
    check(
        "out-of-range points at first bad item",
        status == 422
        and body["error"].get("code") == "distance_out_of_range"
        and item.get("index") == 1
        and item.get("value") == 6,
        f"{status} {raw!r}",
    )

    # determinism: identical and reordered-but-equal requests, byte-identical
    payload = {"L": 17, "n": 6, "distances": hom_ds}
    r1 = call("/turnpike", payload)[1]
    r2 = call("/turnpike", payload)[1]
    r3 = call("/turnpike", {"L": 17, "n": 6, "distances": list(reversed(hom_ds))})[1]
    check("repeated request byte identical", r1 == r2, f"{r1!r} != {r2!r}")
    check(
        "reordered multiset byte identical", r1 == r3, f"{r1!r} != {r3!r}"
    )

    # boundary instance
    status, raw = call(
        "/turnpike", {"L": 10**9, "n": 2, "distances": [10**9]}
    )
    body = json.loads(raw)
    check(
        "boundary L=1e9,n=2",
        status == 200
        and body == {"status": "unique", "solution": [0, 10**9]},
        str(body),
    )

    if failures:
        print(f"\nacceptance FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("\nacceptance passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
