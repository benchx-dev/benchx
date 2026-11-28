"""The local store: one Parquet file (`store.parquet`) or JSONL file (`store.jsonl`).

One row per result: the validated canonical document plus the store's derived
columns (store_project, reported_coordinates_fingerprint, payload_sha256,
attempt_signature, work_order_ref, work_order). Parquet (`benchx[parquet]`) is
used when PyArrow is available and adds typed message columns
(measurement-result-parquet.md); JSONL uses only the standard library and
enforces the exact same schema §5.4 ingest contract.

Entities, series, and series points are computed from the rows when asked for;
nothing else is stored. Every accepting ingest rewrites the file atomically (to
a temporary file beside it, fsynced, then renamed over it) so an acknowledgment
means validated and durable.
"""

import json
import logging
import os
import statistics
import time
from datetime import datetime
from pathlib import Path

from . import core, identity
from .projection import Projection
from .results import ingest_documents, store_project, sweep_paths, warning_codes

JSON_SUFFIXES = (".jsonl", ".json", ".ndjson")


def _is_json_path(path: Path) -> bool:
    return path.suffix.lower() in JSON_SUFFIXES


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "pyarrow":
            raise ModuleNotFoundError(
                "Parquet store requires pyarrow; install it with `pip install 'benchx[parquet]'`.",
                name="pyarrow",
            ) from exc
        raise
    from . import parquet

    return pa, pq, parquet


logger = logging.getLogger(__name__)


class Store:
    """The local store, one Parquet or JSONL file (prototype-design.md §4). Its place
    is the project's: `Config.store_path`, by default `store.parquet` (with pyarrow)
    or `store.jsonl` (without) in the project root."""

    def __init__(self, path):
        self.path = Path(path)
        self._json = _is_json_path(self.path)
        if not self._json:
            self._pa, self._pq, self._parquet = _require_pyarrow()

    # -- reading -------------------------------------------------------------

    def _table(self, columns=None):
        if not self.path.exists():
            logger.debug("%s: absent, read as an empty store", self.path)
            return None
        started = time.monotonic()
        table = self._pq.read_table(self.path, columns=columns)
        logger.debug(
            "%s: read %d row(s), %s, in %.2f s",
            self.path,
            table.num_rows,
            ", ".join(columns) if columns else "all columns",
            time.monotonic() - started,
        )
        return table

    def _json_rows(self) -> list[dict]:
        if not self.path.exists():
            logger.debug("%s: absent, read as an empty store", self.path)
            return []
        started = time.monotonic()
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        logger.debug("%s: read %d row(s) in %.2f s", self.path, len(rows), time.monotonic() - started)
        return rows

    def _index(self) -> list[dict]:
        if self._json:
            out = []
            for row in self._json_rows():
                doc = json.loads(row["document"])
                quantity = doc["measurement"]["quantity"]
                out.append(
                    {
                        "producer": doc["producer"]["name"],
                        "ingest_key": doc["ingest_key"],
                        "attempt_key": doc["attempt_key"],
                        "payload_sha256": row["payload_sha256"],
                        "attempt_signature": row["attempt_signature"],
                        "store_project": row["store_project"],
                        "quantity": quantity["name"],
                        "unit": quantity["unit"],
                    }
                )
            return out
        table = self._table(
            [
                "producer",
                "ingest_key",
                "attempt_key",
                "payload_sha256",
                "attempt_signature",
                "store_project",
                "measurement",
            ]
        )
        if table is None:
            return []
        producers = table.column("producer").combine_chunks().field("name").to_pylist()
        quantity = table.column("measurement").combine_chunks().field("quantity")
        rest = table.select(
            ["ingest_key", "attempt_key", "payload_sha256", "attempt_signature", "store_project"]
        ).to_pylist()
        return [
            dict(row, producer=p, quantity=q, unit=u)
            for row, p, q, u in zip(
                rest, producers, quantity.field("name").to_pylist(), quantity.field("unit").to_pylist()
            )
        ]

    def _documents(self, run_key=None) -> list[dict]:
        if self._json:
            docs = [json.loads(row["document"]) for row in self._json_rows()]
            found = docs if run_key is None else [d for d in docs if d["provenance"]["run_key"] == run_key]
            logger.debug("documents(%s): %d of %d stored document(s)", run_key, len(found), len(docs))
            return found
        table = self._table(["provenance", "document"])
        if table is None:
            return []
        run_keys = table.column("provenance").combine_chunks().field("run_key").to_pylist()
        found = [
            json.loads(text)
            for r, text in zip(run_keys, table.column("document").to_pylist())
            if run_key is None or r == run_key
        ]
        logger.debug("documents(%s): %d of %d stored document(s)", run_key, len(found), len(run_keys))
        return found

    def documents(self, run_key: str) -> list[dict]:
        """The stored documents of one run, exactly as ingested (canonical form)."""
        return self._documents(run_key)

    def order(self, ref: str) -> dict | None:
        if self._json:
            for row in self._json_rows():
                if row.get("work_order_ref") == ref and row.get("work_order") is not None:
                    return json.loads(row["work_order"])
            return None
        table = self._table(["work_order_ref", "work_order"])
        if table is None:
            return None
        for r, text in zip(table.column("work_order_ref").to_pylist(), table.column("work_order").to_pylist()):
            if r == ref and text is not None:
                return json.loads(text)
        return None

    def head(
        self, n: int = 10, project: str | None = None, workload: str | None = None, measurands: bool = False
    ) -> tuple[int, list[dict]]:
        """The store's size and its latest n results, newest first. Rows are in
        ingest order, since every ingest appends to the rewritten file. `project`
        keeps one store project's results; `workload` keeps those whose benchmark
        name contains it; `measurands` leaves out condition measurements (schema
        §4.4). All are applied before n."""
        if self._json:
            all_rows = self._json_rows()
            if not all_rows:
                return 0, []
            parsed = [(i, row, json.loads(row["document"])) for i, row in enumerate(all_rows)]
            kept = [
                (i, row, doc)
                for i, row, doc in parsed
                if (project is None or row["store_project"] == project)
                and (workload is None or workload in doc["coordinates"]["workload"]["name"])
                and not (measurands and doc["measurement"].get("kind", "measurand") == "condition")
            ]
            kept = kept[max(0, len(kept) - n) :]
            if not kept:
                return len(all_rows), []
            rows = []
            for position, row, doc in kept:
                raw_obs = doc["measurement"].get("observations") or []
                values = [o["value"] if isinstance(o, dict) else o for o in raw_obs]
                labels_val = doc["provenance"].get("labels")
                rows.append(
                    {
                        "row": position,
                        "started_at": datetime.fromisoformat(doc["provenance"]["started_at"]),
                        "run_key": doc["provenance"]["run_key"],
                        "project": row["store_project"],
                        "workload": doc["coordinates"]["workload"]["name"],
                        "quantity": doc["measurement"]["quantity"]["name"],
                        "unit": doc["measurement"]["quantity"]["unit"],
                        "status": doc["measurement"]["status"],
                        "median": statistics.median(values) if values else None,
                        "revision": doc["revision"]["key"],
                        "labels": core.canonical(labels_val).decode() if labels_val is not None else None,
                        "document": row["document"],
                    }
                )
            return len(all_rows), rows[::-1]
        table = self._table(
            ["ingest_key", "revision", "coordinates", "measurement", "provenance", "store_project", "document"]
        )
        if table is None:
            return 0, []
        projects = table.column("store_project").to_pylist()
        names = table.column("coordinates").combine_chunks().field("workload").field("name").to_pylist()
        kinds = table.column("measurement").combine_chunks().field("kind").to_pylist()
        kept = [
            i
            for i, (pr, name, kind) in enumerate(zip(projects, names, kinds))
            if (project is None or pr == project)
            and (workload is None or workload in name)
            and not (measurands and kind == "condition")
        ]
        kept = kept[max(0, len(kept) - n) :]
        if not kept:
            return table.num_rows, []
        rows = []
        for position, row in zip(kept, table.take(kept).to_pylist()):
            values = [o["value"] for o in row["measurement"]["observations"] or []]
            rows.append(
                {
                    "row": position,
                    "started_at": row["provenance"]["started_at"],
                    "run_key": row["provenance"]["run_key"],
                    "project": row["store_project"],
                    "workload": row["coordinates"]["workload"]["name"],
                    "quantity": row["measurement"]["quantity"]["name"],
                    "unit": row["measurement"]["quantity"]["unit"],
                    "status": row["measurement"]["status"],
                    "median": statistics.median(values) if values else None,
                    "revision": row["revision"]["key"],
                    "labels": row["provenance"]["labels"],
                    "document": row["document"],
                }
            )
        return table.num_rows, rows[::-1]

    def entities(self, projection: Projection = identity.POLICY) -> dict[str, list[dict]]:
        """The core tables of schema §4.1, derived from the rows; series and
        points under `projection`, the identity policy unless another is asked
        for (§4.3). Nothing is kept: every call materializes them again."""
        found = {}
        for doc in self._documents():
            _add_entities(found, doc, store_project(doc), projection)
        return {name: list(rows.values()) for name, rows in found.items()}

    def series(self, workload=None, quantity=None, projection: Projection = identity.POLICY) -> list[dict]:
        rows = self.entities(projection).get("series", [])
        return [
            s
            for s in rows
            if (workload is None or workload in s["workload"]) and (quantity is None or s["quantity"] == quantity)
        ]

    def history(self, fingerprint_prefix: str, projection: Projection = identity.POLICY) -> list[dict]:
        """A series' points in axis order, each with the ignored facts that
        changed since the point before it (§4.3). On the revision axis they
        are listed by measurement start instead: the implementation has no revision
        graph (schema §4.1)."""
        points = [
            p
            for p in self.entities(projection).get("series_point", [])
            if p["series_fingerprint"].startswith(fingerprint_prefix)
        ]
        points.sort(key=lambda p: (projection.order_key(tuple(p["axis"])), p["started_at"], p["ingest_key"]))
        previous = {}
        for point in points:
            ignored = point.pop("ignored")
            if previous.get(point["series_fingerprint"]) is not None:
                before = previous[point["series_fingerprint"]]
                point["changed"] = sorted(k for k in set(before) | set(ignored) if before.get(k) != ignored.get(k))
            else:
                point["changed"] = []
            previous[point["series_fingerprint"]] = ignored
        return points

    # -- ingest --------------------------------------------------------------

    def ingest_paths(self, paths) -> dict:
        """Sweep files and directories of JSON documents in transit: work orders
        are attached to the results that reference them, results ingested."""
        documents, orders, unreadable = sweep_paths(paths)
        outcome = self.ingest(documents, orders)
        outcome["rejected"] = unreadable + outcome["rejected"]
        return outcome

    def ingest(self, documents: list[tuple[str, str]], orders: dict | None = None) -> dict:
        """Per-result outcomes; a batch is N independent documents (§5.4)."""
        index = self._index()
        existing = {(r["producer"], r["ingest_key"]): r["payload_sha256"] for r in index}
        attempts = {r["attempt_key"]: r["attempt_signature"] for r in index}
        units = {(r["store_project"], r["quantity"]): r["unit"] for r in index}
        accepted, derived, outcome = ingest_documents(
            documents, orders, existing=existing, attempts=attempts, units=units, store_path=self.path
        )
        if accepted:
            if self._json:
                self._write_json(derived)
            else:
                try:
                    table = self._parquet.results_table(accepted, derived)
                except Exception:  # one list comprehension over the batch: an error there writes nothing
                    logger.error(
                        "cannot convert %d result(s) to rows for %s: nothing is written", len(accepted), self.path
                    )
                    raise
                self._write_parquet(table)
        return outcome

    def _write_json(self, new_rows: list[dict]) -> None:
        """Rewrite the JSONL store with the new rows appended, atomically."""
        started = time.monotonic()
        existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        old_count = sum(1 for line in existing.splitlines() if line.strip())
        if existing and not existing.endswith("\n"):
            existing += "\n"
        appended = "".join(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n" for row in new_rows)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(existing)
                f.write(appended)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:  # disk full, permissions: the results are still in their files
            logger.error(
                "cannot write %s (%d row(s) were to be added): %s; a partial %s may be left behind",
                self.path,
                len(new_rows),
                e.strerror or e,
                tmp,
            )
            raise
        logger.debug(
            "wrote %s: %d row(s) added to %d, %.2f s", self.path, len(new_rows), old_count, time.monotonic() - started
        )

    def _write_parquet(self, new) -> None:
        """Rewrite the Parquet store with the new rows appended, atomically."""
        started = time.monotonic()
        old = self._table()
        # Rows written under an older message schema are carried over, with
        # nulls in any column or nested field the schema has since gained.
        table = new if old is None else self._pa.concat_tables([old, new], promote_options="permissive")
        if old is not None and logger.isEnabledFor(logging.DEBUG):
            before, after = _fields(old.schema, self._pa), _fields(table.schema, self._pa)
            added = sorted(set(after) - set(before))
            widened = sorted(
                f"{path}: {before[path]} to {after[path]}" for path in before if before[path] != after[path]
            )
            if added or widened:  # permissive promotion adds nested fields and widens types, not only columns
                logger.debug(
                    "%s: the schema was promoted: added %s; widened %s",
                    self.path,
                    ", ".join(added) or "nothing",
                    ", ".join(widened) or "nothing",
                )
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._pq.write_table(table.replace_schema_metadata(new.schema.metadata), tmp, compression="zstd")
            with open(tmp, "rb") as f:
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:  # disk full, permissions: the results are still in their files
            logger.error(
                "cannot write %s (%d row(s) were to be added): %s; a partial %s may be left behind",
                self.path,
                new.num_rows,
                e.strerror or e,
                tmp,
            )
            raise
        logger.debug(
            "wrote %s: %d row(s) added to %d, %.2f s",
            self.path,
            new.num_rows,
            old.num_rows if old else 0,
            time.monotonic() - started,
        )


def _fields(schema, pa) -> dict[str, str]:
    """Every leaf of a schema by dotted path, with its type: what a schema promotion can change."""
    found = {}

    def walk(prefix, kind):
        if pa.types.is_struct(kind):
            for child in kind:
                walk(f"{prefix}.{child.name}", child.type)
        else:
            found[prefix] = str(kind)

    for field in schema:
        walk(field.name, field.type)
    return found


_ENTITY_KEYS = {
    "project": lambda r: r["key"],
    "source": lambda r: (r["project"], r["uri"]),
    "workload_variant": lambda r: (r["project"], r["name"], r["parameters"]),
    "quantity": lambda r: (r["project"], r["name"]),
    "environment": lambda r: (r["identity_schema"], r["fingerprint"]),
    "revision": lambda r: (r["source"], r["key"]),
    "series": lambda r: r["fingerprint"],
    "series_point": lambda r: (r["series_fingerprint"], r["producer"], r["ingest_key"]),
}


def _add_entities(entities: dict, doc: dict, project: str, projection: Projection) -> None:
    def put(name, row):
        entities.setdefault(name, {}).setdefault(_ENTITY_KEYS[name](row), row)

    coordinates = doc["coordinates"]
    env = coordinates["environment"]
    parameters = core.canonical(coordinates["workload"].get("parameters", {})).decode()
    put("project", {"key": project})
    put("source", {"project": project, "uri": doc["source"]["uri"]})
    put("workload_variant", {"project": project, "name": coordinates["workload"]["name"], "parameters": parameters})
    quantity = doc["measurement"]["quantity"]
    put(
        "quantity",
        {
            "project": project,
            "name": quantity["name"],
            "unit": quantity["unit"],
            "direction": quantity.get("direction"),
            "deterministic": quantity.get("deterministic"),
        },
    )
    env_identity = {"schema_version": env["schema_version"], "identity": env["identity"]}
    put(
        "environment",
        {
            "identity_schema": env["schema_version"],
            "fingerprint": core.sha256_hex(core.canonical(env_identity)),
            "identity": core.canonical(env["identity"]).decode(),
        },
    )
    put("revision", {"source": doc["source"]["uri"], "key": doc["revision"]["key"]})
    for point in identity.points(doc, project, projection):
        put(
            "series",
            {
                "fingerprint": point["series_fingerprint"],
                "identity_schema": projection.identity_schema,
                "project": project,
                "workload": coordinates["workload"]["name"],
                "parameters": parameters,
                "quantity": quantity["name"],
                "unit": quantity["unit"],
                "estimator": core.canonical(point["estimator"]).decode(),
                "comparison_identity": core.canonical(point["identity"]).decode(),
            },
        )
        put(
            "series_point",
            {
                "series_fingerprint": point["series_fingerprint"],
                "producer": doc["producer"]["name"],
                "ingest_key": doc["ingest_key"],
                "attempt_key": doc["attempt_key"],
                "revision": doc["revision"]["key"],
                "axis": list(point["axis"]),
                "value": point["value"],
                "source": point["source"],
                "started_at": doc["provenance"]["started_at"],
                "ignored": point["ignored"],
                "warnings": warning_codes(doc),
            },
        )
