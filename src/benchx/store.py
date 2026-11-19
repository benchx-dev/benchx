"""The local store: one Parquet file (prototype-design.md §4).

One row per result: the message columns derived from the result schema
(measurement-result-parquet.md), then the columns the store derives,
including the canonical document itself and the work order it references.
Entities, series, and series points are computed from the rows when asked
for; nothing else is stored. JSON is for documents in transit, never for
the store.

A Parquet file cannot be appended to, so every ingest that accepts something
rewrites the file: to a temporary file beside it, synced, then renamed over
it, which is atomic. The acknowledgment therefore means validated and durable
(schema §5.4). Uniqueness checks are scans, which is fine at this scale.
"""

import json
import logging
import os
import statistics
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from . import core, identity, parquet
from .core import loader
from .core.errors import AttemptConflict, DocumentError, IdempotencyConflict, ReadError, UnitConflict
from .projection import Projection

logger = logging.getLogger(__name__)
MAX_LOGGED = 40  # the log's cap on a list, like its other lists

# Schema §5.3 attempt integrity: what results sharing an attempt_key agree on.
_ATTEMPT_FIELDS = ("project", "source", "revision", "benchmark")
_ATTEMPT_COORDINATES = ("workload", "subject", "environment")
_ATTEMPT_PROVENANCE = ("run_key", "subject_dirty", "subject_tree", "benchmark_dirty", "benchmark_tree")


def _attempt_signature(doc: dict) -> str:
    facts = {k: doc.get(k) for k in _ATTEMPT_FIELDS}
    facts.update({k: doc["coordinates"][k] for k in _ATTEMPT_COORDINATES})
    facts["environment"] = {k: facts["environment"][k] for k in ("schema_version", "identity")}
    facts.update({k: doc["provenance"].get(k) for k in _ATTEMPT_PROVENANCE})
    return core.sha256_hex(core.canonical(facts))


def store_project(doc: dict) -> str:
    """The project, or local/<hostname> for a thin result (schema §4.1)."""
    if "project" in doc:
        return doc["project"]
    ident = doc["coordinates"]["environment"]["identity"]
    return f"local/{ident.get('runner') or ident.get('hostname') or 'unknown'}"


class Store:
    """The local store, one Parquet file (prototype-design.md §4). Its place is the
    project's: `Config.store_path`, by default `store.parquet` in the project root."""

    def __init__(self, path):
        self.path = Path(path)

    # -- reading -------------------------------------------------------------

    def _table(self, columns=None) -> pa.Table | None:
        if not self.path.exists():
            logger.debug("%s: absent, read as an empty store", self.path)
            return None
        started = time.monotonic()
        table = pq.read_table(self.path, columns=columns)
        logger.debug("%s: read %d row(s), %s, in %.2f s", self.path, table.num_rows,
                     ", ".join(columns) if columns else "all columns", time.monotonic() - started)
        return table

    def _index(self) -> list[dict]:
        table = self._table(["producer", "ingest_key", "attempt_key", "payload_sha256", "attempt_signature",
                             "store_project", "measurement"])
        if table is None:
            return []
        producers = table.column("producer").combine_chunks().field("name").to_pylist()
        quantity = table.column("measurement").combine_chunks().field("quantity")
        rest = table.select(["ingest_key", "attempt_key", "payload_sha256", "attempt_signature",
                             "store_project"]).to_pylist()
        return [dict(row, producer=p, quantity=q, unit=u) for row, p, q, u in
                zip(rest, producers, quantity.field("name").to_pylist(), quantity.field("unit").to_pylist())]

    def _documents(self, run_key=None) -> list[dict]:
        table = self._table(["provenance", "document"])
        if table is None:
            return []
        run_keys = table.column("provenance").combine_chunks().field("run_key").to_pylist()
        found = [json.loads(text) for r, text in zip(run_keys, table.column("document").to_pylist())
                 if run_key is None or r == run_key]
        logger.debug("documents(%s): %d of %d stored document(s)", run_key, len(found), len(run_keys))
        return found

    def documents(self, run_key: str) -> list[dict]:
        """The stored documents of one run, exactly as ingested (canonical form)."""
        return self._documents(run_key)

    def order(self, ref: str) -> dict | None:
        table = self._table(["work_order_ref", "work_order"])
        if table is None:
            return None
        for r, text in zip(table.column("work_order_ref").to_pylist(), table.column("work_order").to_pylist()):
            if r == ref and text is not None:
                return json.loads(text)
        return None

    def head(self, n: int = 10, project: str | None = None, workload: str | None = None,
             measurands: bool = False) -> tuple[int, list[dict]]:
        """The store's size and its latest n results, newest first. Rows are in
        ingest order, since every ingest appends to the rewritten file. `project`
        keeps one store project's results; `workload` keeps those whose benchmark
        name contains it; `measurands` leaves out condition measurements (schema
        §4.4). All are applied before n."""
        table = self._table(["ingest_key", "revision", "coordinates", "measurement", "provenance",
                             "store_project", "document"])
        if table is None:
            return 0, []
        projects = table.column("store_project").to_pylist()
        names = table.column("coordinates").combine_chunks().field("workload").field("name").to_pylist()
        kinds = table.column("measurement").combine_chunks().field("kind").to_pylist()
        kept = [i for i, (pr, name, kind) in enumerate(zip(projects, names, kinds))
                if (project is None or pr == project) and (workload is None or workload in name)
                and not (measurands and kind == "condition")]
        kept = kept[max(0, len(kept) - n):]
        if not kept:
            return table.num_rows, []
        rows = []
        for position, row in zip(kept, table.take(kept).to_pylist()):
            values = [o["value"] for o in row["measurement"]["observations"] or []]
            rows.append({
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
            })
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
        return [s for s in rows if (workload is None or workload in s["workload"])
                and (quantity is None or s["quantity"] == quantity)]

    def history(self, fingerprint_prefix: str, projection: Projection = identity.POLICY) -> list[dict]:
        """A series' points in axis order, each with the ignored facts that
        changed since the point before it (§4.3). On the revision axis they
        are listed by measurement start instead: the implementation has no revision
        graph (schema §4.1)."""
        points = [p for p in self.entities(projection).get("series_point", [])
                  if p["series_fingerprint"].startswith(fingerprint_prefix)]
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
        files = []
        for path in map(Path, paths):
            files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
        orders, documents, unreadable = {}, [], []
        for f in files:
            # A file that cannot be read is one rejection, not the end of the sweep (§5.4).
            text = loader.read_text(f)
            if isinstance(text, ReadError):
                unreadable.append(_rejection(f.name, text))
                continue
            if not f.name.startswith("workorder-"):
                documents.append((f.name, text))
                continue
            order, errors = loader.parse(text)
            if order is None:
                unreadable.append(_rejection(f.name, errors[0]))
            else:
                orders[core.order_ref(order)] = order
        logger.debug("swept %d file(s): %d result document(s), %d work order(s), %d unreadable or malformed", len(files),
                     len(documents), len(orders), len(unreadable))
        outcome = self.ingest(documents, orders)
        outcome["rejected"] = unreadable + outcome["rejected"]
        return outcome

    def ingest(self, documents: list[tuple[str, str]], orders: dict | None = None) -> dict:
        """Per-result outcomes; a batch is N independent documents (§5.4)."""
        orders = orders or {}
        index = self._index()
        existing = {(r["producer"], r["ingest_key"]): r["payload_sha256"] for r in index}
        attempts = {r["attempt_key"]: r["attempt_signature"] for r in index}
        units = {(r["store_project"], r["quantity"]): r["unit"] for r in index}

        accepted, derived = [], []
        outcome = {"ingested": 0, "duplicate": 0, "rejected": []}
        used_orders, without_order = set(), 0
        for name, text in documents:
            doc, errors = loader.parse(text)
            if doc is not None:
                errors = core.check(doc, "measurement-result")
            if errors:
                outcome["rejected"].append(_rejection(name, errors[0]))
                logger.debug("%s: rejected with %d error(s)", name, len(errors))  # the rejection keeps the first
                for error in errors[:MAX_LOGGED]:
                    logger.debug("%s: %s at %s: %s%s", name, error.code, error.location or "the document", error.message,
                                 f" (rule {error.rule})" if getattr(error, "rule", None) else "")
                if len(errors) > MAX_LOGGED:
                    logger.debug("%s: and %d more error(s)", name, len(errors) - MAX_LOGGED)
                continue

            project = store_project(doc)
            canonical = core.canonical(doc)
            payload = core.sha256_hex(canonical)
            key = (doc["producer"]["name"], doc["ingest_key"])
            quantity = doc["measurement"]["quantity"]
            known = units.get((project, quantity["name"]))
            signature = _attempt_signature(doc)
            if key in existing and existing[key] == payload:
                outcome["duplicate"] += 1
                continue
            if key in existing:
                conflict = IdempotencyConflict()
            elif known and known != quantity["unit"]:
                conflict = UnitConflict(quantity=quantity["name"], project=project,
                                        known=known, reported=quantity["unit"])
            elif attempts.setdefault(doc["attempt_key"], signature) != signature:
                conflict = AttemptConflict(attempt_key=doc["attempt_key"])
            else:
                conflict = None
            if conflict is not None:
                outcome["rejected"].append(_rejection(name, conflict))
                if isinstance(conflict, IdempotencyConflict):  # the error does not carry what makes it one
                    logger.warning("%s: idempotency-conflict: %s already holds payload %s for %s, and this one is %s",
                                   name, self.path, existing[key][:16], doc["ingest_key"], payload[:16])
                else:
                    logger.warning("%s: %s: %s", name, conflict.code, conflict.message)
                continue

            existing[key] = payload
            if not known:
                logger.debug("%s: a new quantity %s [%s] in %s: the first result defines its unit", name,
                             quantity["name"], quantity["unit"], project)
            units[(project, quantity["name"])] = quantity["unit"]  # first sight defines the unit
            ref = doc["provenance"].get("info", {}).get("workorder_ref")
            order = orders.get(ref)
            if order is None:
                without_order += 1
            else:
                used_orders.add(ref)
            accepted.append(doc)
            derived.append({
                "store_project": project,
                "reported_coordinates_fingerprint": identity.reported_coordinates_fingerprint(doc),
                "payload_sha256": payload,
                "attempt_signature": signature,
                "document": canonical.decode(),
                "work_order_ref": ref,
                "work_order": core.canonical(order).decode() if order is not None else None,
            })
            outcome["ingested"] += 1

        unused = [ref for ref in orders if ref not in used_orders]
        logger.debug("ingest: %d ingested, %d duplicate, %d rejected; %d work order(s) attached, %d in the sweep "
                     "that no result references (neither stored nor reported), %d result(s) with no order in the sweep "
                     "(an order delivered by an earlier ingest is not attached)", outcome["ingested"],
                     outcome["duplicate"], len(outcome["rejected"]), len(used_orders), len(unused), without_order)
        if accepted:
            self._write(parquet.results_table(accepted, derived))
        return outcome

    def _write(self, new: pa.Table) -> None:
        """Rewrite the file with the new rows appended, atomically."""
        started = time.monotonic()
        old = self._table()
        # Rows written under an older message schema are carried over, with
        # nulls in any column or nested field the schema has since gained.
        table = new if old is None else pa.concat_tables([old, new], promote_options="permissive")
        if old is not None and logger.isEnabledFor(logging.DEBUG):
            before, after = _fields(old.schema), _fields(table.schema)
            added = sorted(set(after) - set(before))
            widened = sorted(f"{path}: {before[path]} to {after[path]}" for path in before if before[path] != after[path])
            if added or widened:  # permissive promotion adds nested fields and widens types, not only columns
                logger.debug("%s: the schema was promoted: added %s; widened %s", self.path, ", ".join(added) or "nothing",
                             ", ".join(widened) or "nothing")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        pq.write_table(table.replace_schema_metadata(new.schema.metadata), tmp, compression="zstd")
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        logger.debug("wrote %s: %d row(s) added to %d, %.2f s", self.path, new.num_rows, old.num_rows if old else 0,
                     time.monotonic() - started)


def _fields(schema: pa.Schema) -> dict[str, str]:
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


def _rejection(name: str, error: DocumentError) -> dict:
    where = f"at {error.location}: " if error.location else ""
    return {"document": name, "code": str(error.code), "message": where + error.message}


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


def warning_codes(document: dict) -> list[str]:
    """A result's quality warnings by code: a warning is a token, or an object that has a `code`."""
    return [w.get("code", str(w)) if isinstance(w, dict) else str(w)
            for w in document.get("quality", {}).get("warnings", [])]


def _add_entities(entities: dict, doc: dict, project: str, projection: Projection) -> None:
    def put(name, row):
        entities.setdefault(name, {}).setdefault(_ENTITY_KEYS[name](row), row)

    coordinates = doc["coordinates"]
    env = coordinates["environment"]
    parameters = core.canonical(coordinates["workload"].get("parameters", {})).decode()
    put("project", {"key": project})
    put("source", {"project": project, "uri": doc["source"]["uri"]})
    put("workload_variant", {"project": project, "name": coordinates["workload"]["name"],
                             "parameters": parameters})
    quantity = doc["measurement"]["quantity"]
    put("quantity", {"project": project, "name": quantity["name"], "unit": quantity["unit"],
                     "direction": quantity.get("direction"), "deterministic": quantity.get("deterministic")})
    env_identity = {"schema_version": env["schema_version"], "identity": env["identity"]}
    put("environment", {"identity_schema": env["schema_version"],
                        "fingerprint": core.sha256_hex(core.canonical(env_identity)),
                        "identity": core.canonical(env["identity"]).decode()})
    put("revision", {"source": doc["source"]["uri"], "key": doc["revision"]["key"]})
    for point in identity.points(doc, project, projection):
        put("series", {"fingerprint": point["series_fingerprint"],
                       "identity_schema": projection.identity_schema, "project": project,
                       "workload": coordinates["workload"]["name"], "parameters": parameters,
                       "quantity": quantity["name"], "unit": quantity["unit"],
                       "estimator": core.canonical(point["estimator"]).decode(),
                       "comparison_identity": core.canonical(point["identity"]).decode()})
        put("series_point", {"series_fingerprint": point["series_fingerprint"],
                             "producer": doc["producer"]["name"], "ingest_key": doc["ingest_key"],
                             "attempt_key": doc["attempt_key"], "revision": doc["revision"]["key"],
                             "axis": list(point["axis"]), "value": point["value"], "source": point["source"],
                             "started_at": doc["provenance"]["started_at"], "ignored": point["ignored"],
                             "warnings": warning_codes(doc)})
