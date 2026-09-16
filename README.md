# Turnpike Restriction-Map Reconstruction Service

A pure-backend service that reconstructs a restriction-enzyme cut map from
capillary-electrophoresis data. Electrophoresis yields only the **multiset of
all pairwise distances** between cut sites; duplicate fragments appear as
repeated distances (they are never collapsed), the map and its left–right
mirror image describe the same molecule, and partial peaks must never be
stitched together into fabricated cut sites.

The core is a hand-written **turnpike reconstruction** with counted multiset
deduction, fully rollback-able branch pruning and mirror deduplication. It
does **not** call a generic constraint solver and never enumerates the integer
coordinate interval `[0, L]` — every candidate coordinate is derived from a
remaining distance.

## Request semantics

`POST /turnpike` with a JSON body:

| Field       | Type        | Constraint                                             |
|-------------|-------------|--------------------------------------------------------|
| `L`         | integer     | total length, `2 ≤ L ≤ 10^9`                           |
| `n`         | integer     | number of cut points, `2 ≤ n ≤ 32`                     |
| `distances` | int array   | exactly `n(n-1)/2` entries, each in `[1, L]`, repeats preserved |

A solution is a strictly increasing sequence of `n` points that includes both
end points, i.e. `0 = p₀ < p₁ < … < pₙ₋₁ = L`. The mirror image
`{L − x : x ∈ p}` is the **same** solution; each equivalence class is
represented by its lexicographically smaller member.

### Responses

* `200 {"status": "impossible"}` — the input is well-formed but no map exists.
  No partial placement is ever returned.
* `200 {"status": "unique", "solution": [0, …, L]}` — exactly one canonical
  map.
* `200 {"status": "ambiguous", "solutions": [[…], […]]}` — at least two
  distinct canonical maps exist; the two lexicographically smallest are
  returned in order.

Every returned witness is accepted only after **regenerating its full distance
multiset** and comparing it, count by count, with the submitted distances.
Identical distance multisets (including a reordering of the input list)
produce byte-for-byte identical responses.

### Structured errors (HTTP 422)

Quantitative contradictions and out-of-range values are reported with the
first offending item:

* `invalid_distance_count` — length of `distances` ≠ `n(n-1)/2`
  (`error.expected`, `error.actual`, `error.item.expected_size`).
* `distance_out_of_range` — first entry outside `[1, L]`
  (`error.item.index`, `error.item.value`, `error.item.lower`,
  `error.item.upper`).
* `invalid_request` — type/bound violations on `L`/`n`, non-integer values,
  unknown fields, or malformed JSON (`error.problems` lists all locations).

## Pausable full enumeration: `POST /turnpike/enumerate`

When a sample is ambiguous the experimenter must check **every** feasible map,
not only the first two witnesses, while no single request may pin a worker for
long. The enumerate endpoint runs the *same* search kernel as `/turnpike` as a
pausable/resumable state machine. The server keeps **no** session state: a
continuation cursor is returned to the client and presented on the next call.

| Field         | Type      | Constraint                              |
|---------------|-----------|-----------------------------------------|
| `L`, `n`, `distances` | as `/turnpike` | identical semantics; continuations must repeat the same normalised input (a permutation of `distances` is the same request) |
| `page_size`   | integer   | `1 … 100`, max new solutions per page   |
| `node_budget` | integer   | `1 … 1_000_000`, max placement attempts per request |
| `cursor`      | string    | omit on the first request; return the prior page's cursor afterwards |

A **node** is one placement attempt of a candidate coordinate derived from a
remaining distance. The attempt counts even if it fails immediately because
the coordinate is out of bounds, duplicates an already placed point, or the
remaining multiset cannot cover its distances. Accepting a finished witness
and rolling a branch back are not attempts and cost no nodes, so a budget is
checked only at an attempt boundary — a deduction is never cut halfway.

Response fields:

* `solutions` — canonical mirror classes discovered **on this page**, in the
  fixed, publicly defined DFS order (root: only `L−d`; deeper: `L−d` then
  `d`; duplicates within a frame attempted once);
* `emitted` — classes already emitted on earlier pages (`0` on the first
  request);
* `nodes_used` — attempts consumed by this request;
* `complete` / `stop_reason` — `complete`, `page_full` or `budget_exhausted`
  with the fixed priority complete → page full → budget;
* `total_classes` — present only when `complete` is true (it is `0` for a
  legal input with no solution);
* `cursor` — present only while the search is incomplete.

The paging parameters may change between calls: they only move page
boundaries. The concatenated solution sequence and the cumulative node count
are identical for any combination of `page_size`/`node_budget`; there are no
gaps and no duplicate classes across pages. Every witness is re-derived from
its full pairwise-distance multiset before it is emitted.

### Cursors: format, size cap and key rotation

The cursor is `tp1.<zlib(compact-json)>.<HMAC-SHA256>` (base64url), versioned
and deterministic — the same state with the same key is byte-for-byte
identical, so identical replays (same input, same cursor) return identical
bytes. It embeds a digest of `L`, `n` and the normalised distance multiset.
The wire token is capped at 256 KiB and the decompressed payload at 1 MiB.

Authentication uses the secret in `TURNPIKE_CURSOR_SECRET`
(HMAC-SHA256, constant-time comparison):

* **restart** — cursors are serverless; an API restart with the same secret
  leaves every outstanding cursor valid;
* **rotation** — changing the variable (e.g. `API_CURSOR_SECRET=… docker
  compose up -d api`) immediately and safely rejects all old cursors with
  `cursor_bad_signature`; clients simply restart the enumeration without a
  cursor. Rotate whenever a secret may have leaked; no state is lost on the
  server because none is held there;
* if the variable is unset, a clearly-named development fallback is used so
  local and compose runs work zero-config — always set a strong value in
  production.

Structured cursor errors (HTTP 422, `item.field == "cursor"`, never containing
stack traces, state or key material): `cursor_malformed` (bad shape,
encoding, compression or truncation), `cursor_unknown_version`,
`cursor_bad_signature` (tampering or wrong/rotated secret),
`cursor_too_large`, and `cursor_input_mismatch` (cursor from another `L`, `n`
or distance multiset).

### Recovery example

```bash
# first page: at most 10 classes, spend at most 100000 attempts
curl -s localhost:8000/turnpike/enumerate -H 'content-type: application/json' \
  -d '{"L":17,"n":6,
       "distances":[1,4,10,12,17,3,9,11,16,6,8,13,2,7,5],
       "page_size":10,"node_budget":100000}'
# {"solutions":[[0,1,4,10,12,17],[0,1,8,11,13,17]],"emitted":0,
#  "nodes_used":13,"complete":true,"stop_reason":"complete",
#  "total_classes":2,"cursor":null}

# a harder sample: loop until "complete" is true, carrying the cursor and
# repeating the *same* normalised L/n/distances; page_size and node_budget may
# be tuned on every call
python - <<'PY'
import json, urllib.request
body = {"L": 17, "n": 6,
        "distances": [1,4,10,12,17,3,9,11,16,6,8,13,2,7,5],
        "page_size": 1, "node_budget": 4}
cursor = None
all_solutions = []
while True:
    if cursor is not None:
        body["cursor"] = cursor
    req = urllib.request.Request(
        "http://localhost:8000/turnpike/enumerate",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"}, method="POST")
    page = json.load(urllib.request.urlopen(req))
    all_solutions += page["solutions"]
    if page["complete"]:
        print("total:", page["total_classes"], all_solutions)
        break
    cursor = page["cursor"]  # persist it; a restart with the same key is fine
PY
```

### Examples

```bash
# unique
curl -s localhost:8000/turnpike -H 'content-type: application/json' \
  -d '{"L":10,"n":5,"distances":[2,4,7,10,2,5,8,3,6,3]}'
# {"status":"unique","solution":[0,2,4,7,10]}

# impossible (legal input, no embedding)
curl -s localhost:8000/turnpike -H 'content-type: application/json' \
  -d '{"L":5,"n":3,"distances":[3,4,5]}'
# {"status":"impossible"}

# ambiguous: two genuinely homometric maps
curl -s localhost:8000/turnpike -H 'content-type: application/json' \
  -d '{"L":17,"n":6,"distances":[1,4,10,12,17,3,9,11,16,6,8,13,2,7,5]}'
# {"status":"ambiguous","solutions":[[0,1,4,10,12,17],[0,1,8,11,13,17]]}

# first bad item identified
curl -s localhost:8000/turnpike -H 'content-type: application/json' \
  -d '{"L":5,"n":3,"distances":[2,6,5]}'
# {"error":{"code":"distance_out_of_range",...,"item":{"field":"distances",
#  "index":1,"value":6,"lower":1,"upper":5}}}
```

## Running with Docker Compose

The API's host port is taken from `API_PORT` (default `8000`), and the cursor
authentication secret from `API_CURSOR_SECRET` (a development default is
provided; set your own for anything beyond local use):

```bash
API_PORT=8042 API_CURSOR_SECRET=$(openssl rand -hex 32) \
  docker compose up --build api
curl -s localhost:8042/healthz
```

### One-shot acceptance service

```bash
docker compose run --build verify
```

The `verify` service waits for the API to become healthy, runs the pytest
suite (`tests/`) and then `verify/acceptance.py`, which exercises unique,
ambiguous and impossible cases end-to-end over HTTP, independently
regenerates each witness's distance counts, checks both structured-error
shapes and verifies byte-identical repeated responses. It additionally drives
`/turnpike/enumerate` through multi-page ambiguity, page-size/budget changes,
exact budget exhaustion, mirror collapse, the no-solution case, byte-identical
replays, tampered/truncated/unknown-version/oversized cursors, input
mismatch, real process-restart recovery and secret-rotation rejection. It
exits non-zero on any failure and is not a long-running service
(`restart: "no"`).

## Local development

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
pytest
```

## Layout

```
app/turnpike.py   # shared counted-multiset rollback DFS kernel (single-shot + resumable)
app/cursor.py     # versioned deterministic HMAC cursor encoding / verification
app/schemas.py    # Pydantic request/response models
app/main.py       # FastAPI app, semantic validation, structured errors
tests/            # core, enumerate, cursor attacks, restart/rotation tests
verify/           # one-shot end-to-end acceptance script
```
