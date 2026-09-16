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

The API's host port is taken from `API_PORT` (default `8000`):

```bash
API_PORT=8042 docker compose up --build api
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
shapes and verifies byte-identical repeated responses. It exits non-zero on
any failure and is not a long-running service (`restart: "no"`).

## Local development

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
pytest
```

## Layout

```
app/turnpike.py   # counted multiset deduction + rollback-able turnpike search
app/schemas.py    # Pydantic request/response models
app/main.py       # FastAPI app, semantic validation, structured errors
tests/            # core and HTTP tests
verify/           # one-shot end-to-end acceptance script
```
