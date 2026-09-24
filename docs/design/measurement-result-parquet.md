# Measurement Results in Parquet

**Status:** Draft for review; exploratory example

**Companion to:** `benchmark-result-schema.md` §5 (the message; where this document and the schema differ, the schema wins), `prototype-design.md` §4 (the prototype store whose `results/` table this row defines)

**Author:** Rok Mihevc

## 1. Scope

This document shows the schema under which a validated measurement result (§5.2 ingest object) is stored in a Parquet file, and a small PyArrow program that writes one: [`tools/json_to_parquet.py`](../../tools/json_to_parquet.py). It defines nothing about the message itself.

One row is one result, that is one attempt and one quantity. A row holds the reported facts only. Series fingerprints, derived estimates, and entity tables are computed by a store at ingest (§4.3, §5.3) and sit beside these columns (§4 below); they are not part of a message. The mapping is not byte-preserving, so a store that needs the original document keeps it separately, as the prototype store does under `raw/`.

## 2. Mapping

The Arrow schema is derived from [`schema.json`](../../schemas/measurement-result/0.1.0/schema.json) by the rules below, so it follows the message schema without being maintained by hand.

| JSON Schema node | Arrow and Parquet type |
|---|---|
| closed object (`additionalProperties: false`) | `struct` with the same field order; required properties are non-nullable |
| open object, or a value of mixed primitive types | `json`: the Arrow JSON extension type, written with the Parquet JSON logical type, holding RFC 8785 canonical text |
| `oneOf` of closed objects (`summaries`) | one `struct` with every property any alternative declares, in first-appearance order; non-nullable only when every alternative requires it; alternatives that give one property different types are an error, not a silent choice |
| `oneOf` of number and structured observation (`observations`) | the structured observation's `struct`; a bare number is stored with only `value` set |
| `allOf` of a reference and extra assertions (`derivation`) | the referenced type |
| array | `list` |
| string with `format: date-time` | `timestamp[us, UTC]` |
| `const` or `enum` | by its values: all strings `string`, all integers `int64`, other numbers `float64`, all booleans `bool`, mixed `json` |
| `oneOf` of primitives only | `json` |
| string | `string` |
| integer | `int64` |
| number | `float64` |
| boolean | `bool` |

Open objects stay JSON because their keys are project-defined: workload parameters, subject configuration, comparison context, environment identity, resource selection, observed context, and procedure. Canonical text makes equal values equal strings. A store promotes a key to a typed column only when a query needs it.

## 3. Resulting schema

For message schema 0.1.0, whose messages carry `schema_version: 5`: the file name and `$id` version the schema document, the integer versions the message contract. `*` marks a non-nullable field.

```text
schema_version*: int64
producer*: struct
  name*: string, version*: string, mapping_version*: string
ingest_key*: string, attempt_key*: string, project: string
source*: struct
  uri*: string, type: string
revision*: struct
  key*: string, native_order: int64, native_time: timestamp[us, UTC]
benchmark: struct
  source*: struct                       (as source)
  revision*: struct                     (as revision)
coordinates*: struct
  workload*: struct
    name*: string, parameters: json
    dataset: struct
      name*: string, version: string, sha256: string, parameters: json
  subject*: struct
    name*: string
    components*: list of struct
      role*: string, source: string, revision: string
    configuration: json
  quantity*: struct
    name*: string, unit*: string, direction: string, deterministic: bool
  comparison_context*: json
  environment*: struct
    schema*: string, identity*: json, metadata: json
  resource_selection: json
measurement*: struct
  status*: string, reason: string
  constraint: struct
    kind*: string, lower: float64, upper: float64,
    lower_inclusive: bool, upper_inclusive: bool, cause*: string
  observations: list of struct
    value*: float64, ordinal: int64, inner_iterations: int64, slot: int64,
    time: timestamp[us, UTC],
    group: string, pair: string, included: bool, exclusion_reason: string
  derivation: struct                    (as estimator)
  summaries: list of struct
    type*: string
    estimator: struct
      name*: string, method_version*: string, input_level*: string,
      parameters: json
      inputs: list of struct
        role*: string, quantity*: string
    value: float64, source*: string, name: string, method: string,
    statistic: string, level: float64, lower: float64, upper: float64,
    label: string, coverage_factor: float64
observed_context: json, procedure: json
provenance*: struct
  run_key: string, labels: json, native_id: string,
  started_at*: timestamp[us, UTC], ended_at: timestamp[us, UTC],
  subject_dirty*: string, subject_tree: string,
  benchmark_dirty*: string, benchmark_tree: string
  runner: struct
    name*: string, version: string
  original_unit: string, source_payload_uri: string,
  source_payload_sha256: string, ci_uri: string
  references: list of struct
    role*: string
    result*: struct
      producer*: string, ingest_key*: string
  artifacts: list of struct
    kind*: string, media_type*: string, schema: string, uri*: string,
    sha256*: string
  info: json
quality: struct
  warnings: list of string, validation: json
```

The file's key-value metadata records which message schema it was written under:

```text
benchx.schema_uri            = urn:benchx:schema:measurement-result:0.1.0
benchx.message_schema_sha256 = <SHA-256 of the RFC 8785 form of schema.json>
```

The checksum matters while 0.1.0 is a draft that changes under one URI.

## 4. Columns a store adds

The prototype store (`prototype-design.md` §4) writes this row as its `results/` table and adds, beside the message columns and outside the `coordinates`, `measurement`, and `provenance` structs, the columns it derives at ingest:

```text
reported_coordinates_fingerprint*: string   schema §4.3
series_fingerprint: string                  per identity-policy schema; absent for thin results
estimate: float64, estimate_source: string  the series point's value and whether producer or derived
payload_sha256*: string                     of the RFC 8785 form, for idempotency checks
ingested_at*: timestamp[us, UTC]
round: int64, slot: int64                   promoted from procedure
inner_iterations: int64                     promoted from procedure
```

The last three are promoted because the comparator's run mode pairs attempts by `procedure.round` and checks the schedule by `procedure.slot` (schema §5.5), and per-iteration values need the loop count; reading them from a JSON column would parse it on every row of every comparison. They are copies: the `procedure` column keeps the message's value, and a promoted column is rebuilt from it. Other `procedure` or `comparison_context` keys are promoted the same way when a query needs them, never by changing the message schema.

**Appending and schema changes.** A store appends one file per ingest call. Because the Arrow schema is derived, a change to `schema.json` changes the file schema, and a dataset then holds files written under several message schemas, told apart by `benchx.message_schema_sha256`. Additive changes, a new optional field such as `inner_iterations` on observations, are read together with PyArrow's permissive schema unification (`pyarrow.dataset` with `unify_schemas(..., promote_options="permissive")`), where missing fields read as null. A change that removes a field or changes its type is a new message contract version (`schema_version`) and is written to a separate dataset or rewritten on migration, never mixed.

## 5. Writing and reading

The program reads each message strictly, validates it against the JSON Schema, derives the Arrow schema, and writes all messages given to it as the rows of one file:

```bash
pip install jsonschema 'pyarrow>=19' rfc8785   # Python 3.11 or newer
python tools/json_to_parquet.py schemas/measurement-result/0.1.0/schema.json \
    results.parquet schemas/measurement-result/0.1.0/examples/*.json
```

Input is strict because JSON Schema validation only sees a message after it has been parsed, when some defects are already invisible. Messages use the I-JSON data model (RFC 7493), and the program rejects what falls outside it before validating:

- **Duplicate object keys.** A parser keeps one of them silently, so two readers could store different values for the same message.
- **`NaN`, `Infinity`, and `-Infinity`.** Python's parser accepts these tokens although JSON does not, and they have no place in a finite observation.
- **Numbers outside the RFC 8785 domain.** Every message is passed through RFC 8785 serialization, which refuses integers beyond ±2^53, the I-JSON safe-integer range. A larger value is read differently by parsers that hold numbers as binary64, so it would not mean the same thing to every reader.

The same canonical form is what the `json` columns hold and what the schema checksum is computed over. One rejected message fails the run and nothing is written.

Any Arrow-capable engine reads the result directly. In DuckDB, structs are addressed with dots, the observation list with list functions, and JSON columns with JSON paths:

```sql
SELECT coordinates.workload.name     AS workload,
       coordinates.quantity.name     AS quantity,
       len(measurement.observations) AS n,
       list_min([o.value FOR o IN measurement.observations])  AS fastest,
       coordinates.comparison_context->>'$.protocol.timer'    AS timer
FROM 'results.parquet'
ORDER BY workload, quantity;
```

```text
workload      quantity        n     fastest   timer
matmul        cpu-time        10    0.000212  perf_counter
matmul        gpu-time        10    0.01836   cuda-events
parquet-read  wall-time       5     0.0363    perf_counter
truncate      overhead-ratio  NULL  NULL      perf_counter
truncate      wall-time       5     0.000128  perf_counter
```

The overhead ratio is an aggregate-only compound result, so it has no observations.

`tests/test_json_to_parquet.py` converts the examples and checks row count, nullability, canonical JSON, observation values, file metadata, strict input, and the type-mapping rules:

```bash
python -m unittest tests/test_json_to_parquet.py
```

## 6. Not covered

- The rules the message schema leaves to the ingest server or comparator: idempotency and conflicts on `(producer, ingest_key)`, unit immutability, attempt integrity, comparison profiles.
- Entity tables, partitioning, and compaction; the prototype store rewrites its small entity tables and compacts on request.
- Reading a message back out of Parquet. Member order, number spelling, and whether an observation was a bare number are not kept.
- Timestamps finer than microseconds are truncated to the column's precision.
