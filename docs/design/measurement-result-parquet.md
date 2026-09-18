# Measurement Results in Parquet

**Status:** Draft for review; exploratory example

**Companion to:** `benchmark-result-schema.md` §5 (the message; where this document and the schema differ, the schema wins)

**Author:** Rok Mihevc

## 1. Scope

This document shows the schema under which a validated measurement result (§5.2 ingest object) is stored in a Parquet file, and a small PyArrow program that writes one: [`tools/json_to_parquet.py`](../../tools/json_to_parquet.py). It defines nothing about the message itself.

One row is one result, that is one attempt and one quantity. A row holds the reported facts only. Series fingerprints, derived estimates, and entity tables are computed by a store at ingest (§4.3, §5.3) and would sit beside these columns; they are not part of a message and so not part of this schema. The mapping is not byte-preserving, so a store that needs the original document keeps it separately.

## 2. Mapping

The Arrow schema is derived from [`schema.json`](../../schemas/measurement-result/0.1.0/schema.json) by the rules below, so it follows the message schema without being maintained by hand.

| JSON Schema node | Arrow and Parquet type |
|---|---|
| closed object (`additionalProperties: false`) | `struct` with the same field order; required properties are non-nullable |
| open object, or a value of mixed primitive types | `json`: the Arrow JSON extension type, written with the Parquet JSON logical type, holding RFC 8785 canonical text |
| `oneOf` of closed objects (`summaries`) | one `struct` with every property any alternative declares, in first-appearance order; non-nullable only when every alternative requires it |
| `oneOf` of number and structured observation (`observations`) | the structured observation's `struct`; a bare number is stored with only `value` set |
| `allOf` of a reference and extra assertions (`derivation`) | the referenced type |
| array | `list` |
| string with `format: date-time` | `timestamp[us, UTC]` |
| string, enum, string const | `string` |
| integer, integer const | `int64` |
| number | `float64` |
| boolean | `bool` |

Open objects stay JSON because their keys are project-defined: workload parameters, subject configuration, comparison context, environment identity, resource selection, observed context, and procedure. Canonical text makes equal values equal strings. A store promotes a key to a typed column only when a query needs it.

## 3. Resulting schema

For message schema 0.1.0; `*` marks a non-nullable field.

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

## 4. Writing and reading

The program validates each message against the JSON Schema, derives the Arrow schema, and writes all messages given to it as the rows of one file:

```bash
pip install jsonschema pyarrow rfc8785
python tools/json_to_parquet.py schemas/measurement-result/0.1.0/schema.json \
    results.parquet schemas/measurement-result/0.1.0/examples/*.json
```

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

## 5. Not covered

- The rules the message schema leaves to the ingest server or comparator: idempotency and conflicts on `(producer, ingest_key)`, unit immutability, attempt integrity, comparison profiles.
- Derived columns and tables, appending to a dataset, partitioning, and compaction.
- Reading a message back out of Parquet. Member order, number spelling, and whether an observation was a bare number are not kept.
- Timestamps finer than microseconds are truncated to the column's precision.
