# xpbench — design document

## 1. Goals

- A `benchmark` fixture with the same name, call shape, and return value as
  pytest-benchmark's (`benchmark(func, *args, **kwargs)`, `.pedantic(...)`),
  so suites can be authored against this design and swapped to real
  pytest-benchmark (or back) by adding/removing one fixture override — no
  test body changes.
- Correct synchronization for async-dispatch array backends (torch CUDA,
  jax, cupy) before timing stops.
- Multiple genuinely first-class metrics per benchmark (own name, own full
  stats, own row in output) — not sidecar `extra_info`.
- Suite-level (not per-test) control of which measurement strategy an `xp`
  backend uses.
- Suite authors can add new measurement strategies without modifying the
  core engine.
- Two measurement modes: adaptive (`%timeit`-like, auto-calibrated) and
  pedantic (manual rounds/iterations/warmup, no calibration).
- Multi-GPU support: compare N cards, or benchmark a function that spans
  multiple GPUs at once.
- Persist results and compare across runs.
- Core measurement logic has zero pytest dependency; pytest is an adapter
  layer on top.

## 2. Non-goals

- True concurrent multi-GPU contention measurement — configurations are
  measured in isolation/sequentially, not simultaneously.
- CI regression-failure gating (numbers are computed and can be compared,
  but nothing fails a build automatically).
- pytest-xdist parallel-worker result aggregation.
- Environment/provenance metadata (git commit, GPU model, driver version)
  in saved runs.

## 3. Why not build on pytest-benchmark directly

pytest-benchmark ties exactly one stats series to one test node — there is
no API to attach two independently-tracked metrics (e.g. `time` and
`gpu_time`) to a single test. Working around this by generating one
synthetic test per metric (`bench_foo[wall]`, `bench_foo[gpu_event]`, via
`pytest_generate_tests` + a swappable `--benchmark-timer` clock) works, but
reruns the whole benchmark once per metric and — critically — cannot
express **value-dependent synchronization** (see §4). A custom clock
function has no access to the return value of the benchmarked call, so it
cannot call `jax.block_until_ready(result)`. This made pytest-benchmark
unsuitable as the substrate for jax support specifically, not just
inconvenient for multi-metric. The engine described here owns its own
round loop and stats storage instead, which makes multi-metric-per-node
native and removes the value-blindness problem, at the cost of
reimplementing calibration, rounds, and reporting.

## 4. Core correctness distinction: value-dependent vs. value-independent sync

Async array backends return a handle immediately and finish the real
computation later. Getting a correct timing measurement requires waiting
for that computation before stopping the clock. The wait mechanism differs
by backend in a way that matters structurally:

- **Value-independent** (torch CUDA, cupy): a global or per-device barrier
  (`torch.cuda.synchronize(device)`, `cupy.cuda.Device(id).synchronize()`)
  or a pair of bracketing event objects. Neither needs the function's
  return value — sync can happen purely by knowing which device/stream to
  wait on.
- **Value-dependent** (jax): `jax.block_until_ready(value)` must be given
  the actual returned array/pytree. There is no global "wait for
  everything" primitive in jax. This is not a convenience choice — it is
  the only correct mechanism, because jax's async model requires a
  concrete value to block on.

Consequence for the design: any measurement abstraction must receive the
call's result, not just bracket the call from outside. This ruled out a
pure "swap the clock function" mechanism as the general case (see §3) and
is why `Probe.stop(result)` below takes `result` explicitly.

A related, easily-missed case: a function like
`def foo(arr): return float(arr.sum())` triggers an *implicit* host sync
inside `foo` itself (via `__float__`), before it even returns. A
downstream `block_until_ready(result)` on the returned Python float is
then a harmless no-op — sync already happened. The design does not special
-case this; calling `block_until_ready` unconditionally on the return
value is always safe, whether or not it turns out to be redundant.

## 5. Core contracts (no pytest dependency)

### 5.1 `Context`

Replaces named parameters (`xp`, `device`, ...) in `Timer`/`Probe.setup()`.
Without this, every new dimension a suite invents would require editing
the base contract and every existing subclass. `Context` is a plain
`dict` with attribute-access sugar; nothing more.

```python
class Context(dict):
    """ctx["name"] / ctx.name -> required, raises if absent.
       ctx.get("name", default) -> optional.
       "name" in ctx -> presence check."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)
```

Values in a `Context` are already resolved (no laziness, no memoization —
an earlier lazy/memoized design was simplified away; see §11). Probes
choose `ctx["xp"]` (required — fail loudly if absent) vs. `ctx.get("device")`
(optional — genuinely absent for CPU-only suites) per key, deliberately.

`xp` is the one structurally special key: the Timer registry (§5.4) must
select a `Timer` class by backend name *before* `Timer.setup(ctx)` can run,
so something outside `Context` has to extract it first. Every other
dimension (`device`, `devices`, or anything a suite invents later) is
fully generic from the base contract's point of view.

### 5.2 `Timer`

```python
class Timer:
    primary_metric = "time"   # which returned metric key drives calibration
    max_iterations = None     # None = unbounded; see §8

    def setup(self, ctx: Context): ...
    def teardown(self): ...
    def measure(self, func, *args, iterations=1, **kwargs):
        """Run func `iterations` times. Return (last_result, metrics: dict[str, float])."""
        raise NotImplementedError
```

`measure()` returning `(last_result, metrics)` — not a single number — is
what makes multi-metric-per-node native (§3): whatever keys the dict
contains all become independent stats series with no extra plumbing.

`primary_metric` must name a metric whose value is a true, sync-included
elapsed-time bound. If it named a dispatch-only (unsynced) quantity,
calibration (§7) would massively over-batch, since dispatch time is
typically microseconds regardless of real kernel cost — the doubling
search would pick a batch size assuming the operation is cheap when it
isn't, and a single calibration trial could run for a very long time
before the safety valve (`max_calibration_time`) intervenes.

### 5.3 `Probe` and `CompositeTimer`

A `Probe` is one independent measurement concern (wall time, GPU events,
peak memory, a jax sync barrier). `CompositeTimer` composes several probes
into a `Timer` without each probe reimplementing the iteration loop.

```python
class Probe:
    cumulative = ()              # metric names from stop() to divide by iterations
    single_iteration_only = False

    def setup(self, ctx: Context): ...
    def teardown(self): ...
    def start(self): ...
    def stop(self, result) -> dict: raise NotImplementedError


class CompositeTimer(Timer):
    probes: list[Probe]

    def setup(self, ctx):
        self.ctx = ctx
        for p in self.probes:
            p.setup(ctx)

    def teardown(self):
        for p in self.probes:
            p.teardown()

    @property
    def max_iterations(self):
        return 1 if any(p.single_iteration_only for p in self.probes) else None

    def measure(self, func, *args, iterations=1, **kwargs):
        for p in self.probes:                    # first listed = outermost = starts first
            p.start()
        result = None
        for _ in range(iterations):
            result = func(*args, **kwargs)
        metrics = {}
        for p in reversed(self.probes):           # LIFO: last-started stops first
            for k, v in p.stop(result).items():
                metrics[k] = v / iterations if k in p.cumulative else v
        return result, metrics
```

**LIFO ordering is load-bearing, not stylistic.** A probe that reads wall
time must observe any sync an inner probe performs, or it silently
undercounts. Listing probes outermost-first and stopping them in reverse
guarantees this by construction: the wall-time probe's `stop()` — the
clock read — necessarily runs after every inner probe's `stop()`,
including whatever sync they do. Getting the list order backwards
produces a plausible-looking, silently wrong `time` value with no
exception — this is the one sharp edge suite authors must know about when
composing probes by hand.

**`cumulative` controls division by `iterations`.** Time-like metrics
divide (batched calls → per-call average is meaningful, since device
execution is cumulative). Peak-memory-like metrics never divide — peak is
a high-water mark, not a cumulative quantity; running N calls back-to-back
should hit roughly the same peak per call in steady state, and a genuine
leak across calls should show up as an inflated peak, not get diluted by
`/N`. `TracemallocProbe`/`RSSProbe` below correctly omit their metric
names from `cumulative`.

### 5.4 Registry

```python
_registry = {}

def register_timer(xp_name: str, timer_cls):
    _registry[xp_name] = timer_cls

def get_timer_cls(xp):
    if xp is None:
        return NumpyTimer
    name = xp.__name__.split(".")[0]
    return _registry.get(name, _DEFAULT_TIMERS.get(name, NumpyTimer))
```

`register_timer` is called once by a suite author, typically in a
`conftest.py`. Because conftest files load hierarchically, a call in a
directory's `conftest.py` scopes to that directory and everything below
it; a nested `conftest.py` can override further for a subtree. Individual
`bench_*` test authors never call this — the requirement was suite-level,
not per-test, control.

## 6. Built-in probes and composed timers

```python
class CpuTimeProbe(Probe):
    cumulative = ("time",)
    def start(self):
        self._t0 = time.perf_counter()
    def stop(self, result):
        return {"time": time.perf_counter() - self._t0}


def _normalize_devices(device):
    if device is None:
        return ()
    if isinstance(device, (list, tuple)):
        return tuple(device)
    return (device,)


class TorchCudaEventProbe(Probe):
    """Per-device GPU time via CUDA events, plus a max-across-devices
    aggregate. Works unmodified for 1 device (compare-cards case) or N
    (function-spans-multiple-GPUs case) — device count is not special-cased."""
    def setup(self, ctx):
        super().setup(ctx)
        self.xp = ctx["xp"]
        self.devices = _normalize_devices(ctx.get("device") or ctx.get("devices"))

    def start(self):
        self._pairs = {}
        for d in self.devices:
            with self.xp.cuda.device(d):
                s = self.xp.cuda.Event(enable_timing=True)
                s.record()
                self._pairs[d] = [s]

    def stop(self, result):
        for d in self.devices:
            with self.xp.cuda.device(d):
                e = self.xp.cuda.Event(enable_timing=True)
                e.record()
                self._pairs[d].append(e)
        for d in self.devices:
            self.xp.cuda.synchronize(d)          # only this device's queue
        metrics = {f"gpu_time[{d}]": s.elapsed_time(e) / 1000.0
                   for d, (s, e) in self._pairs.items()}
        if metrics:
            metrics["gpu_time_max"] = max(metrics.values())
        return metrics

    @property
    def cumulative(self):
        return tuple(f"gpu_time[{d}]" for d in self.devices) + ("gpu_time_max",)


class CupyDeviceSyncProbe(Probe):
    def setup(self, ctx):
        super().setup(ctx)
        self.xp = ctx["xp"]
        self.devices = _normalize_devices(ctx.get("device") or ctx.get("devices"))
    def stop(self, result):
        for d in self.devices:
            self.xp.cuda.Device(d).synchronize()
        return {}


class JaxSyncProbe(Probe):
    """No metric of its own. Exists to force jax's value-dependent sync
    before an outer probe reads its clock. Correctly requires no
    device-awareness at all: a jax array already carries its own device
    (via jax.device_put), and block_until_ready walks the full pytree, so
    multi-device jax functions are synced correctly with zero extra code."""
    def stop(self, result):
        import jax
        jax.block_until_ready(result)
        return {}


class TracemallocProbe(Probe):
    """Peak host (Python-level) memory. Resettable per round via
    tracemalloc.reset_peak() (stdlib, added specifically for this use case:
    rebasing the peak counter without restarting the underlying trace)."""
    def setup(self, ctx):
        super().setup(ctx)
        self._owns = not tracemalloc.is_tracing()
        if self._owns:
            tracemalloc.start()
    def teardown(self):
        if self._owns:
            tracemalloc.stop()
    def start(self):
        tracemalloc.reset_peak()
    def stop(self, result):
        _, peak = tracemalloc.get_traced_memory()
        return {"peak_host_memory": peak}        # absent from cumulative -> never divided


class RSSProbe(Probe):
    """True OS-level peak RSS. See §10 for the tradeoffs vs TracemallocProbe."""
    def setup(self, ctx):
        super().setup(ctx)
        import resource
        self._resource = resource
    def stop(self, result):
        maxrss = self._resource.getrusage(self._resource.RUSAGE_SELF).ru_maxrss
        return {"rss_high_water_mark": maxrss}
```

Composed timers — the units suite authors actually register:

```python
class NumpyTimer(CompositeTimer):
    primary_metric = "time"
    probes = [CpuTimeProbe()]

class TorchFullTimer(CompositeTimer):
    primary_metric = "time"
    probes = [CpuTimeProbe(), TorchCudaEventProbe(), TracemallocProbe()]

class TorchFullWithRSS(CompositeTimer):        # swap memory strategy: one-line diff
    primary_metric = "time"
    probes = [CpuTimeProbe(), TorchCudaEventProbe(), RSSProbe()]

class JaxWallTimer(CompositeTimer):
    primary_metric = "time"
    probes = [CpuTimeProbe(), JaxSyncProbe()]

class CupyWallTimer(CompositeTimer):
    primary_metric = "time"
    probes = [CpuTimeProbe(), CupyDeviceSyncProbe()]

_DEFAULT_TIMERS = {"numpy": NumpyTimer, "cupy": CupyWallTimer,
                    "torch": TorchFullTimer, "jax": JaxWallTimer}
```

A suite author who wants something not covered here (e.g. an RSS-based
memory metric, or a bespoke sync strategy) writes a `Probe` or a plain
`Timer` subclass satisfying §5.2/§5.3 directly — no core file changes.

## 7. Measurement modes: `Benchmark.__call__` (adaptive) and `.pedantic`

```python
class Benchmark:
    def __init__(self, timer, warmup_rounds=1, target_round_time=0.2,
                 repeat=5, max_calibration_time=5.0):
        self.timer = timer
        self.warmup_rounds = warmup_rounds
        self.target_round_time = target_round_time
        self.repeat = repeat
        self.max_calibration_time = max_calibration_time
        self.result = BenchmarkResult()
        self.group = None
        self.extra_info = {}
        self._called = False

    def __call__(self, func, *args, **kwargs):
        if self._called:
            raise RuntimeError("benchmark() can only be called once per test")
        self._called = True

        for _ in range(self.warmup_rounds):        # untimed: absorbs jit/first-launch cost
            self.timer.measure(func, *args, iterations=1, **kwargs)

        max_it = self.timer.max_iterations
        n = 0
        primary = self.timer.primary_metric
        t_cal_start = time.perf_counter()
        while True:
            n = _next_count(n)                      # 1, 2, 5, 10, 20, 50, ... (timeit-style)
            if max_it is not None:
                n = min(n, max_it)
            _, metrics = self.timer.measure(func, *args, iterations=n, **kwargs)
            batch_total = metrics[primary] * n
            if batch_total >= self.target_round_time:
                break
            if max_it is not None and n >= max_it:
                break
            if time.perf_counter() - t_cal_start > self.max_calibration_time:
                break

        result = None
        for _ in range(self.repeat):
            result, metrics = self.timer.measure(func, *args, iterations=n, **kwargs)
            self.result.add_round(metrics)
        self.result.iterations_per_round = n
        return result

    def pedantic(self, func, args=(), kwargs=None, setup=None,
                 rounds=1, warmup_rounds=0, iterations=1):
        max_it = self.timer.max_iterations
        if max_it is not None and iterations > max_it:
            raise ValueError(
                f"{type(self.timer).__name__} requires iterations<={max_it} "
                f"(a probe needs a single call's result per measurement); got {iterations}")
        kwargs = kwargs or {}
        self._called = True

        def _one_round():
            call_args, call_kwargs = args, kwargs
            if setup is not None:
                s = setup()
                if s:
                    call_args, call_kwargs = s
            return self.timer.measure(func, *call_args, iterations=iterations, **call_kwargs)

        for _ in range(warmup_rounds):
            _one_round()
        result = None
        for _ in range(rounds):
            result, metrics = _one_round()
            self.result.add_round(metrics)
        return result
```

```python
import itertools
def _next_count(n):
    for precision in itertools.count():
        for m in (1, 2, 5):
            candidate = m * 10 ** precision
            if candidate > n:
                return candidate
```

`__call__` vs `.pedantic`, matching pytest-benchmark's real semantics:

| | `__call__` | `.pedantic` |
|---|---|---|
| Round/iteration count | Auto-calibrated | Caller specifies explicitly |
| Per-round fresh args | Not supported | `setup` callable, may return new `(args, kwargs)` |
| Reproducibility | Lower (adaptive) | Higher (fixed counts) |
| `iterations` batching | Automatic | Caller's choice |

## 8. `iterations` batching: correctness and its limit

Batching N calls, syncing once at the end, dividing the total by N is
valid **because execution on a single device/stream is ordered (FIFO)**:
if call N was dispatched after calls 1..N-1 on the same stream, its
completion implies theirs. Blocking on only the *last* call's result (or
doing one global/per-device sync at the end) is therefore sufficient —
you do not need to hold or sync every intermediate result. What is lost is
per-call granularity: you get one averaged sample per round, not N
independent samples. This is also exactly how pytest-benchmark's own
`iterations`/`rounds` split works (iterations batch and average within a
round; rounds supply the distribution across rounds).

This assumption can break: explicit additional CUDA streams (torch) are
not FIFO-ordered relative to the default stream purely by issue order, and
jax `pmap`/multi-device sharding places calls on devices with no shared
ordering guarantee at all. For the common case — one device, default
stream/dispatch — it holds.

`Timer.max_iterations` / `Probe.single_iteration_only` exist for probes
that need a single call's result specifically (e.g. a hypothetical
per-call memory-delta probe, where "the last of N calls" and "the only
call" must be the same thing for the reading to mean anything).
`CompositeTimer.max_iterations` is 1 if any composed probe requires it.
Enforcement: the calibration loop in `__call__` caps `n` at `max_iterations`
and cannot grow the batch further; `.pedantic` raises `ValueError` rather
than silently clamping a caller-specified `iterations`, since pedantic
mode's whole purpose is caller control. Known consequence: with
`max_iterations=1`, calibration's only lever (batch growth) is gone —
`repeat` is currently fixed, not adaptive, so a genuinely fast
single-iteration-only benchmark sits at the `time.perf_counter()`
resolution floor. Making `repeat` adaptive is an open item, not yet built.

## 9. Multi-GPU: comparing cards vs. one function spanning several

Both are the same underlying case — a set of devices to sync — unified by
always normalizing `device` to a tuple in probe `setup()` (`_normalize_devices`,
§6): `None → ()`, a single device `→ (d,)`, a list/tuple `→` itself.
`TorchCudaEventProbe` loops over `self.devices` unconditionally; at length
1 this reproduces single-device behavior exactly, at length N it produces
one `gpu_time[device]` metric per device plus a `gpu_time_max` aggregate.

Two fixtures on the pytest side, feeding the same normalized path:

```python
@pytest.fixture
def device(request):
    return request.param        # single device, parametrized -> "compare cards"

@pytest.fixture
def devices(request):
    return getattr(request, "param", None)   # list -> "function needs several at once"
```

```python
def bench_matmul(benchmark, xp, device):        # one row per card
    a = xp.randn(512, 512, device=device)
    benchmark(xp.matmul, a, a)

def bench_allreduce(benchmark, xp, devices):      # one function touching N devices
    tensors = [xp.ones(10_000_000, device=d) for d in devices]
    benchmark(lambda: some_multi_gpu_op(tensors))
```

Parametrizing `devices` itself over increasing subset sizes
(`params=[1, 2, 4]`) yields a scaling curve as a byproduct of the same
mechanism — not a separate feature.

**Caveat, not fixed by this design:** `gpu_time_max` assumes the wall
clock is bounded by the slowest device, which holds for genuinely
concurrent dispatch but understates the true critical path for
serialized/dependent multi-GPU patterns (e.g. a ring all-reduce with
inter-device data dependencies). A true critical-path metric would need a
dedicated span computation (first device's start event to last device's
end event across the dependency chain), not just `max` over independent
per-device brackets.

## 10. Memory probe tradeoffs (`TracemallocProbe` vs `RSSProbe`)

- `TracemallocProbe`: Python-level allocations only (via `PyMem_Malloc`
  hooks). Resettable per round (`tracemalloc.reset_peak()`, stdlib,
  Python ≥3.9, added specifically to rebase the peak without restarting
  the trace). Under-reports C-level buffers (e.g. raw numpy array data)
  unless numpy's `tracemalloc_domain` integration is separately enabled.
- `RSSProbe`: true OS-level peak resident set size via
  `resource.getrusage(...).ru_maxrss`. Complete (sees all memory, not just
  Python-tracked), but POSIX-only (unavailable on Windows), **monotonic
  for the process lifetime with no reset** (so a "round's" reading can
  reflect the worst round seen anywhere earlier in the session, not just
  that round), and units differ by platform (KB on Linux, bytes on macOS).

Neither is strictly better; which to register per suite is a real
decision, made once via `register_timer`, not hardcoded into the engine.

## 11. pytest layer

The core (`Context`, `Timer`, `Probe`, `CompositeTimer`, `Benchmark`,
`Stats`, `BenchmarkResult`, the registry) has no pytest import anywhere.
Proof: it is directly usable from a plain script —

```python
ctx = Context(xp=torch, device=torch.device("cuda:0"))
timer = get_timer_cls(torch)()
timer.setup(ctx)
bench = Benchmark(timer)
a = torch.randn(512, 512, device="cuda:0")
bench(torch.matmul, a, a)
print(bench.result.metrics["time"].mean)
timer.teardown()
```

The pytest adapter builds a `Context` from whatever fixtures a test
declared:

```python
def context_from_request(request, exclude=()) -> Context:
    """exclude MUST contain the name of the fixture currently under
    construction. request.fixturenames includes that fixture's own name;
    eagerly resolving it here means asking pytest to resolve a fixture
    from inside its own setup, which pytest rejects. Every other name in
    fixturenames is resolved unconditionally, including fixtures no
    Probe ends up using — add those to exclude too if that cost or side
    effect is unacceptable."""
    return Context({
        name: request.getfixturevalue(name)
        for name in request.fixturenames
        if name not in exclude
    })

@pytest.fixture
def benchmark(request):
    ctx = context_from_request(request, exclude={"benchmark"})
    xp = ctx.get("xp")
    timer = get_timer_cls(xp)()
    timer.setup(ctx)
    fixture = Benchmark(timer)
    yield fixture
    timer.teardown()
    if fixture._called:
        _results[request.node.nodeid] = fixture.result

@pytest.fixture
def xp(request): return request.param

@pytest.fixture
def device(request): return request.param

@pytest.fixture
def devices(request): return getattr(request, "param", None)

def pytest_generate_tests(metafunc):
    if "xp" not in metafunc.fixturenames:
        return
    xp_values = _xp_param_values(metafunc)          # suite-supplied
    if "device" in metafunc.fixturenames:
        combos = [(v, d) for v in xp_values for d in _devices_for(v)]  # suite-supplied
        metafunc.parametrize("xp,device", combos, indirect=["xp", "device"])
    else:
        metafunc.parametrize("xp", xp_values, indirect=["xp"])
```

An earlier version of `Context` used a `Lazy` wrapper with per-key
memoized resolution, so fixtures were only ever resolved if a probe
actually asked for them. This was simplified to eager resolution (plain
dict, §5.1) because pytest already caches fixtures per test scope — the
memoization was mostly redundant safety there — at the cost of the
`exclude` parameter above being an explicit, documented rule instead of
something that fell out invisibly. The only place the lazy version bought
something real was pure-script usage with a non-deterministic value
source; not a concern for the pytest adapter.

### Zero-diff migration contract

Swapping to real pytest-benchmark means deleting this `benchmark` fixture
override (fixture resolution falls through to pytest-benchmark's own,
registered via its `pytest11` entry point) — no `bench_*` test body
changes either direction, because fixture name, call shape, return value,
and `.pedantic` signature (minus `iterations` values above a probe's
`max_iterations`, which upstream has no equivalent restriction for) all
match.

**Lost on swap to real pytest-benchmark:** xp-aware sync correctness (it
just calls and times `func`, no jax/torch/cupy sync at all — numbers
become dispatch-time only for async backends); multi-metric output (only
wall time remains); the `register_timer` suite-level mechanism (no
equivalent).

**Gained on swap:** pytest-benchmark's more mature reporting —
`--benchmark-json`, `--benchmark-compare`, histograms, and calibration
logic that has been tuned in production for longer than this design's
fixed-`repeat` adaptive loop.

## 12. Persistence and comparison

`Stats`/`BenchmarkResult` store raw per-round samples (not just summary
numbers), so summaries can always be derived later:

```python
class Stats:
    def __init__(self): self.samples = []
    def add(self, value): self.samples.append(value)
    @property
    def mean(self): return statistics.mean(self.samples)
    @property
    def stddev(self): return statistics.stdev(self.samples) if len(self.samples) > 1 else 0.0
    @property
    def min(self): return min(self.samples)
    @property
    def max(self): return max(self.samples)
    def to_dict(self): return {"samples": self.samples}
    @classmethod
    def from_dict(cls, d):
        s = cls(); s.samples = d["samples"]; return s

class BenchmarkResult:
    def __init__(self):
        self.metrics = {}              # name -> Stats
        self.iterations_per_round = None
        self.group = None
        self.extra_info = {}
    def add_round(self, metrics: dict):
        for name, value in metrics.items():
            self.metrics.setdefault(name, Stats()).add(value)
    def to_dict(self):
        return {"iterations_per_round": self.iterations_per_round,
                 "group": self.group, "extra_info": self.extra_info,
                 "metrics": {k: v.to_dict() for k, v in self.metrics.items()}}
    @classmethod
    def from_dict(cls, d):
        r = cls()
        r.iterations_per_round = d.get("iterations_per_round")
        r.group = d.get("group")
        r.extra_info = d.get("extra_info", {})
        r.metrics = {k: Stats.from_dict(v) for k, v in d["metrics"].items()}
        return r
```

CLI surface (pytest layer only):

```
--xp-benchmark-save=NAME        save this run under .benchmarks/NAME.json
--xp-benchmark-autosave         save with a timestamped name
--xp-benchmark-compare=NAME     print % deltas vs a previously saved run
--xp-benchmark-storage=DIR      default .benchmarks
```

```python
def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if not _results:
        return
    storage = pathlib.Path(config.getoption("--xp-benchmark-storage"))
    storage.mkdir(parents=True, exist_ok=True)
    name = config.getoption("--xp-benchmark-save")
    if name or config.getoption("--xp-benchmark-autosave"):
        name = name or time.strftime("%Y%m%d_%H%M%S")
        payload = {"created": time.time(),
                   "results": {nid: r.to_dict() for nid, r in _results.items()}}
        (storage / f"{name}.json").write_text(json.dumps(payload, indent=2))
    compare_target = config.getoption("--xp-benchmark-compare")
    if compare_target:
        _print_comparison(compare_target, storage)

def _load_run(name_or_path, storage):
    path = pathlib.Path(name_or_path)
    if not path.exists():
        path = storage / f"{name_or_path}.json"
    data = json.loads(path.read_text())
    return {nid: BenchmarkResult.from_dict(d) for nid, d in data["results"].items()}

def _print_comparison(name_or_path, storage):
    old = _load_run(name_or_path, storage)
    for nodeid, new in _results.items():
        old_result = old.get(nodeid)
        if old_result is None:
            continue
        for metric, new_stats in new.metrics.items():
            old_stats = old_result.metrics.get(metric)
            if old_stats is None:
                continue
            delta = (new_stats.mean - old_stats.mean) / old_stats.mean * 100
            print(f"{nodeid} {metric}: old={old_stats.mean:.6g} new={new_stats.mean:.6g} ({delta:+.1f}%)")
```

Usage:
```
pytest --xp-benchmark-save=baseline
# ... make changes ...
pytest --xp-benchmark-compare=baseline
```

## 13. Proposed file layout

```
xpbench/
  context.py        # Context
  timer.py           # Timer, Probe, CompositeTimer, built-in probes and composed timers
  engine.py          # Stats, BenchmarkResult, Benchmark, calibration helper, registry
  pytest_plugin.py   # the only module that imports pytest: fixtures, parametrization,
                      # save/compare CLI, context_from_request adapter
```

## 14. Known limitations / open items

- **pytest-xdist**: `_results` is a plain module-level dict; under
  parallel workers each worker has its own copy, so `pytest_sessionfinish`
  on the controller process would not see worker data without xdist's
  cross-worker aggregation hooks (`workeroutput`/`pytest_testnodedown`),
  not implemented here.
- **No CI regression gating**: `--xp-benchmark-compare` prints deltas; it
  never fails the run. A `--xp-benchmark-compare-fail=metric:pct`
  equivalent to pytest-benchmark's is a straightforward addition, not yet
  built.
- **No environment metadata** in saved JSON (Python version, git commit,
  GPU model) — comparisons across different machines can be misread as
  regressions.
- **Sequential multi-GPU measurement only** — configurations are measured
  one after another, not simultaneously; does not capture cross-device
  contention (shared PCIe/NVLink bandwidth, host dispatch overhead when
  driving multiple devices from one process concurrently).
- **`gpu_time_max` is a lower bound on critical path**, not a true
  critical-path measurement, for multi-GPU functions with inter-device
  dependencies (§9).
- **`max_iterations=1` removes calibration's batch-growth lever**;
  `repeat` is currently fixed rather than adaptive, so single-iteration
  -only timers can sit at the host clock's resolution floor for very fast
  functions.
- **Probe ordering correctness is unenforced** — listing probes in the
  wrong order (§5.3) silently produces a wrong `time` value with no
  runtime detection.
- **`__call__` has no `setup` hook**, matching upstream pytest-benchmark
  (only `.pedantic` has one) — intentional parity, not an oversight, but
  means adaptive-mode calibration cannot be combined with per-round fresh
  arguments for mutating functions.
