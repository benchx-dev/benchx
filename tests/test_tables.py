"""`bx compare --tables`: the asv-style tables (TABLES_PLAN.md), on hand-built results and through the CLI."""

import pytest

from benchx import cli, compare, tables
from conftest import cases, make_build
from test_comparator import BASE, HEAD, result

MEAN = "bench_a.Mean.time_mean_axis"
PLAIN = "bench_b.Plain.time_x"
WORKLOADS = ((MEAN, {"size": 1}, 5.2e-6), (MEAN, {"size": 10}, 5.6e-6), (PLAIN, None, 2.0e-3))


def make(ratios=None, rounds=5, **changes):
    """A `revisions` run of three cases over `rounds` rounds. The head takes `ratios[workload]` times
    the base's time (default 1.05); `changes` apply to the head's results."""
    ratios = ratios or {}
    docs, slot = [], 0
    for r in range(rounds):
        for side in (BASE, HEAD):
            for name, parameters, seconds in WORKLOADS:
                ratio = ratios.get(name, 1.05) if side == HEAD else 1.0
                d = result(
                    side, r, slot, seconds * (1 + 0.01 * r) * ratio, workload=name, **(changes if side == HEAD else {})
                )
                if parameters:
                    d["coordinates"]["workload"]["parameters"] = parameters
                d["ingest_key"] += f":{parameters}"
                docs.append(d)
            slot += 1
    return docs


def evaluate(docs):
    return compare.compare(docs, run_key="run", profile="revisions", baseline=BASE)


@pytest.mark.parametrize(
    "value, spread, unit, expected",
    [
        (5.2e-6, 8e-8, "s", "5.20±0.08μs"),
        (96.3e-6, 1e-7, "s", "96.3±0.1μs"),
        (97.6e-6, 2e-6, "s", "97.6±2μs"),
        (2.08e-3, 6e-5, "s", "2.08±0.06ms"),
        (1.5, None, "s", "1.50s"),
        (100e-6, None, "s", "100μs"),
        (10e-6, None, "s", "10.0μs"),
        (3e-9, 1e-10, "s", "3.00±0.1ns"),
        (5.2e-6, 4e-9, "s", "5.20±0μs"),  # a spread below 0.005 of the unit shows as 0, as in asv
        (90.0, 3.0, "s", "1.50±0.05m"),
        (0.0, None, "s", "0s"),
        (1500.0, 20.0, "B", "1500±20 B"),
        (None, None, "s", "n/a"),
    ],
)
def test_values_are_written_as_asv_writes_them(value, spread, unit, expected):
    assert tables.human(value, spread, unit) == expected


def test_a_revisions_run_has_a_table_per_commit_and_one_of_changes():
    """asv's layout (format_text_table, tabulate "github"), the grids indented by two spaces: every cell centered, a column
    its widest cell plus 2, lines not trimmed; Ratio and Noise right-aligned; the verdict repeated."""
    docs = make()
    assert tables.render(evaluate(docs), docs).split("\n") == [
        "",
        "For local/host commit aaaaaaaa (5 rounds):",
        "bench_a.Mean.time_mean_axis  ok",
        "  ====== =============",
        "   size               ",
        "  ------ -------------",
        "    1     5.30±0.08μs ",
        "    10    5.71±0.09μs ",
        "  ====== =============",
        "bench_b.Plain.time_x  2.04±0.03ms",
        "",
        "For local/host commit bbbbbbbb (5 rounds):",
        "bench_a.Mean.time_mean_axis  ok",
        "  ====== =============",
        "   size               ",
        "  ------ -------------",
        "    1     5.57±0.09μs ",
        "    10    6.00±0.09μs ",
        "  ====== =============",
        "bench_b.Plain.time_x  2.14±0.03ms",
        "",
        "| Change   | Before [aaaaaaaa]   | After [bbbbbbbb]   |   Ratio |   Noise | Verdict   | Benchmark (Parameter)           |",
        "|----------|---------------------|--------------------|---------|---------|-----------|---------------------------------|",
        "| +        | 5.30±0.08μs         | 5.57±0.09μs        |    1.05 |   0.00% | regressed | bench_a.Mean.time_mean_axis(1)  |",
        "| +        | 5.71±0.09μs         | 6.00±0.09μs        |    1.05 |   0.00% | regressed | bench_a.Mean.time_mean_axis(10) |",
        "| +        | 2.04±0.03ms         | 2.14±0.03ms        |    1.05 |   0.00% | regressed | bench_b.Plain.time_x            |",
    ]


def test_the_noise_of_a_row_is_the_comparators():
    """Rounds whose ratios alternate 1.05 and 1.06: the paired differences .05, .06, .05, .06, .05 have
    sd 0.005477, and the noise, their standard error, is sd / sqrt(5) = 0.2449%."""
    docs, slot = [], 0
    for r in range(5):
        for side, ratio in ((BASE, 1.0), (HEAD, 1.05 + 0.01 * (r % 2))):
            docs.append(result(side, r, slot, 2.0e-3 * ratio, workload=PLAIN))
            slot += 1
    doc = evaluate(docs)
    assert doc["units"][0]["details"]["noise"] == pytest.approx(0.0024494897, rel=1e-6)
    row = next(line for line in tables.render(doc, docs).splitlines() if line.startswith("| +"))
    assert "|   0.24% | regressed |" in row


def test_only_changed_rows_unless_all():
    """As asv continuous does: the unchanged benchmark is listed only with --all, with a blank mark."""
    docs = make({PLAIN: 1.0})
    doc = evaluate(docs)
    changes = tables.render(doc, docs).splitlines()
    assert [line for line in changes if line.startswith("| ")][1:] and not any(
        PLAIN in line for line in changes if line.startswith("| ")
    )
    everything = [line for line in tables.render(doc, docs, all_rows=True).splitlines() if line.startswith("| ")]
    assert len(everything) == 4  # the header and three rows
    assert (
        everything[-1].startswith("|          | 2.04±0.03ms") and "1.00" in everything[-1] and PLAIN in everything[-1]
    )
    assert "| no change detected |" in everything[-1] and "| regressed " in everything[1]


def test_a_run_without_changes_says_so():
    docs = make({MEAN: 1.0, PLAIN: 1.0})
    shown = tables.render(evaluate(docs), docs)
    assert shown.endswith("No benchmark changed (the verdict is benchx's; --all lists every row).")
    assert "| Change" not in shown
    assert "| Change" in tables.render(evaluate(docs), docs, all_rows=True)


def test_an_improvement_is_marked_with_a_minus():
    docs = make({MEAN: 0.8})
    rows = [line for line in tables.render(evaluate(docs), docs).splitlines() if line.startswith("| -")]
    assert len(rows) == 2 and all("0.80" in line and "| improved " in line for line in rows)


def test_a_side_that_failed_shows_its_status():
    docs = [d for d in make() if d["coordinates"]["workload"]["name"] != PLAIN or d["revision"]["key"] == BASE]
    docs += [result(HEAD, r, 2 * r + 1, 1.0, workload=PLAIN, measurement__status="error") for r in range(5)]
    for d in docs[-5:]:
        d["ingest_key"] += ":failed"
    doc = evaluate(docs)
    assert next(u for u in doc["units"] if u["workload"]["name"] == PLAIN)["verdict"] == "indeterminate"
    shown = tables.render(doc, docs, all_rows=True)
    assert "bench_b.Plain.time_x  error" in shown
    assert "| ?        | 2.04±0.03ms" in shown and "| error " in shown and "n/a" in shown
    assert "| indeterminate (" in shown


def test_a_benchmark_that_raises_on_one_side_leaves_the_others_compared():
    """The failing benchmark reports no harness version or runtime. It shows its status in the tables and is
    `indeterminate` in the change table; the other benchmarks have their rows."""
    docs = make()
    for d in docs:
        if d["revision"]["key"] == HEAD and d["coordinates"]["workload"]["name"] == PLAIN:
            d["measurement"] = {"quantity": d["measurement"]["quantity"], "status": "error", "reason": "harness.error"}
            d["coordinates"]["environment"]["system"]["runtime"] = {"python": "3.12"}
    doc = evaluate(docs)
    assert doc["failed_invariants"] == []
    shown = tables.render(doc, docs, all_rows=True)
    assert "bench_b.Plain.time_x  2.04±0.03ms" in shown and "bench_b.Plain.time_x  error" in shown
    rows = [line for line in shown.splitlines() if line.startswith("| ")]
    assert sum("| regressed " in line for line in rows) == 2
    assert any(
        "bench_b.Plain.time_x" in line
        and "| ?" in line
        and "error" in line
        and "n/a" in line
        and "indeterminate (not-measured)" in line
        for line in rows
    )


def test_an_edited_benchmark_is_marked_not_comparable_and_the_others_have_their_rows():
    """Its two sides ran different code, so it has no verdict: as `asv compare` does, the change table lists
    it with `x` by default, and the table of each side still shows what it measured."""
    docs = make()
    for d in docs:
        if d["coordinates"]["workload"]["name"] == MEAN:
            d["coordinates"]["protocol"]["asv"] = {"benchmark_version": "v2" if d["revision"]["key"] == HEAD else "v1"}
    doc = evaluate(docs)
    assert doc["failed_invariants"] == []
    shown = tables.render(doc, docs)
    rows = [line for line in shown.splitlines() if line.startswith("| ")]
    edited = [line for line in rows if MEAN in line]
    assert edited and all(line.startswith("| x ") and "not comparable (benchmark-changed)" in line for line in edited)
    assert any(PLAIN in line for line in rows)
    assert shown.count(f"{MEAN}  ok") == 2  # the grid of each side


def test_a_dirty_side_is_named_in_its_heading():
    docs = make(provenance__subject_dirty="dirty")
    shown = tables.render(evaluate(docs), docs)
    assert "For local/host commit bbbbbbbb [dirty tree] (5 rounds):" in shown
    assert "commit aaaaaaaa (5 rounds):" in shown


def test_a_comparison_without_units_has_no_tables():
    docs = [d for d in make() if d["revision"]["key"] == BASE]  # one side: the two-sides invariant fails
    doc = evaluate(docs)
    assert doc["failed_invariants"] and tables.render(doc, docs) == ""


@pytest.mark.parametrize(
    "profile, named, axis",
    [
        ("environments", {"label": "env"}, "/provenance/labels/env"),
        ("variants", {"parameter": "role"}, "/coordinates/workload/parameters/role"),
        ("revisions", {}, "/provenance/subject_tree"),
    ],
)
def test_the_projection_is_rebuilt_from_the_document(profile, named, axis):
    doc = {
        "selector": {
            "name": "benchx/run-profile",
            "version": "v1",
            "params": {"run_key": "r", "profile": profile, "baseline": "a", **named},
        }
    }
    assert tables._projection(doc).axis[0] == axis


# Through the CLI, on the fake Google Benchmark of the session tests.


@pytest.fixture
def builds(repo, tmp):
    base = make_build(tmp / "build-base", repo["wt-base"], cases())
    head = make_build(tmp / "build-head", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3))
    return {"baseline": f"{base}:{repo['wt-base']}", "contender": f"{head}:{repo['wt-head']}"}


def test_tables_after_a_measure_mode_comparison_and_the_same_in_read_mode(repo, tmp, builds, capsys):
    common = ["--profile", "revisions"]
    assert (
        cli.main(
            [
                "compare",
                builds["baseline"],
                builds["contender"],
                *common,
                "--adapter",
                "google-benchmark",
                "--suite",
                "demo-bench",
                "--rounds",
                "5",
                "--run-key",
                "t-1",
                "--out",
                str(tmp / "o"),
                "--tables",
            ]
        )
        == cli.EXIT_OK
    )
    measured = capsys.readouterr().out
    assert measured.index("REGRESSED") < measured.index(f"commit {repo['base'][:8]}") < measured.index("| Change")
    assert f"commit {repo['head'][:8]} (5 rounds):" in measured and "BM_Quiet/1024" in measured
    assert "| + " in measured and f"Before [{repo['base'][:8]}]" in measured
    assert cli.main(["compare", "--run", "t-1", *common, "--baseline", repo["base"][:10], "--tables"]) == cli.EXIT_OK
    assert capsys.readouterr().out == measured


def test_without_tables_the_output_is_what_it_was(repo, tmp, builds, capsys):
    cli.main(
        [
            "compare",
            builds["baseline"],
            builds["contender"],
            "--profile",
            "revisions",
            "--adapter",
            "google-benchmark",
            "--suite",
            "demo-bench",
            "--rounds",
            "3",
            "--run-key",
            "t-2",
            "--out",
            str(tmp / "o"),
        ]
    )
    assert "For " not in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--run", "k", "--profile", "revisions", "--baseline", "x", "--tables", "--json"], "--tables is a text view"),
        (["--run", "k", "--profile", "revisions", "--baseline", "x", "--all"], "--all goes with --tables"),
    ],
)
def test_tables_flags_that_do_not_fit_are_refused(argv, message, capsys):
    assert cli.main(["compare", *argv]) == cli.EXIT_REFUSED
    assert message in capsys.readouterr().err


def same_commit(dirty_sides):
    """The two sides are one commit (its key), told apart only by their trees: uncommitted changes
    against HEAD, say. `dirty_sides` are the sides with changes."""
    docs = make()
    for d in docs:
        d["revision"]["key"] = "c" * 40
        if d["provenance"]["subject_tree"] in dirty_sides:
            d["provenance"]["subject_dirty"] = "dirty"
    return docs


def test_uncommitted_changes_against_the_same_commit_are_told_apart_in_the_change_table():
    docs = same_commit({HEAD})
    shown = tables.render(evaluate(docs), docs)
    assert "Before [cccccccc]" in shown and "After [cccccccc (dirty)]" in shown
    assert "For local/host commit cccccccc (5 rounds):" in shown
    assert "For local/host commit cccccccc [dirty tree] (5 rounds):" in shown


def test_two_dirty_sides_of_one_commit_are_told_apart_by_their_trees():
    docs = same_commit({BASE, HEAD})
    shown = tables.render(evaluate(docs), docs)
    assert "Before [cccccccc tree aaaaaaaa (dirty)]" in shown and "After [cccccccc tree bbbbbbbb (dirty)]" in shown
    assert "For local/host commit cccccccc tree aaaaaaaa [dirty tree] (5 rounds):" in shown
    assert "For local/host commit cccccccc tree bbbbbbbb [dirty tree] (5 rounds):" in shown
