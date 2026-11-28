"""Arrow-free result helpers and in-memory ingestion (schema §4.1, §5.3, §5.4).

Shared between the Parquet-backed `Store` (`benchx.store`) and the JSON-only
paths (`bx bench --no-ingest`, `bx compare --results`) so that running and
comparing JSON results never imports PyArrow while enforcing the same
validation, deduplication, and consistency checks.
"""

import json
import logging
from pathlib import Path

from . import core, identity
from .core import loader
from .core.errors import (
    AttemptConflict,
    DocumentError,
    IdempotencyConflict,
    ReadError,
    UnitConflict,
)

logger = logging.getLogger(__name__)
MAX_LOGGED = 40  # the log's cap on a list, like its other lists

# Schema §5.3 attempt integrity: what results sharing an attempt_key agree on.
_ATTEMPT_FIELDS = ("project", "source", "revision", "benchmark")
_ATTEMPT_COORDINATES = ("workload", "subject", "environment")
_ATTEMPT_PROVENANCE = (
    "run_key",
    "subject_dirty",
    "subject_tree",
    "benchmark_dirty",
    "benchmark_tree",
)


def attempt_signature(doc: dict) -> str:
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


def warning_codes(document: dict) -> list[str]:
    """A result's quality warnings by code: a warning is a token, or an object that has a `code`."""
    return [
        w.get("code", str(w)) if isinstance(w, dict) else str(w)
        for w in document.get("quality", {}).get("warnings", [])
    ]


def rejection(name: str, error: DocumentError) -> dict:
    where = f"at {error.location}: " if error.location else ""
    return {"document": name, "code": str(error.code), "message": where + error.message}


def sweep_paths(paths) -> tuple[list[tuple[str, str]], dict[str, dict], list[dict]]:
    """Sweep files and directories of JSON documents in transit: work orders
    are separated from results, and unreadable files become rejections (§5.4)."""
    files = []
    for path in map(Path, paths):
        files.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
    orders, documents, unreadable = {}, [], []
    for f in files:
        # A file that cannot be read is one rejection, not the end of the sweep (§5.4).
        text = loader.read_text(f)
        if isinstance(text, ReadError):
            unreadable.append(rejection(f.name, text))
            continue
        if not f.name.startswith("workorder-"):
            documents.append((f.name, text))
            continue
        order, errors = loader.parse(text)
        if order is None:
            unreadable.append(rejection(f.name, errors[0]))
        else:
            orders[core.order_ref(order)] = order
    logger.debug(
        "swept %d file(s): %d result document(s), %d work order(s), %d unreadable or malformed",
        len(files),
        len(documents),
        len(orders),
        len(unreadable),
    )
    return documents, orders, unreadable


def ingest_documents(
    documents: list[tuple[str, str]],
    orders: dict | None = None,
    *,
    existing: dict[tuple[str, str], str] | None = None,
    attempts: dict[str, str] | None = None,
    units: dict[tuple[str, str], str] | None = None,
    store_path=None,
) -> tuple[list[dict], list[dict], dict]:
    """Validate and check per-result outcomes; a batch is N independent documents (§5.4).

    Returns `(accepted, derived, outcome)` and updates `existing`, `attempts`, and
    `units` in place when provided.
    """
    orders = orders or {}
    existing = {} if existing is None else existing
    attempts = {} if attempts is None else attempts
    units = {} if units is None else units

    accepted, derived = [], []
    outcome = {"ingested": 0, "duplicate": 0, "rejected": []}
    used_orders, without_order = set(), 0
    for name, text in documents:
        doc, errors = loader.parse(text)
        if doc is not None:
            errors = core.check(doc, "measurement-result")
        if errors:
            outcome["rejected"].append(rejection(name, errors[0]))
            logger.debug("%s: rejected with %d error(s)", name, len(errors))  # the rejection keeps the first
            for error in errors[:MAX_LOGGED]:
                logger.debug(
                    "%s: %s at %s: %s%s",
                    name,
                    error.code,
                    error.location or "the document",
                    error.message,
                    f" (rule {error.rule})" if getattr(error, "rule", None) else "",
                )
            if len(errors) > MAX_LOGGED:
                logger.debug("%s: and %d more error(s)", name, len(errors) - MAX_LOGGED)
            continue

        project = store_project(doc)
        canonical = core.canonical(doc)
        payload = core.sha256_hex(canonical)
        key = (doc["producer"]["name"], doc["ingest_key"])
        quantity = doc["measurement"]["quantity"]
        known = units.get((project, quantity["name"]))
        signature = attempt_signature(doc)
        if key in existing and existing[key] == payload:
            outcome["duplicate"] += 1
            continue
        if key in existing:
            conflict = IdempotencyConflict()
        elif known and known != quantity["unit"]:
            conflict = UnitConflict(
                quantity=quantity["name"],
                project=project,
                known=known,
                reported=quantity["unit"],
            )
        elif attempts.get(doc["attempt_key"], signature) != signature:
            conflict = AttemptConflict(attempt_key=doc["attempt_key"])
        else:
            conflict = None
        if conflict is not None:
            outcome["rejected"].append(rejection(name, conflict))
            if isinstance(conflict, IdempotencyConflict):  # the error does not carry what makes it one
                where = store_path if store_path is not None else "the store"
                logger.warning(
                    "%s: idempotency-conflict: %s already holds payload %s for %s, and this one is %s",
                    name,
                    where,
                    existing[key][:16],
                    doc["ingest_key"],
                    payload[:16],
                )
            else:
                logger.warning("%s: %s: %s", name, conflict.code, conflict.message)
            continue

        existing[key] = payload
        attempts[doc["attempt_key"]] = signature
        if not known:
            logger.debug(
                "%s: a new quantity %s [%s] in %s: the first result defines its unit",
                name,
                quantity["name"],
                quantity["unit"],
                project,
            )
        units[(project, quantity["name"])] = quantity["unit"]  # first sight defines the unit
        ref = doc["provenance"].get("info", {}).get("workorder_ref")
        order = orders.get(ref)
        if order is None:
            without_order += 1
        else:
            used_orders.add(ref)
        accepted.append(doc)
        derived.append(
            {
                "store_project": project,
                "reported_coordinates_fingerprint": identity.reported_coordinates_fingerprint(doc),
                "payload_sha256": payload,
                "attempt_signature": signature,
                "document": canonical.decode(),
                "work_order_ref": ref,
                "work_order": core.canonical(order).decode() if order is not None else None,
            }
        )
        outcome["ingested"] += 1

    unused = [ref for ref in orders if ref not in used_orders]
    logger.debug(
        "ingest: %d ingested, %d duplicate, %d rejected; %d work order(s) attached, %d in the sweep "
        "that no result references (neither stored nor reported), %d result(s) with no order in the sweep "
        "(an order delivered by an earlier ingest is not attached)",
        outcome["ingested"],
        outcome["duplicate"],
        len(outcome["rejected"]),
        len(used_orders),
        len(unused),
        without_order,
    )
    return accepted, derived, outcome


class MemoryStore:
    """In-memory result store enforcing the same ingest contract as `Store`,
    without requiring PyArrow or writing Parquet files."""

    def __init__(self) -> None:
        self._existing: dict[tuple[str, str], str] = {}
        self._attempts: dict[str, str] = {}
        self._units: dict[tuple[str, str], str] = {}
        self._documents: list[dict] = []
        self._orders: dict[str, dict] = {}

    def documents(self, run_key: str) -> list[dict]:
        """The accepted documents of one run, in canonical form."""
        return [d for d in self._documents if d["provenance"]["run_key"] == run_key]

    def order(self, ref: str) -> dict | None:
        return self._orders.get(ref)

    def ingest_paths(self, paths) -> dict:
        """Sweep files and directories of JSON documents in transit."""
        documents, orders, unreadable = sweep_paths(paths)
        outcome = self.ingest(documents, orders)
        outcome["rejected"] = unreadable + outcome["rejected"]
        return outcome

    def ingest(self, documents: list[tuple[str, str]], orders: dict | None = None) -> dict:
        """Per-result outcomes; a batch is N independent documents (§5.4)."""
        _, derived, outcome = ingest_documents(
            documents,
            orders,
            existing=self._existing,
            attempts=self._attempts,
            units=self._units,
        )
        for row in derived:
            self._documents.append(json.loads(row["document"]))
            if row["work_order_ref"] is not None and row["work_order"] is not None:
                self._orders.setdefault(row["work_order_ref"], json.loads(row["work_order"]))
        return outcome
