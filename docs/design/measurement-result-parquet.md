# Measurement Results in Parquet

**Status:** Draft for review

**Companion to:** `benchmark-result-schema.md` §5 (the message; where this document and the schema differ, the schema wins), `prototype-design.md` §4 (the local store, whose row begins with these columns)

**Author:** Rok Mihevc

## 1. Scope

This document defines the columns under which a validated measurement result (§5.2 ingest object) is stored in Parquet: the mapping from the message JSON Schema to Arrow types, implemented in `src/benchx/parquet.py`, and a small program that writes them from JSON files, `tools/json_to_parquet.py`. It defines nothing about the message itself.

One row is one result, that is one attempt and one quantity. These *message columns* hold the reported facts only. Fingerprints, derived estimates, and entity tables are computed by a store and sit beside them (§4). The mapping is not byte-preserving, so a store that needs the original document keeps it beside the columns, as the local store does in its `document` column.

## 2. Mapping

The Arrow schema is derived from `src/benchx/schemas/measurement-result/0.1.0/schema.json` by the rules below, so it follows the message schema without being maintained by hand.

| JSON Schema node | Arrow and Parquet type |
|---|---|
| closed object (`additionalProperties: false`) | `struct` with the same field order; required properties are non-nullable |
| open object, or a value of mixed primitive types | `json`: the Arrow JSON extension type, written with the Parquet JSON logical type, holding RFC 8785 canonical text |
| `oneOf` of closed objects (`summaries`) | one `struct` with every property any alternative declares, in first-appearance order; non-nullable only when every alternative requires it; alternatives that give one property different types are an error, not a silent choice |
| `oneOf` of number and structured observation (`observations`) | the structured observation's `struct`; a bare number is stored with only `value` set |
| `allOf` of a reference and extra assertions (`derivation`) | the referenced type |
| array | `list` |
| string with `format: date-time` | `timestamp[us, UTC]` |
| string, enum, string const | `string` |
| integer, integer const | `int64` |
| number | `float64` |
| boolean | `bool` |
| any other `const`, `enum`, or `oneOf` | an error, so that a schema change the rules do not cover is noticed rather than stored loosely |

Open objects stay JSON because their keys are project-defined: workload parameters, subject configuration, the protocol, environment identity and system description, resource selection, labels, and procedure. Canonical text makes equal values equal strings. A store promotes a key to a typed column only when a query needs it.

## 3. Resulting schema

For message schema 0.1.0, whose messages carry `schema_version: "benchx/measurement-result/0.1.0"`. `*` marks a non-nullable field.

```text
schema_version*: string
producer*: struct
  name*: string, version*: string, mapping_version*: string
ingest_key*: string, attempt_key*: string, project: string
source*: struct
  uri*: string, type: string
revision*: struct
  key*: string, native_order: int64, native_time: timestamp[us, UTC]
benchmark: struct
  source*: struct                       (as source)
  revision: struct                      (as revision)
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
  protocol*: json
  environment*: struct
    schema_version*: string, identity*: json, system: json
  resource_selection: json
measurement*: struct
  quantity*: struct
    name*: string, unit*: string, direction: string, deterministic: bool
  kind: string, status*: string, reason: string
  constraint: struct
    kind*: string, lower: float64, upper: float64,
    lower_inclusive: bool, upper_inclusive: bool, cause*: string
  observations: list of struct
    value*: float64, ordinal: int64, slot: int64, time: timestamp[us, UTC],
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
procedure: json
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

## 4. Beside the message columns

A store adds the columns it derives at ingest after the message columns, and may copy a key out of an open object into a typed column when a query needs it. The local store adds its project, `reported_coordinates_fingerprint`, `payload_sha256`, an attempt signature, the canonical `document`, and the work order the result references (`parquet.DERIVED`; `prototype-design.md` §4). Those columns and the store's layout belong to the store's design, not to this document.

Because the Arrow schema is derived, a change to `schema.json` changes the file schema, and files written under different message schemas are told apart by `benchx.message_schema_sha256`. An added optional field reads as null from older rows under PyArrow's permissive schema unification, which is how the local store carries old rows over when it rewrites its file; removing a field or changing its type is a new message contract and is not mixed into one file.

## 5. Writing and reading

The program reads each document strictly and checks it as `bx validate` does, then writes all documents given to it as the rows of one file:

```bash
pip install benchx
python tools/json_to_parquet.py results.parquet \
    src/benchx/schemas/measurement-result/0.1.0/examples/*.json
```

Input is strict because JSON Schema validation only sees a message after it has been parsed, when some defects are already invisible. Messages use the I-JSON data model (RFC 7493), and `benchx.core.loader` rejects what falls outside it before validating:

- **Duplicate object keys.** A parser keeps one of them silently, so two readers could store different values for the same message.
- **`NaN`, `Infinity`, and `-Infinity`.** Python's parser accepts these tokens although JSON does not, and they have no place in a finite observation.
- **Integers beyond ±(2^53 − 1).** A larger value is read differently by parsers that hold numbers as binary64, so it would not mean the same thing to every reader, and RFC 8785 cannot serialize it.

RFC 8785 is the canonical form the `json` columns hold and the schema checksum is computed over. One rejected document fails the run and nothing is written.

Any Arrow-capable engine reads the result directly, and the same query runs against the local store, `store.parquet`, since its row begins with these columns. In DuckDB, structs are addressed with dots, the observation list with list functions, and JSON columns with JSON paths:

```sql
SELECT coordinates.workload.name     AS workload,
       measurement.quantity.name     AS quantity,
       len(measurement.observations) AS n,
       list_min([o.value FOR o IN measurement.observations]) AS minimum,
       coordinates.protocol->>'$.timer' AS timer
FROM 'results.parquet'
ORDER BY workload, quantity;
```

```text
workload      quantity         n     minimum   timer
matmul        cpu-time         10    0.000212  perf_counter
matmul        gpu-time         10    0.01836   cuda-events
parquet-read  cpu-temperature  4     61.0      NULL
parquet-read  wall-time        5     0.0363    perf_counter
truncate      overhead-ratio   NULL  NULL      perf_counter
truncate      wall-time        5     0.000128  perf_counter
```

The temperature is a condition of the wall-time attempt, read from a sensor rather than timed. The overhead ratio is an aggregate-only compound result, so it has no observations.

`tests/test_parquet.py` checks the mapping over the examples (row order, nullability, canonical JSON, observation values, file metadata), the rules that must fail, and the program.

## 6. Not covered

- The rules the message schema leaves to the ingest server or comparator: idempotency and conflicts on `(producer, ingest_key)`, unit immutability, attempt integrity, comparison profiles. The local store implements them (`prototype-design.md` §4).
- Entity tables, partitioning, and compaction.
- Reading a message back out of the message columns. Member order, number spelling, and whether an observation was a bare number are not kept; a store that needs the document keeps it (§1).
- Timestamps finer than microseconds are truncated to the column's precision.
