#!/usr/bin/env python3
"""One-shot acceptance check against a running turnpike API.

Exits 0 only if every check passes. Exercises:

* ``/healthz``;
* one unique, one ambiguous and one provably impossible reconstruction;
* both structured error shapes (count mismatch, out-of-range first item);
* byte-for-byte identical responses for repeated and reordered requests;
* independent regeneration of every witness distance multiset;
* the resumable ``POST /turnpike/enumerate`` kernel: multi-page ambiguity,
  page-size/budget changes, exact budget exhaustion, mirror collapse, no
  solution, byte-identical replays, restart recovery (when the API keeps the
  same secret), cursor attacks and secret rotation rejection.
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

    enumerate_checks()

    if failures:
        print(f"\nacceptance FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("\nacceptance passed")
    return 0


def enumerate_call(
    base_url: str, body: dict[str, object]
) -> tuple[int, dict[str, object]]:
    data = json.dumps(body, separators=(",", ":")).encode()
    request = urllib.request.Request(
        base_url + "/turnpike/enumerate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def drain_enumeration(
    base_url: str,
    length: int,
    n: int,
    distances: list[int],
    page_size: int,
    node_budget: int,
) -> tuple[list[list[int]], int, list[dict[str, object]], bytes]:
    """Paginate to completion; returns classes, node total, pages, first bytes."""
    cursor: str | None = None
    classes: list[list[int]] = []
    node_total = 0
    pages: list[dict[str, object]] = []
    first_bytes = b""
    first_body: dict[str, object] | None = None
    while True:
        body: dict[str, object] = {
            "L": length,
            "n": n,
            "distances": distances,
            "page_size": page_size,
            "node_budget": node_budget,
        }
        if cursor is not None:
            body["cursor"] = cursor
        raw = json.dumps(body, separators=(",", ":")).encode()
        request = urllib.request.Request(
            base_url + "/turnpike/enumerate",
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            failures.append("enumeration page returned an error")
            print(f"  unexpected {exc.code}: {exc.read()!r}")
            return classes, node_total, pages, first_bytes
        page = json.loads(payload)
        if first_body is None:
            first_body = body
            first_bytes = payload
        pages.append(page)
        classes.extend(page["solutions"])
        node_total += page["nodes_used"]
        if page["complete"]:
            assert page["cursor"] is None
            assert page["total_classes"] == len(classes)
            break
        assert page["total_classes"] is None and page["cursor"]
        cursor = page["cursor"]
    # Replay the first request: bytes must be identical.
    assert first_body is not None
    replay_request = urllib.request.Request(
        base_url + "/turnpike/enumerate",
        data=json.dumps(first_body, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(replay_request, timeout=30) as response:
        if response.read() != first_bytes:
            failures.append("enumeration replay not byte identical")
    return classes, node_total, pages, first_bytes


def enumerate_checks() -> None:
    hom = [0, 1, 4, 10, 12, 17]
    hom_ds = pairwise(hom)
    expected = [
        [0, 1, 4, 10, 12, 17],
        [0, 1, 8, 11, 13, 17],
    ]

    # multi-page ambiguity with page_size 1: two page_full pages and one
    # empty closing page, emitted counts 0,1,2.
    classes, node_total, pages, _ = drain_enumeration(
        BASE_URL, 17, 6, hom_ds, 1, 1_000_000
    )
    ok = (
        classes == expected
        and [p["solutions"] for p in pages] == [expected[:1], expected[1:], []]
        and [p["emitted"] for p in pages] == [0, 1, 2]
        and all(p["stop_reason"] in ("page_full", "complete") for p in pages)
        and pages[-1]["stop_reason"] == "complete"
        and all(expect_valid_witness(w, 17, hom_ds) for w in classes)
    )
    check("enumerate multi-page ambiguity", ok, str(pages))

    # changing page_size/node_budget only moves page boundaries: same sequence,
    # same cumulative node consumption.
    variants = [(100, 1_000_000), (1, 1), (2, 3), (1, 7), (50, 1)]
    variant_results = [
        drain_enumeration(BASE_URL, 17, 6, hom_ds, ps, nb)
        for ps, nb in variants
    ]
    ok = all(v[0] == expected for v in variant_results) and len(
        {v[1] for v in variant_results}
    ) == 1 and {v[1] for v in variant_results} == {node_total}
    check("enumerate paging/budget invariance", ok)

    # reordered distance list is the same request and starts the same way
    status, body = enumerate_call(
        BASE_URL,
        {
            "L": 17,
            "n": 6,
            "distances": list(reversed(hom_ds)),
            "page_size": 1,
            "node_budget": 1_000_000,
        },
    )
    check(
        "enumerate reorder is same request",
        status == 200 and body["solutions"] == expected[:1],
        str(body),
    )

    # budget exactly exhausted: page_size large, node_budget 1 suspends after
    # one attempt with no page content; finishing yields both classes.
    status, body = enumerate_call(
        BASE_URL,
        {
            "L": 17,
            "n": 6,
            "distances": hom_ds,
            "page_size": 100,
            "node_budget": 1,
        },
    )
    ok = (
        status == 200
        and body["complete"] is False
        and body["stop_reason"] == "budget_exhausted"
        and body["nodes_used"] == 1
        and body["solutions"] == []
        and bool(body["cursor"])
    )
    check("enumerate budget exhaustion suspends", ok, str(body))

    # impossible legal input finishes complete with total 0.
    status, body = enumerate_call(
        BASE_URL,
        {
            "L": 5,
            "n": 3,
            "distances": [3, 4, 5],
            "page_size": 10,
            "node_budget": 10,
        },
    )
    check(
        "enumerate impossible completes with zero",
        status == 200
        and body["complete"] is True
        and body["total_classes"] == 0
        and body["solutions"] == []
        and body["cursor"] is None,
        str(body),
    )

    # mirror self-symmetric solution is a single class
    sym = [0, 1, 3, 4]
    classes, _n, _p, _b = drain_enumeration(
        BASE_URL, 4, 4, pairwise(sym), 1, 1
    )
    check("enumerate mirror collapse", classes == [sym], str(classes))

    # cursor attacks: every variant is a structured 422 that leaks no internals
    _s, first = enumerate_call(
        BASE_URL,
        {
            "L": 17,
            "n": 6,
            "distances": hom_ds,
            "page_size": 1,
            "node_budget": 1_000_000,
        },
    )
    good_cursor = first["cursor"]
    assert isinstance(good_cursor, str)
    version, payload, signature = good_cursor.split(".")
    attacks = {
        "truncated": good_cursor[:-6],
        "flipped": f"{version}.{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}",
        "shape": "tp1.abc",
        "unknown_version": f"tp9.{payload}.{signature}",
        "empty": "",
        "oversized": "tp1." + "A" * 300_000,
    }
    attack_codes = set()
    leaked = False
    for token in attacks.values():
        status, body = enumerate_call(
            BASE_URL,
            {
                "L": 17,
                "n": 6,
                "distances": hom_ds,
                "page_size": 1,
                "node_budget": 1_000_000,
                "cursor": token,
            },
        )
        if status != 422:
            leaked = True
            continue
        error = body.get("error", {})
        attack_codes.add(error.get("code"))
        serialized = json.dumps(error)
        if "Traceback" in serialized or "secret" in serialized.lower():
            leaked = True
    check(
        "enumerate cursor attacks rejected without leakage",
        not leaked
        and attack_codes
        <= {"cursor_malformed", "cursor_bad_signature",
            "cursor_unknown_version", "cursor_too_large"},
        str(attack_codes),
    )

    # cursor bound to another input
    status, body = enumerate_call(
        BASE_URL,
        {
            "L": 18,
            "n": 6,
            "distances": [18] + hom_ds[1:],
            "page_size": 1,
            "node_budget": 1_000_000,
            "cursor": good_cursor,
        },
    )
    check(
        "enumerate cursor input mismatch",
        status == 422
        and body["error"]["code"] == "cursor_input_mismatch",
        str(body),
    )

    # restart recovery and key rotation, against throwaway local processes
    restart_and_rotation_checks(good_cursor, hom_ds)


def restart_and_rotation_checks(good_cursor: str, hom_ds: list[int]) -> None:
    """Spawn local servers to prove serverless restart/rotation semantics."""
    import socket
    import subprocess
    import time

    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start(port: int, secret: str) -> subprocess.Popen[bytes]:
        env = dict(os.environ)
        env["TURNPIKE_CURSOR_SECRET"] = secret
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def wait_ready(base_url: str) -> bool:
        for _ in range(100):
            try:
                with urllib.request.urlopen(base_url + "/healthz", timeout=1) as r:
                    if r.status == 200:
                        return True
            except OSError:
                time.sleep(0.1)
        return False

    def stop(proc: subprocess.Popen[bytes]) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    body = {
        "L": 17,
        "n": 6,
        "distances": hom_ds,
        "page_size": 1,
        "node_budget": 1_000_000,
    }

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = start(port, "acceptance-restart-key")
    try:
        if not wait_ready(base):
            check("enumerate restart recovery", False, "server did not start")
            return
        # Mint a cursor on the fresh server.
        status, page = enumerate_call(base, body)
        cursor = page["cursor"]
        ok = status == 200 and bool(cursor)
    finally:
        stop(proc)

    # Restart with the same secret: the old cursor continues.
    proc = start(port, "acceptance-restart-key")
    try:
        if wait_ready(base):
            status, page = enumerate_call(base, {**body, "cursor": cursor})
            ok = ok and status == 200 and page["emitted"] == 1
            check("enumerate restart keeps cursor", ok, str(page))
        else:
            check("enumerate restart keeps cursor", False, "no restart")
    finally:
        stop(proc)

    # Rotate the secret: every old cursor must be refused.
    proc = start(port, "acceptance-rotated-key")
    try:
        if wait_ready(base):
            status, page = enumerate_call(base, {**body, "cursor": cursor})
            check(
                "enumerate secret rotation rejects cursor",
                status == 422
                and page["error"]["code"] == "cursor_bad_signature",
                str(page),
            )
        else:
            check("enumerate secret rotation rejects cursor", False, "no start")
    finally:
        stop(proc)


if __name__ == "__main__":
    sys.exit(main())
