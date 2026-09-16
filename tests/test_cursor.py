"""Unit tests for cursor encoding, authentication and state validation."""

from __future__ import annotations

import base64
import json
import zlib

import pytest

from app.cursor import (
    MAX_PAYLOAD_BYTES,
    MAX_TOKEN_BYTES,
    CursorError,
    class_marker,
    decode_cursor,
    encode_cursor,
    input_digest,
)
from app.turnpike import TurnpikeEnumerator


def pairwise(points: list[int]) -> list[int]:
    return [b - a for i, a in enumerate(points) for b in points[i + 1 :]]


HOM = [0, 1, 4, 10, 12, 17]


@pytest.fixture()
def secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("TURNPIKE_CURSOR_SECRET", "cursor-unit-secret")
    return "cursor-unit-secret"


def _snapshot() -> tuple[dict[str, object], int, int, str]:
    from collections import Counter

    distances = pairwise(HOM)
    original = Counter(distances)
    digest = input_digest(17, 6, original)
    search = TurnpikeEnumerator(17, 6, original)
    search.run(1, 1_000_000)  # suspend after the first page
    return search.to_state(), 17, 6, digest


def test_deterministic_encoding(secret: str) -> None:
    state, length, n, digest = _snapshot()
    assert encode_cursor(state, length, n, digest) == encode_cursor(
        state, length, n, digest
    )


def test_round_trip(secret: str) -> None:
    state, length, n, digest = _snapshot()
    token = encode_cursor(state, length, n, digest)
    # JSON converts tuples to lists; compare through a JSON normal form.
    decoded = decode_cursor(token, length, n, digest)
    assert json.loads(json.dumps(decoded)) == json.loads(json.dumps(state))


def test_input_reordering_same_digest(secret: str) -> None:
    from collections import Counter

    distances = pairwise(HOM)
    assert input_digest(17, 6, Counter(distances)) == input_digest(
        17, 6, Counter(reversed(distances))
    )


def test_replay_under_foreign_key_fails(
    secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, length, n, digest = _snapshot()
    token = encode_cursor(state, length, n, digest)
    monkeypatch.setenv("TURNPIKE_CURSOR_SECRET", "a-totally-different-key")
    with pytest.raises(CursorError) as exc:
        decode_cursor(token, length, n, digest)
    assert exc.value.code == "cursor_bad_signature"


def test_truncation_and_garbage(secret: str) -> None:
    state, length, n, digest = _snapshot()
    token = encode_cursor(state, length, n, digest)
    for bad in (token[:-8], token[5:], "", "abc", "tp1", "tp1.x", 42, None):
        with pytest.raises(CursorError):
            decode_cursor(bad, length, n, digest)


def test_unknown_version(secret: str) -> None:
    state, length, n, digest = _snapshot()
    token = encode_cursor(state, length, n, digest)
    rest = token.split(".", 1)[1]
    with pytest.raises(CursorError) as exc:
        decode_cursor(f"tp42.{rest}", length, n, digest)
    assert exc.value.code == "cursor_unknown_version"


def test_input_binding_mismatch(secret: str) -> None:
    state, length, n, digest = _snapshot()
    token = encode_cursor(state, length, n, digest)
    with pytest.raises(CursorError) as exc:
        decode_cursor(token, length + 1, n, digest)
    assert exc.value.code == "cursor_input_mismatch"
    with pytest.raises(CursorError) as exc:
        decode_cursor(token, length, n + 1, digest)
    assert exc.value.code == "cursor_input_mismatch"
    with pytest.raises(CursorError) as exc:
        decode_cursor(token, length, n, "0" * 64)
    assert exc.value.code == "cursor_input_mismatch"


def test_oversized_token(secret: str) -> None:
    with pytest.raises(CursorError) as exc:
        decode_cursor("tp1." + "A" * (MAX_TOKEN_BYTES + 1), 17, 6, "0" * 64)
    assert exc.value.code == "cursor_too_large"


def test_decompression_bomb_rejected(
    secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hmac
    import hashlib
    from app import cursor as cursor_module

    # Valid signature over a payload that decompresses beyond the cap.
    bomb = b'{"x":"' + b"0" * (MAX_PAYLOAD_BYTES + 1024) + b'"}'
    compressed = zlib.compress(bomb, 9)
    key = hashlib.sha256(b"cursor-unit-secret").digest()
    mac = hmac.new(
        key, cursor_module._MAC_LABEL + compressed, hashlib.sha256
    ).digest()

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    token = f"tp1.{b64(compressed)}.{b64(mac)}"
    with pytest.raises(CursorError) as exc:
        decode_cursor(token, 17, 6, "0" * 64)
    assert exc.value.code == "cursor_too_large"


def _resigned(envelope: dict[str, object], secret_value: str) -> str:
    import hashlib
    import hmac
    from app import cursor as cursor_module

    raw = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    compressed = zlib.compress(raw, 9)
    key = hashlib.sha256(secret_value.encode()).digest()
    mac = hmac.new(
        key, cursor_module._MAC_LABEL + compressed, hashlib.sha256
    ).digest()

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    return f"tp1.{b64(compressed)}.{b64(mac)}"


def test_authenticated_but_internally_corrupt_state(secret: str) -> None:
    from collections import Counter

    state, length, n, digest = _snapshot()

    corrupt_variants: list[dict[str, object]] = [
        {**state, "p": [0, 17, 17]},  # duplicate point
        {**state, "p": [0, 99]},  # endpoint not L
        {**state, "f": []},  # suspended search without frames
        {**state, "e": 999},  # emitted disagrees with seen count
        {**state, "r": [["not-an-int", 1]]},
        {**state, "s": ["zzzz"]},  # non-hex marker
    ]
    for variant in corrupt_variants:
        envelope = {"i": [length, n, digest], "s": variant}
        token = _resigned(envelope, "cursor-unit-secret")
        decoded = decode_cursor(token, length, n, digest)
        with pytest.raises(ValueError):
            TurnpikeEnumerator.from_state(
                decoded, length, n, Counter(pairwise(HOM))
            )


def test_class_marker_stability() -> None:
    canonical = (0, 1, 4, 10, 12, 17)
    assert class_marker(17, canonical) == class_marker(17, canonical)
    assert len(class_marker(17, canonical)) == 32
    assert class_marker(17, canonical) != class_marker(
        17, (0, 1, 8, 11, 13, 17)
    )
