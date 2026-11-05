// Two benchmarks for the benchx demo: a quiet one and a noisy one.
//
// kWork is what the revisions demo changes between commits; DEMO_HARDENED is
// what the environments demo changes between two builds of one commit. Both
// add a few percent of work, so the expected verdicts are known: the quiet
// benchmark flags the change, the noisy one does not.
#include <benchmark/benchmark.h>

#include <random>
#include <vector>

constexpr int kWork = 100;
#ifdef DEMO_HARDENED
constexpr int kExtra = 3;  // simulated cost of hardening checks, in percent
#else
constexpr int kExtra = 0;
#endif

static double Sum(const std::vector<double>& v, long rounds) {
  double s = 0;
  for (long r = 0; r < rounds; ++r) {
    for (double x : v) s += x;
    benchmark::DoNotOptimize(s);
  }
  return s;
}

static void BM_Quiet(benchmark::State& state) {
  std::vector<double> v(state.range(0), 1.0);
  const long rounds = kWork * (100 + kExtra);
  for (auto _ : state) benchmark::DoNotOptimize(Sum(v, rounds));
}
BENCHMARK(BM_Quiet)->Arg(1024);

// Each repetition draws its own load level, so repetitions disagree by
// around ten percent, as they do for a benchmark at the mercy of its machine.
static void BM_Noisy(benchmark::State& state) {
  std::vector<double> v(state.range(0), 1.0);
  std::random_device device;
  std::uniform_real_distribution<double> level(1.0, 1.25);
  const long rounds = static_cast<long>(kWork * (100 + kExtra) * level(device));
  for (auto _ : state) benchmark::DoNotOptimize(Sum(v, rounds));
}
BENCHMARK(BM_Noisy)->Arg(1024);
