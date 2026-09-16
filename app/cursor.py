"""Versioned, authenticated, serverless resumption cursors.

A cursor carries the full serialised state of a suspended
:class:`app.turnpike.TurnpikeEnumerator`; the server keeps no session state.
The wire format is versioned, deterministic and authenticated:

``tp1.<zlib(json(state)) b64url>.<HMAC-SHA256 b64url>``

Properties:

* **versioned** -- unknown version prefixes are rejected before any parsing;
* **deterministic** -- canonical JSON (sorted keys, compact separators),
  fixed compression, no nonce or timestamp: the same state with the same key
  always encodes to the exact same token, so replays are byte-identical;
* **authenticated** -- HMAC-SHA256 over the compressed payload with the
  ``TURNPIKE_CURSOR_SECRET`` key (constant-time comparison); a different key,
  a truncated token or a flipped byte all fail verification;
* **input-bound** -- the payload embeds a digest of the normalised request
  (``L``, ``n`` and the distance *multiset*, so input reordering is the same
  request); a cursor replayed against any other input is rejected;
* **bounded** -- both the inbound token and the decompressed payload have
  explicit size caps; decompression is guarded against bombs.

Failure modes are reported as distinct :class:`CursorError` codes and never
include stack traces, payload contents or key material:

``cursor_malformed``        shape/encoding/zlib/JSON failure or truncation
``cursor_unknown_version``  version prefix is not recognised
``cursor_bad_signature``    HMAC missing/incorrect (tampering or wrong key)
``cursor_too_large``        token or payload exceeds its hard cap
``cursor_input_mismatch``   authenticated cursor bound to another L/n/multiset
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import zlib
from collections import Counter
from typing import Final, Mapping, Sequence

VERSION: Final = "tp1"
_SEPARATOR: Final = "."
_MAC_LABEL: Final = b"turnpike-enumerate-v1"

# Hard, public caps.  The token cap bounds request size; the payload cap bounds
# decompression independently so a small compressed bomb cannot inflate freely.
MAX_TOKEN_BYTES: Final = 1 << 18  # 256 KiB on the wire
MAX_PAYLOAD_BYTES: Final = 1 << 20  # 1 MiB after decompression

# Used only when TURNPIKE_CURSOR_SECRET is unset, so zero-config development and
# the docker compose acceptance chain keep working.  Deployments MUST set the
# variable; cursors minted under another secret are always rejected.
_DEV_FALLBACK_SECRET: Final = "turnpike-dev-insecure-fallback-key"


class CursorError(Exception):
    """A cursor rejection with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _secret_key() -> bytes:
    secret = os.environ.get("TURNPIKE_CURSOR_SECRET", _DEV_FALLBACK_SECRET)
    # Normalise through SHA-256 so arbitrary-length env strings yield a
    # fixed-size key; different secrets never alias.
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    try:
        padding = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + padding)
    except Exception as exc:  # binascii error / non-ascii / wrong type
        raise CursorError("cursor_malformed", "cursor is not valid base64") from exc


def input_digest(
    length: int, n: int, distances: Counter[int] | Mapping[int, int]
) -> str:
    """Digest of the *normalised* request: L, n and the distance multiset.

    The multiset is encoded as sorted ``(distance, count)`` pairs, so any
    permutation of the input list yields the same digest.
    """
    body = ",".join(
        f"{distance}:{count}" for distance, count in sorted(distances.items())
    )
    message = f"{length}\n{n}\n{body}".encode("utf-8")
    return hashlib.sha256(message).hexdigest()


def class_marker(
    length: int, canonical: Sequence[int]
) -> str:
    """Compact identity of a discovered canonical mirror class.

    Storing full witnesses cumulatively would let deeply ambiguous searches
    grow the cursor without bound; instead the cursor keeps, per class, only
    this short, domain-separated digest of the canonical coordinate sequence.
    It is deterministic and input-independent, and it is carried inside the
    authenticated envelope, so it cannot be forged to suppress a class.
    """
    message = (
        str(length) + "|" + ",".join(str(point) for point in canonical)
    ).encode("utf-8")
    return hashlib.sha256(_MAC_LABEL + message).hexdigest()[:32]


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def encode_cursor(
    state: Mapping[str, object],
    length: int,
    n: int,
    digest: str,
) -> str:
    """Serialise, compress and authenticate a suspended-search snapshot."""
    envelope = {"i": [length, n, digest], "s": state}
    raw = _canonical_json(envelope)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise CursorError(
            "cursor_too_large",
            "serialised search state exceeds the payload size limit",
        )
    compressed = zlib.compress(raw, level=9)
    signature = hmac.new(
        _secret_key(), _MAC_LABEL + compressed, hashlib.sha256
    ).digest()
    token = (
        VERSION
        + _SEPARATOR
        + _b64encode(compressed)
        + _SEPARATOR
        + _b64encode(signature)
    )
    if len(token.encode("ascii")) > MAX_TOKEN_BYTES:
        raise CursorError(
            "cursor_too_large", "cursor exceeds the maximum encoded size"
        )
    return token


def _guarded_decompress(compressed: bytes) -> bytes:
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(compressed, MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES or decompressor.unconsumed_tail:
            raise CursorError(
                "cursor_too_large",
                "decompressed cursor state exceeds the payload size limit",
            )
        tail = decompressor.flush()
        if tail or decompressor.unused_data:
            raise CursorError("cursor_malformed", "cursor payload is corrupt")
    except zlib.error as exc:
        raise CursorError("cursor_malformed", "cursor payload is corrupt") from exc
    return raw


def decode_cursor(
    token: object,
    length: int,
    n: int,
    digest: str,
) -> dict[str, object]:
    """Authenticate and parse a cursor, enforcing the current input binding.

    Raises :class:`CursorError` for every malformed, untrusted, oversized or
    input-mismatched token.  Only the fixed code strings escape; no internal
    detail, state or key material is ever included.
    """
    if not isinstance(token, str):
        raise CursorError("cursor_malformed", "cursor must be a string")
    if len(token.encode("utf-8", errors="ignore")) > MAX_TOKEN_BYTES:
        raise CursorError("cursor_too_large", "cursor exceeds the maximum size")

    parts = token.split(_SEPARATOR)
    if len(parts) != 3 or not all(parts):
        raise CursorError("cursor_malformed", "cursor has the wrong shape")
    version, compressed_text, signature_text = parts
    if version != VERSION:
        # Do not echo client data back; the code already distinguishes this case.
        raise CursorError(
            "cursor_unknown_version", "cursor uses an unsupported version"
        )

    compressed = _b64decode(compressed_text)
    signature = _b64decode(signature_text)
    expected = hmac.new(
        _secret_key(), _MAC_LABEL + compressed, hashlib.sha256
    ).digest()
    if len(signature) != len(expected) or not hmac.compare_digest(
        signature, expected
    ):
        # Covers tampering, truncation of either field and a rotated/foreign
        # secret; the caller must not learn which component differed.
        raise CursorError(
            "cursor_bad_signature", "cursor authentication failed"
        )

    raw = _guarded_decompress(compressed)
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("cursor_malformed", "cursor payload is corrupt") from exc
    if not isinstance(envelope, dict):
        raise CursorError("cursor_malformed", "cursor payload is corrupt")

    binding = envelope.get("i")
    state = envelope.get("s")
    if (
        not isinstance(binding, list)
        or len(binding) != 3
        or not isinstance(binding[0], int)
        or not isinstance(binding[1], int)
        or not isinstance(binding[2], str)
        or not isinstance(state, dict)
    ):
        raise CursorError("cursor_malformed", "cursor payload is corrupt")

    bound_length, bound_n, bound_digest = binding
    if (
        bound_length != length
        or bound_n != n
        or not hmac.compare_digest(bound_digest, digest)
    ):
        raise CursorError(
            "cursor_input_mismatch",
            "cursor does not match this L, n and distance multiset",
        )
    return state
