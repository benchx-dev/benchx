"""The fixed middle of the comparator: Evidence and Assessment (comparator.md §2.6, §4).

    Selector -> Evidence -> Method -> Assessment -> comparison document

Selectors and methods are modular and versioned; what passes between them is
defined here, so that neither knows about the other. Evidence is a flat table,
and the only structure the framework reads from it is what counting can show:
whether a key is crossed with the side or nested in it, and whether the slots
alternated. Statistics belong to methods.
"""

from dataclasses import dataclass, field
from typing import Any

from .core import canonical
from .key import Key

SIDE = "side"
# The columns of the evidence table, each read by a key. `side` is not among
# them: the selector assigns it.
COLUMNS = {
    "round": Key("/procedure/round"),
    "slot": Key("/procedure/slot"),
    "run_key": Key("/provenance/run_key"),
    "attempt_key": Key("/attempt_key"),
    "started_at": Key("/provenance/started_at"),
    "revision": Key("/revision/key"),
}


class CompareError(Exception):
    """The request itself cannot be answered, such as an unknown baseline."""


def result_ref(doc: dict) -> dict:
    return {"producer": doc["producer"]["name"], "ingest_key": doc["ingest_key"]}


@dataclass(frozen=True)
class Row:
    """One attempt's estimate. A row with an `exclusion` stays in the table,
    since it still says how the run was designed, but no method sees it."""
    result: dict                 # producer and ingest_key
    side: str                    # as the selector names it, such as baseline or target
    columns: dict                # column name -> value, None where the result has none
    estimate: float | None = None
    observations: tuple = ()
    exclusion: str | None = None


@dataclass(frozen=True)
class Evidence:
    """One unit: the rows of one series, and what the selector knows about them."""
    series: str
    workload: dict
    quantity: dict               # the measurement's quantity: name, unit, direction, ...
    sides: tuple                 # in order: the reference side first
    rows: tuple
    missing: tuple = ()          # sides with no result, which the selector expected
    unmatched_differences: tuple | None = None
    caveats: tuple = ()
    # History evidence (comparator.md §2.2): `target` and `reference` are result
    # references, the reference oldest first.
    target: tuple = ()
    reference: tuple = ()
    ordering: str | None = None
    boundaries: tuple = ()
    calibration: tuple = ()      # past comparisons in the series, for a method's threshold
    # Why no verdict may be given though the unit has evidence, such as side-failed
    # (UC-02 §10). A selector's rule, not a statistic: every method returns
    # indeterminate with this reason, and the framework checks that it did.
    no_verdict: str | None = None


@dataclass
class Selection:
    """What a selector hands the framework: the units, and the facts about the
    request that hold for all of them."""
    selector: dict               # name, version, params
    estimator: dict
    identity_schema: str
    local_only: bool
    inputs: list
    units: list = field(default_factory=list)
    sides: dict | None = None
    slots: str | None = None     # alternating, not-alternating, or absent, over the whole run
    failed: list = field(default_factory=list)         # selector invariants
    disagreements: list = field(default_factory=list)  # selector invariants found after the method's run-level ones
    advice: dict = field(default_factory=dict)  # invariant -> what the selector adds to a method requirement's detail
    stopped: bool = False        # a failure so basic that nothing else can be checked


@dataclass(frozen=True)
class Assessment:
    """What a method found. Deterministic given its inputs, method version, and configuration."""
    verdict: str
    reason: str | None = None
    effect: dict | None = None   # measure and value
    used: tuple = ()             # rows the verdict rests on
    excluded: tuple = ()         # (row, reason): rows the method read and set aside
    details: dict = field(default_factory=dict)


class Method:
    """A comparison method owns all the statistics (comparator.md §4.1). It says
    what shape of evidence it needs in `dims`, and what its statistics assume of
    the run in `requires`: alternating-slots, equal-attempts, duplicate-points."""
    name: str
    version: str
    dims: tuple
    requires: tuple = ()
    defaults: dict = {}
    config_schema: dict = {"type": "object"}
    details_schema: dict = {"type": "object"}

    @property
    def id(self) -> str:
        return f"{self.name}/{self.version}"

    def assess(self, layout: "Layout", evidence: Evidence, config: dict) -> Assessment:
        raise NotImplementedError

    def describe(self, effect: dict | None, details: dict) -> str:
        """One line of the reviewer's table for a unit this method assessed."""
        raise NotImplementedError


class Selector:
    """A selector forms the evidence (comparator.md §2.2, §2.3, §2.5)."""
    name: str
    version: str

    def select(self, documents, **params) -> Selection:
        raise NotImplementedError


# -- what counting shows -------------------------------------------------------

def _sides_by_value(rows, column) -> dict:
    found = {}
    for row in rows:
        value = row.columns.get(column)
        if value is not None:
            found.setdefault(canonical(value), set()).add(row.side)
    return found


def relation(rows, column) -> str:
    """How a column relates to the side. Excluded rows count: they show how the
    run was designed, which does not depend on which attempts succeeded."""
    found = _sides_by_value(rows, column)
    if not found:
        return "absent"
    return "crossed" if any(len(sides) > 1 for sides in found.values()) else "nested"


def slot_schedule(slots_and_sides) -> str:
    """Whether the sides alternate, from (slot, side) over a whole run. A slot
    is a position in the run's realized order, so a single unit cannot show it:
    a unit that skipped one attempt has gaps that say nothing about the schedule."""
    slots = {}
    for slot, side in slots_and_sides:
        if slot is None:
            return "absent"
        slots.setdefault(slot, set()).add(side)
    order = [slots[slot] for slot in sorted(slots)]
    if any(len(o) != 1 for o in order) or any(a == b for a, b in zip(order, order[1:])):
        return "not-alternating"
    return "alternating"


# -- layout --------------------------------------------------------------------

@dataclass(frozen=True)
class Layout:
    """A unit's usable rows over named dimensions. `cells` nests one list per
    dimension, in `dims` order, along `coords`. A leaf is the tuple of rows at
    those coordinates: empty means masked, and it holds several where a dimension
    does not tell rows apart, as `("side",)` does not."""
    dims: tuple
    coords: dict
    cells: Any

    def leaves(self):
        def walk(node, depth):
            if depth == len(self.dims):
                yield node
            else:
                for child in node:
                    yield from walk(child, depth + 1)
        return walk(self.cells, 0)


class NotApplicable(Exception):
    """A unit cannot be laid out over a method's dimensions; the message says why."""


def _value(row: Row, dim: str):
    return row.side if dim == SIDE else row.columns.get(dim)


def pivot(evidence: Evidence, dims: tuple) -> Layout:
    """`evidence` over `dims`, with missing cells masked. Every dimension except
    the side must be crossed with it, or there is nothing to line the sides up by."""
    rows = evidence.rows
    coords = {}
    for dim in dims:
        if dim == SIDE:
            coords[dim] = tuple(evidence.sides)
            continue
        found = relation(rows, dim)
        if found == "absent":
            raise NotApplicable(f"it needs {dim}, and no result has one")
        if found == "nested":
            raise NotApplicable(f"it needs {dim}, and no value of {dim} appears on more than one side")
        values = {canonical(_value(r, dim)): _value(r, dim) for r in rows if _value(r, dim) is not None}
        coords[dim] = tuple(sorted(values.values(), key=COLUMNS[dim].sort_key))

    def nest(rows, remaining):
        if not remaining:
            return tuple(rows)
        dim, *rest = remaining
        return [nest([r for r in rows if _value(r, dim) == value], rest) for value in coords[dim]]

    return Layout(tuple(dims), coords, nest([r for r in rows if r.exclusion is None], dims))


def change_label(signed: float, direction: str | None) -> str:
    """What a change of this sign is, by the quantity's direction when it has one."""
    if direction == "lower-is-better":
        return "regressed" if signed > 0 else "improved"
    if direction == "higher-is-better":
        return "improved" if signed > 0 else "regressed"
    return "changed"
