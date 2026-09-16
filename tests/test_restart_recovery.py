"""Real process-restart recovery tests for serverless cursors.

These tests launch the API as a separate ``uvicorn`` subprocess, mint a
cursor, terminate the server and start a new one:

* with the same secret, the old cursor continues (server restart recovery);
* with a rotated secret, the old cursor is refused.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest


def pairwise(points: list[int]) -> list[int]:
    return [b - a for i, a in enumerate(points) for b in points[i + 1 :]]


HOM_DISTANCES = pairwise([0, 1, 4, 10, 12, 17])


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerProcess:
    def __init__(self, port: int, secret: str) -> None:
        env = dict(os.environ)
        env["TURNPIKE_CURSOR_SECRET"] = secret
        self.proc = subprocess.Popen(
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
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.port = port

    def wait_ready(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/healthz", timeout=1
                ) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        out = self.proc.stdout.read().decode() if self.proc.stdout else ""
        raise AssertionError(f"server never became ready:\n{out}")

    def call(self, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        data = json.dumps(body, separators=(",", ":")).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/turnpike/enumerate",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


@pytest.fixture()
def port() -> int:
    return _free_port()


def test_cursor_survives_server_restart_with_same_secret(port: int) -> None:
    body_page1 = {
        "L": 17,
        "n": 6,
        "distances": HOM_DISTANCES,
        "page_size": 1,
        "node_budget": 1_000_000,
    }
    first = ServerProcess(port, "restart-secret")
    try:
        first.wait_ready()
        status, page = first.call(body_page1)
        assert status == 200 and page["solutions"] == [
            [0, 1, 4, 10, 12, 17]
        ]
        cursor = page["cursor"]
    finally:
        first.stop()

    # Brand-new process, same secret: the serverless cursor still resumes.
    second = ServerProcess(port, "restart-secret")
    try:
        second.wait_ready()
        status, page = second.call({**body_page1, "cursor": cursor})
        assert status == 200, page
        assert page["solutions"] == [[0, 1, 8, 11, 13, 17]]
        assert page["emitted"] == 1
        cursor = page["cursor"]
        status, page = second.call({**body_page1, "cursor": cursor})
        assert status == 200
        assert page["complete"] is True and page["total_classes"] == 2
    finally:
        second.stop()


def test_cursor_refused_after_secret_rotation(port: int) -> None:
    body = {
        "L": 17,
        "n": 6,
        "distances": HOM_DISTANCES,
        "page_size": 1,
        "node_budget": 1_000_000,
    }
    old = ServerProcess(port, "before-rotation")
    try:
        old.wait_ready()
        _status, page = old.call(body)
        cursor = page["cursor"]
    finally:
        old.stop()

    rotated = ServerProcess(port, "after-rotation")
    try:
        rotated.wait_ready()
        status, page = rotated.call({**body, "cursor": cursor})
        assert status == 422
        assert page["error"]["code"] == "cursor_bad_signature"
        assert "before-rotation" not in json.dumps(page)
    finally:
        rotated.stop()
