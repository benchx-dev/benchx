"""The local store: one Parquet file (prototype-design.md §4).

One row per result: the message columns #30 derives from the result schema,
then the columns the store derives, including the canonical document itself
and the work order it references. Entities, series, and series points are
computed from the rows when asked for; nothing else is stored. JSON is for
documents in transit, never for the store.

A Parquet file cannot be appended to, so every ingest that accepts something
rewrites the file: to a temporary file beside it, synced, then renamed over
it, which is atomic. The acknowledgment therefore means validated and durable
(schema §5.4). Uniqueness checks are scans, which is fine at prototype scale.
"""

import json
import os
import statistics
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from . import core, identity, parquet
from .core import loader
from .core.errors import AttemptConflict, DocumentError, IdempotencyConflict, UnitConflict

# Schema §5.3 attempt integrity: what results sharing an attempt_key agree on.
_ATTEMPT_FIELDS = ("project", "source", "revision", "benchmark")
_ATTEMPT_COORDINATES = ("workload", "subject", "environment")
_ATTEMPT_PROVENANCE = ("run_key", "subject_dirty", "subject_tree", "benchmark_dirty", "benchmark_tree")


def _attempt_signature(doc: dict) -> str:
    facts = {k: doc.get(k) for k in _ATTEMPT_FIELDS}
    facts.update({k: doc["coordinates"][k] for k in _ATTEMPT_COORDINATES})
    facts["environment"] = {k: facts["environment"][k] for k in ("schema", "identity")}
    facts.update({k: doc["provenance"].get(k) for k in _ATTEMPT_PROVENANCE})
    return core.sha256_hex(core.canonical(facts))


def store_project(doc: dict) -> str:
    """The project, or local/<hostname> for a thin result (schema §4.1)."""
    if "project" in doc:
        return doc["project"]
    ident = doc["coordinates"]["environment"]["identity"]
    return f"local/{ident.get('runner') or ident.get('hostname') or 'unknown'}"


def home() -> Path:
    """benchx's home: ~/.benchx, or $BENCHX_HOME to relocate it (tests, CI)."""
    return Path(os.environ.get("BENCHX_HOME") or Path.home() / ".benchx")


def default_path() -> Path:
    """The local store's one place (prototype-design.md §4)."""
    return home() / "store.parquet"


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_path()

    # -- reading -------------------------------------------------------------

    def _table(self, columns=None) -> pa.Table | None:
        return pq.read_table(self.path, columns=columns) if self.path.exists() else None

    def _index(self) -> list[dict]:
        table = self._table(["producer", "ingest_key", "attempt_key", "payload_sha256", "attempt_signature",
                             "store_project", "coordinates"])
        if table is None:
            return []
        producers = table.column("producer").combine_chunks().field("name").to_pylist()
        quantity = table.column("coordinates").combine_chunks().field("quantity")
        rest = table.select(["ingest_key", "attempt_key", "payload_sha256", "attempt_signature",
                             "store_project"]).to_pylist()
        return [dict(row, producer=p, quantity=q, unit=u) for row, p, q, u in
                zip(rest, producers, quantity.field("name").to_pylist(), quantity.field("unit").to_pylist())]

    def _documents(self, run_key=None) -> list[dict]:
        table = self._table(["provenance", "document"])
        if table is None:
            return []
        run_keys = table.column("provenance").combine_chunks().field("run_key").to_pylist()
        return [json.loads(text) for r, text in zip(run_keys, table.column("document").to_pylist())
                if run_key is None or r == run_key]

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

    def head(self, n: int = 10, project: str | None = None, workload: str | None = None) -> tuple[int, list[dict]]:
        """The store's size and its latest n results, newest first. Rows are in
        ingest order, since every ingest appends to the rewritten file. `project`
        keeps one store project's results; `workload` keeps those whose benchmark
        name contains it. Both are applied before n."""
        table = self._table(["ingest_key", "revision", "coordinates", "measurement", "provenance",
                             "store_project", "document"])
        if table is None:
            return 0, []
        projects = table.column("store_project").to_pylist()
        names = table.column("coordinates").combine_chunks().field("workload").field("name").to_pylist()
        kept = [i for i, (pr, name) in enumerate(zip(projects, names))
                if (project is None or pr == project) and (workload is None or workload in name)]
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
                "quantity": row["coordinates"]["quantity"]["name"],
                "unit": row["coordinates"]["quantity"]["unit"],
                "status": row["measurement"]["status"],
                "median": statistics.median(values) if values else None,
                "revision": row["revision"]["key"],
                "labels": row["provenance"]["labels"],
                "document": row["document"],
            })
        return table.num_rows, rows[::-1]

    def entities(self) -> dict[str, list[dict]]:
        """The core tables of schema §4.1, derived from the rows."""
        found = {}
        for doc in self._documents():
            _add_entities(found, doc, store_project(doc))
        return {name: list(rows.values()) for name, rows in found.items()}

    def series(self, workload=None, quantity=None) -> list[dict]:
        rows = self.entities().get("series", [])
        return [s for s in rows if (workload is None or workload in s["workload"])
                and (quantity is None or s["quantity"] == quantity)]

    def history(self, fingerprint_prefix: str) -> list[dict]:
        """A series' points. The prototype has no revision graph, so revision
        order is unavailable (schema §4.1) and points are listed by measurement
        start instead."""
        points = [p for p in self.entities().get("series_point", [])
                  if p["series_fingerprint"].startswith(fingerprint_prefix)]
        return sorted(points, key=lambda p: (p["started_at"], p["ingest_key"]))

    # -- ingest --------------------------------------------------------------

    def ingest_paths(self, paths) -> dict:
        """Sweep files and directories of JSON documents in transit: work orders
        are attached to the results that reference them, results ingested."""
        files = []
        for path in map(Path, paths):
            files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
        orders, documents, unreadable = {}, [], []
        for f in files:
            text = f.read_text(encoding="utf-8")
            if not f.name.startswith("workorder-"):
                documents.append((f.name, text))
                continue
            order, errors = loader.parse(text)
            if order is None:
                unreadable.append(_rejection(f.name, errors[0]))
            else:
                orders[core.order_ref(order)] = order
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
        for name, text in documents:
            doc, errors = loader.parse(text)
            if doc is not None:
                errors = core.check(doc, "measurement-result")
            if errors:
                outcome["rejected"].append(_rejection(name, errors[0]))
                continue

            project = store_project(doc)
            canonical = core.canonical(doc)
            payload = core.sha256_hex(canonical)
            key = (doc["producer"]["name"], doc["ingest_key"])
            quantity = doc["coordinates"]["quantity"]
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
                continue

            existing[key] = payload
            units[(project, quantity["name"])] = quantity["unit"]  # first sight defines the unit
            ref = doc["provenance"].get("info", {}).get("workorder_ref")
            order = orders.get(ref)
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

        if accepted:
            self._write(parquet.results_table(accepted, derived))
        return outcome

    def _write(self, new: pa.Table) -> None:
        """Rewrite the file with the new rows appended, atomically."""
        old = self._table()
        # Rows written under an older message schema are carried over, with
        # nulls in any column or nested field the schema has since gained.
        table = new if old is None else pa.concat_tables([old, new], promote_options="permissive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        pq.write_table(table.replace_schema_metadata(new.schema.metadata), tmp, compression="zstd")
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


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


def _add_entities(entities: dict, doc: dict, project: str) -> None:
    def put(name, row):
        entities.setdefault(name, {}).setdefault(_ENTITY_KEYS[name](row), row)

    coordinates = doc["coordinates"]
    env = coordinates["environment"]
    parameters = core.canonical(coordinates["workload"].get("parameters", {})).decode()
    put("project", {"key": project})
    put("source", {"project": project, "uri": doc["source"]["uri"]})
    put("workload_variant", {"project": project, "name": coordinates["workload"]["name"],
                             "parameters": parameters})
    quantity = coordinates["quantity"]
    put("quantity", {"project": project, "name": quantity["name"], "unit": quantity["unit"],
                     "direction": quantity.get("direction"), "deterministic": quantity.get("deterministic")})
    env_identity = {"schema": env["schema"], "identity": env["identity"]}
    put("environment", {"identity_schema": env["schema"],
                        "fingerprint": core.sha256_hex(core.canonical(env_identity)),
                        "identity": core.canonical(env["identity"]).decode()})
    put("revision", {"source": doc["source"]["uri"], "key": doc["revision"]["key"]})
    for point in identity.points(doc, project):
        put("series", {"fingerprint": point["series_fingerprint"],
                       "identity_schema": identity.IDENTITY_SCHEMA, "project": project,
                       "workload": coordinates["workload"]["name"], "parameters": parameters,
                       "quantity": quantity["name"], "unit": quantity["unit"],
                       "estimator": core.canonical(point["estimator"]).decode(),
                       "comparison_identity": core.canonical(identity.comparison_identity(doc, project)).decode()})
        put("series_point", {"series_fingerprint": point["series_fingerprint"],
                             "producer": doc["producer"]["name"], "ingest_key": doc["ingest_key"],
                             "attempt_key": doc["attempt_key"], "revision": doc["revision"]["key"],
                             "value": point["value"], "source": point["source"],
                             "started_at": doc["provenance"]["started_at"]})
