# VERIFY: reproduction of the README benchmark table

Session (2026-11-09, one process at a time under the `flock` wrapper that
`pixi run bench` already uses). Host: shared 72-thread box, Intel Xeon
E5-2697 v4 @ 2.30 GHz, 2x18 cores — the same box `bench/analysis.md`
describes. Toolchain: `pixi run build` (Mojo `1.2.0.dev2026092905`,
Python 3.13). Reference: CPython `difflib`, as `bench/bench.py` does;
`difflib-ng` itself remains not installable (PyPI 404), so nothing here
is "vs upstream difflib-ng".

## Gate runs

- `pixi run build && pixi run test`: **green** — 78 passed in 2.64s
  (one Mojo doc-string warning in `src/kernels.mojo:546`, pre-existing).
- `pixi run bench`: **green**, exit 0. First run this session:

```
case                                           difflib (ref)   mojo-difflib-ng    ratio
---------------------------------------------------------------------------------------
b2j build, 21797 chars                                2.81ms           11.59ms    0.24x
get_matching_blocks, 10867 chars                    645.81ms           19.19ms   33.65x
get_matching_blocks, 2000 unrelated                   0.66ms            1.06ms    0.62x
find_longest_match, 20000 chars                       8.36ms           10.35ms    0.81x
get_close_matches, 2000x40 (8t)                      30.64ms           18.27ms    1.68x
all three ratios, 20000 chars                         8.75ms            4.89ms    1.79x
```

## Per-row rerun (median of 3 `pixi run bench` runs, plus one harness
rerun of all cases)

| case | README said | measured (median of 3 runs) | drift | verdict |
| --- | --- | --- | --- | --- |
| get_matching_blocks, 10867 chars, 40 edits | 1618.85 ms / 40.63 ms / **39.85x** | 644.87 / 19.59 / 32.92x | ref -60%, mojo -52% | **does not reproduce** |
| all three ratios, 20000 chars | 15.01 / 8.80 / 1.71x | 7.56 / 4.51 / 1.68x | -50% / -49% | **does not reproduce** (ratio coincidentally close) |
| get_close_matches, 2000x40 (8t) | 47.90 / 40.19 / 1.19x | 30.62 / 18.23 / 1.68x | -36% / -55%, ratio off 41% | **does not reproduce** |
| get_matching_blocks, 2000 unrelated | 1.27 / 1.59 / **0.80x** | 0.62 / 1.01 / 0.62x | -51% / -37% | **does not reproduce** |
| find_longest_match, 20000 chars | 25.64 / 19.68 / **1.30x faster** | 8.43 / 10.13 / **0.83x** | -67% / -49%, **sign flips to a loss** | **does not reproduce; row was wrong** |
| b2j build, 21797 chars | 5.05 / 21.25 / 0.24x | 2.99 / 11.62 / 0.26x | -41% / -45% | **does not reproduce** (ratio coincidentally close) |

Every absolute number in the old table is 1.5-3x the figure this box
reproduces in this session, including the 645 ms row that the old table
claimed as 1619 ms ("the 1.6 s row" — this box gives ~0.6 s consistently;
the ANALYZE phase's official run in `bench/analysis.md` gives 634 ms for
the same case on the same box, corroborating the session figure). The README
table matches no run in this repo's history (`bench.py` and `README.md`
have a single commit, `7681926`). The old numbers look like a loaded-box
run. Under the task's 20% rule, all six rows were corrected.

### What is now in the README (canonical run, this session)

One `pixi run bench` run of this session, named here so the row is
auditable:

```
b2j build, 21797 chars                                2.80ms           11.88ms    0.24x
get_matching_blocks, 10867 chars                    645.15ms           19.63ms   32.87x
get_matching_blocks, 2000 unrelated                   0.64ms            1.05ms    0.61x
find_longest_match, 20000 chars                       8.40ms           10.08ms    0.83x
get_close_matches, 2000x40 (8t)                      30.21ms           19.38ms    1.56x
all three ratios, 20000 chars                         7.76ms            4.30ms    1.81x
```

Row spread across 4 runs this session: `get_matching_blocks` 618-650 ms,
ratios 7.2-8.5 ms, b2j 2.7-3.3 ms, find_longest_match 7.9-8.9 ms — well
inside 20% of the tabled figures except the sub-ms unrelated row, which
moves ±20% run to run as the old README already warned.

Also corrected: the row label said "158 blocks"; the input actually
fragments into **171 blocks** (170 + sentinel), verified on both sides
(`bench.py` only asserts >100). And "36-core box" became "72-thread box"
(`nproc` = 72, matching `analysis.md`).

### Losses stayed losses

The two rows the old table called losses (unrelated small input, b2j)
are still losses, with the same wording — b2j's explanation (encode both
sequences + junk flags before the counting sort) is unchanged and still
correct. The `find_longest_match` row, previously claimed as 1.30x
*faster*, is now honestly shown as **0.83x, a loss in bench form**.
This is a row whose sign changed under measurement, not a rewording: in
bench form each rep constructs a fresh matcher, so the ~10 ms Mojo
construct cost (measured separately: 9.6 ms vs 2.5 ms ref) dominates the
~0.1 ms compiled scan. The README now says so explicitly, and notes the
kernel-only split measured in this session (0.12 ms vs ~6.0 ms on an
existing matcher, ~51x) so the loss is not mistaken for a slow kernel.
`bench/analysis.md` already reported this row as a near-miss/loss in
bench form (0.86x) — the old README was the outlier.

## Fairness checks

1. **Same input both sides**: yes. Every case in `bench/bench.py` builds
   `a`/`b` once and passes the same objects to both sides; verified by
   reading the file and by the per-case equality asserts (9 asserts,
   all comparing the two results before any timing).
2. **No skipped work on either side**: checked both directions.
   - Caching: both matchers cache `get_matching_blocks` (identity `is`
     on repeat calls) — but the bench constructs a *fresh* matcher per
     rep on both sides, so the cache never serves a timed rep. Same for
     `find_longest_match` (no memoisation either side: repeat calls
     re-scan) and the ratios.
   - `b2j build` row: the reference's timed fn is
     `SequenceMatcher(None, a, b)` + `set_seq2(b)`. Measured that the
     ref `__init__` already builds `b2j`, and `set_seq2` early-outs on
     `if b is self.b` for the identical object, so the extra call is a
     no-op — the row is construct-vs-construct, one index build each.
     This does **not** flatter either side.
   - `get_matching_blocks` row splits (measured, best-of-5): ref
     construct-only 1.3 ms vs mojo 5.5 ms; construct+blocks 642 ms vs
     20 ms. Construction is inside both numbers, symmetric.
   - The one asymmetry found is not in the bench but in the old README:
     the `find_longest_match` "1.30x faster" figure could only be a
     kernel-only number; on the bench form (fresh construct per rep)
     the port loses. The table now discloses this.
3. **Correctness gate is real**: all six cases `assert` result equality
   before timing (b2j dict, blocks, longest match, close-matches,
   three ratios). A wrong-but-fast kernel fails the run. Confirmed by
   inspection; no case times a partial result.

## Test audit (`git log -p -- tests/`)

History of `tests/`: `7681926` (initial, 26 test fns, all strict
`==` against `difflib`) → `5370fc3` (TEST phase: added 13 test fns,
444 insertions / 7 deletions across `tests/` + `_matcher.py` +
`kernels.mojo`).

- **No test function was deleted** (old 26 names all present; 13 added).
- **No assertion was deleted or loosened.** The only 7 deletions in
  `tests/` are the six exact-equality lines inside
  `test_random_blocks_opcodes_ratios`, replaced by a call to the new
  `_check_all(r, m)` helper, which re-asserts *all six* of those exact
  equalities plus `assert m._b2j_dict() == r.b2j`, and then adds
  tolerance-form repeats on top. Nothing was converted from `==` to
  `approx`.
- The `RATIO_TOL = 1e-12` tolerance form (`assert_ratio_close`) is
  **additive**: it appears *in addition to* exact `==` on the main
  paths (`_check_all`, `test_random_blocks_opcodes_ratios`,
  `test_empty_sequences`). The tolerance-only tests
  (`test_ratios_use_documented_tolerance`, etc.) are new tests, and the
  ratios are bit-exact in practice: 2,000 random pairs this session,
  0/6000 mismatches on exact `==` for all three ratios. A 1e-12
  tolerance cannot be masking a real drift — drift at that scale would
  be 1 ulp; any genuine divergence fails the exact asserts that remain.
- The TEST phase also shipped two code fixes alongside the tests (the
  Python fallback for out-of-bounds `find_longest_match` windows and
  the `reset_n` clamp in `kernels.mojo`), covered by the new
  out-of-range/window tests. No code changes in this VERIFY phase:
  nothing failed, so the "fix a bug you can show with a failing test"
  rule left the code untouched.

Nothing to restore. (The suite is 78 tests; the README's "64 tests"
stale count was also fixed.)

## What changed

- `README.md`: benchmark table replaced with the canonical run above
  (all six rows), "158 blocks" → "171 blocks", "36-core" → "72-thread"
  box note, "two losses" → "three losses", new paragraph explaining
  the find_longest_match bench-form loss with its measured kernel split,
  "1.6 s row" → "~0.6 s row", "64 tests" → "78 tests". Loss rows kept
  as losses; no row removed or reworded to look better.
- `bench/verify.md`: this file.
- No other files. No code, no bench, no test changes.

## Commands (all this session)

- `pixi run build && pixi run test` → green, 78 passed.
- `pixi run bench` → green (4+ runs; canonical run quoted above; first
  run quoted in "Gate runs").
- Row reruns: `pixi run python` harness importing `bench.bench` cases
  (median of 3), construction/kernel splits, the 2,000-pair exact-ratio
  check, `git log -p -- tests/`, `git diff 7681926..5370fc3 -- tests/`.
- Final `pixi run build && pixi run test` → green, 78 passed; final
  `pixi run build && pixi run test && pixi run bench` → build and test
  green, bench exit 0:
  `b2j build 2.82ms / 10.78ms / 0.26x; get_matching_blocks 635.45ms /
  20.34ms / 31.24x; unrelated 0.57ms / 1.05ms / 0.55x; find_longest_match
  8.90ms / 10.52ms / 0.85x; get_close_matches 31.90ms / 18.96ms / 1.68x;
  all three ratios 8.13ms / 4.43ms / 1.84x`.
