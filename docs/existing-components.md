# Changepoint detectors

## Apache Otava

### What it is

An [Apache Incubator](https://incubator.apache.org/clutch/otava.html) CLI tool (Python) that runs statistical change-point detection on performance test time-series to catch regressions.

**Data sources it ingests:** [CSV](https://otava.apache.org/docs/csv), JSON, [PostgreSQL](https://otava.apache.org/docs/postgresql) (via SQL query), [BigQuery](https://otava.apache.org/docs/big-query), and [Graphite](https://otava.apache.org/docs/graphite) — each mapped to a common shape: **time** + **metrics** + **attributes** (metadata like branch/commit, not analyzed statistically).

### Metrics — the core unit of analysis

- A metric is a single named numeric signal tracked over time (e.g. `throughput`, `response_time`, `cpu_usage`, `p99_latency_ms`).
- A test typically tracks **multiple metrics at once** — each declared separately (a list of CSV columns, an array of `{name, value}` pairs in JSON, a set of SQL columns, or Graphite path suffixes under a shared prefix).
- Each metric can carry its own config: `direction` (whether higher or lower is "better," affecting regression vs. improvement labeling) and `scale` (value normalization).
- **Each metric is analyzed independently** — Otava runs change-point detection separately per series, so it's normal for a test to show a flagged regression in one metric (e.g. `response_time`) while other metrics from the same runs (e.g. `cpu_usage`) show nothing, due to differing noise levels, sensitivity settings, or data completeness.

### Interface

Primarily a CLI — `otava analyze <test>` — with flags for time-range filtering (`--since`, `--since-commit`, `--since-version`) and writing results back to [Grafana](https://otava.apache.org/docs/grafana), PostgreSQL, or BigQuery (`--update-grafana`, `--update-postgres`, `--update-bigquery`). Installable via pip, Docker, or from source.

### Typical use case

Run in CI/cron after each commit/test run, storing multiple metrics per run in one of the supported backends, with Otava periodically analyzing the accumulated per-metric history and reporting/annotating significant shifts.

**Full documentation:** [Apache Otava Documentation Overview](https://otava.apache.org/docs/overview)



## Airspeed Velocity (asv) — Change-Point Detection

### What it is

Regression detection in asv is a **step-detection algorithm** built into the core library ([`asv.step_detect`](https://asv.readthedocs.io/en/latest/autoapi/asv/step_detect/index.html), documented in detail on the [Step Detection](https://asv.readthedocs.io/en/latest/step_detection.html) page). It fits each benchmark's history to a piecewise-constant function — flat segments plus noise — using L1-penalized fitting (a weighted Potts model), and flags upward jumps as regressions. Noise weighting comes from each measurement's confidence interval, an automatic penalty-selection step avoids overfitting, and an optional AR(1) term accounts for correlated noise across nearby commits. `detect_regressions()` then filters the fitted steps down to real regressions using a tunable threshold and minimum segment size.

### Metrics

Change-point detection runs on **individual benchmark series**, and in asv "metric" maps directly onto **separate benchmark types**, distinguished by function-name prefix (see [Benchmark types and attributes](https://asv.readthedocs.io/en/latest/benchmarks.html)) — not a generic column concept:

| Prefix | What's measured |
|---|---|
| `time_*` | Wall-clock time |
| `timeraw_*` | Time in a fresh interpreter process |
| `mem_*` | Size of the returned object |
| `peakmem_*` | Peak process memory |
| `track_*` | Arbitrary user-defined value |

Each function yields one series per parameter combination/environment. `time_*` benchmarks carry median + interquartile range across repeated samples, feeding directly into step detection's noise weighting. Detection runs independently per series — a regression in `time_foo` says nothing about `mem_foo`.

### Interface/integration

Step detection is woven into the standard CLI flow rather than a separate command: `asv run` collects data, `asv publish` runs detection and builds report data, `asv preview` renders the interactive web report with regressions marked on each graph, and `asv compare`/`asv continuous` surface comparisons in the terminal for CI gating. Results are stored as JSON files in a `results_dir`, whose schema is documented in asv's [Developer Docs](https://asv.readthedocs.io/en/latest/dev.html#benchmark-suite-layout-and-file-formats). asv ships **no first-party notification or export system** — no built-in Slack/Grafana-style hooks — unlike Otava.

### Typical use case

Because asv is a full benchmarking harness, the typical pattern is **CI integration**: run the suite on a schedule or on new commits, accumulate results, and use step detection at publish time to surface regressions in the historical graphs — most valuable when tightly wired into a project's pipeline (e.g. `asv continuous` for PR-vs-main comparisons) rather than run ad hoc.

Pandas' [**`asv-runner`**](https://github.com/pandas-dev/asv-runner/) is a concrete example of this integration pattern: it runs on a schedule via GitHub Actions, adds a rolling-min/rolling-max ("established best" vs. "established worst") layer on top of core step detection to suppress noise from shared CI runners, and automatically files GitHub issues for confirmed regressions with commit links, magnitude, and chart links — plus consolidates all historical results into a single `results.parquet` for easier analysis than asv's raw per-commit JSON.

For the full picture, the [main asv documentation site](https://asv.readthedocs.io/en/latest/) covers installation, writing benchmarks, and the complete CLI reference.



## Conbench — Change-Point Detection

### What it is

Conbench's regression detection is a **statistical comparison method** called the **lookback z-score method** — not a true change-point/segmentation algorithm like Otava's or asv's. For a given commit ("contender"), it gathers matching **historical runs on the default branch** (same hardware, same benchmark case/context), computes their mean and standard deviation, and calculates how many standard deviations the new result deviates from that baseline. If the z-score exceeds a configured threshold (commonly 30–50 in practice — much higher than typical statistical conventions, tuned specifically to suppress false positives from noisy CI hardware), it's flagged as a possible regression. It requires **at least two matching historic runs** to run at all; without enough history it explicitly reports "could not do the lookback z-score analysis" rather than guessing. Some flagged results are separately labeled **"unstable"** — regressions known to sometimes be false positives with this method.

### Metrics

Conbench doesn't define fixed benchmark-type prefixes the way asv does (`time_*`, `mem_*`, etc.) — instead, each benchmark result is submitted with a **case** (parameterization) and **context** (compiler, build flags, etc.), and the z-score test runs **independently per unique (case, context, hardware) combination**. This means, like Otava and asv, a regression flagged in one metric/case doesn't imply anything about others from the same run — but Conbench's matching is more granular, since it also partitions by hardware and build context, not just metric name.

### Interface/integration

Conbench is designed as a **CI-integrated service** rather than a standalone offline analysis library. It typically runs as a GitHub Action step after a PR merges, posting a **"Conbench performance report"** comment/check directly on the commit — listing regressions found, benchmarks analyzed, and a link to details, with pass/fail status wired into CI (e.g. used by Apache Arrow and Meta's Velox). Results are stored centrally (originally backed by a PostgreSQL database) so historical runs can be queried for the z-score baseline. Unlike Otava, it doesn't ship generic CSV/SQL/Graphite importers — data is submitted to it as benchmark results directly.

### Typical use case

Conbench is built for **large, multi-hardware CI performance monitoring** in big open-source C++/data-systems projects — its most visible users are **Apache Arrow** and **Meta's Velox**, both of which run benchmark suites across many hardware configurations (x86, ARM, various instance types) on every merge, with Conbench automatically comparing each new commit's results against recent history per hardware/benchmark-case combination and posting a pass/fail report. This differs from asv's per-project single-history model and Otava's pluggable-backend-for-any-metrics-store model — Conbench's design center is being wired directly into a CI pipeline as a gating check across a benchmark matrix, at the cost of using a simpler (z-score, not segmentation-based) statistical method than either.



## Nyrkiö

### What it is

Nyrkiö is an **open-source, hosted change-detection platform** for Continuous Performance Engineering, built by Henrik Ingo (one of the long-time contributors to the MongoDB → Hunter → Apache Otava lineage) under the company Nyrkiö Oy. Rather than being a library you embed, it's primarily consumed as a **service**: you submit benchmark results, it runs change-point detection, and it reports back.

### Core algorithm

Nyrkiö uses **E-Divisive** (Matteson & James, 2014) — the same algorithmic family as Otava — implemented as its detection backend. In practice usage (documented in a MooBench case study), it's configured with:
- **p-value threshold** (e.g. `0.001`) — controls how conservative the significance test is; lower values mean fewer, higher-confidence change points (fewer false positives).
- **Change-magnitude threshold** (e.g. `5%`) — filters out statistically significant but practically trivial shifts, so Nyrkiö only surfaces changes above a meaningful size.
- Nyrkiö's own marketing claims sensitivity down to **0.5–1% regressions** for well-behaved (low-noise) data.

### How data gets in

- **REST API** — direct programmatic submission of results.
- **GitHub Action** — the primary on-ramp: Nyrkiö is explicitly built as an **extension of `benchmark-action/github-action-benchmark`**, the popular but simplistic GitHub Action that just does percentage-threshold alerting against the previous run. Nyrkiö sits on top of that same data pipeline and workflow integration, replacing its naive thresholding with genuine E-Divisive change-point detection.
- It supports output from major benchmarking frameworks (JMH, pytest-benchmark, etc.) via that same github-action-benchmark compatibility layer.

### Integration & reporting

- **GitHub integration**: identifies the specific commit responsible for a detected change, and can post notifications via **GitHub Issues**.
- **Public dashboard**: `nyrkio.com/public` hosts live examples (mentioned in Nyrkiö's own FOSDEM 2026 talk) — e.g. tracking noise/variance in trivial SQL queries (`SELECT 1`, `SELECT COUNT(*)`) run against a Turso database on GitHub-hosted runners, used to illustrate how much of "signal" in typical CI benchmarks is actually just hardware noise.
- **GitHub App** ([github.com/apps/nyrkio](https://github.com/apps/nyrkio)) is the installable entry point for repos.

### Beyond change detection: GitHub Runners product

Notably, Nyrkiö's FOSDEM talk revealed it also sells **custom-tuned GitHub Actions runners** (`nyrkio_2`, on C7a instances) specifically engineered for **low benchmark noise/variance** rather than raw performance — no hyperthreading, single CPU socket, hand-picked instance families — because a large fraction of "regressions" that plague continuous benchmarking are actually just cloud/CPU-scheduling noise, not real code regressions. Their own measurements show meaningfully tighter min-max timing ranges on their runners vs. GitHub's default or generic third-party runners. This reflects a broader thesis in their messaging: **change-point detection algorithms alone aren't enough — noise has to be minimized at the infrastructure layer too**, or you're just asking better math to compensate for bad data.

### Relationship to Otava

Nyrkiö and Apache Otava share direct lineage and personnel (Henrik Ingo contributed to both, and co-authored the Otava optimization history paper). The practical distinction:
- **Otava** = self-hosted, open-source CLI/library, pluggable into your own CSV/SQL/Graphite/BigQuery data stores, full control over your own pipeline.
- **Nyrkiö** = hosted service, tightly coupled to the GitHub Actions ecosystem (extending `github-action-benchmark`), paid subscription model (including the specialized runners), lower setup friction if you're already on GitHub Actions and don't want to run your own change-detection infrastructure.

### Sources

- [Nyrkiö GitHub App](https://github.com/apps/nyrkio)
- [Detection of Performance Changes in MooBench Results Using Nyrkiö on GitHub Actions (arXiv:2510.11310)](https://arxiv.org/pdf/2510.11310)
- [8 Years of Optimizing Apache Otava (arXiv:2505.06758)](https://arxiv.org/pdf/2505.06758)
- [FOSDEM 2026 — Continuous Performance Engineering HowTo (Henrik Ingo)](https://fosdem.org/2026/events/attachments/YNB7KR-continuous-perf-engineering/slides/267539/fosdem_20_ugdcwn6.pdf)


