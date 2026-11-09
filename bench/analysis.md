# ANALYZE: where the time goes in mojo-difflib-ng

Measured map, not fixes. No algorithm was changed in this phase; every number
below was produced by a run in this session (honest best-of-N, one process, and
the official `pixi run bench` run). Shared 72-thread box, Intel Xeon E5-2697 v4
(2.30 GHz, 2 sockets x 18 cores), 251 GB RAM, numpy 2.5.1, Python 3.13.14.
Reference closure: CPython `difflib` (pure-Python Ratcliff/Obershelp) via
`bench/bench.py`; `difflib-ng` itself could not be run (404 on PyPI, not
importable). Measured host memory bandwidth: ~27.9 GB/s (40 MB numpy copy, 80 MB
counted as read+write).

## Official bench run (this session)

`pixi run bench` (flock-wrapped, no other benchmark running alongside):

```
case                                           difflib (ref)   mojo-difflib-ng    ratio
---------------------------------------------------------------------------------------
b2j build, 21797 chars                                2.78ms           11.12ms    0.25x
get_matching_blocks, 10867 chars                    633.96ms           20.67ms   30.67x
get_matching_blocks, 2000 unrelated                   0.54ms            1.01ms    0.53x
find_longest_match, 20000 chars                       8.73ms           10.16ms    0.86x
get_close_matches, 2000x40 (8t)                      30.51ms           17.43ms    1.75x
all three ratios, 20000 chars                         7.89ms            4.67ms    1.69x
```

Two losses (b2j, unrelated-small-input), one near-miss (find_longest_match bench
form, 0.86x), three wins. The bench form constructs a fresh `SequenceMatcher` on
every rep, so construct cost is inside every row; the splits in the table below
separate it from the kernel.

## Upstream availability

- `import difflib_ng`: **not available**. `pypi.org/pypi/difflib-ng/json` and
  the simple index both returned 404 this session; PIXI env has no pip itself.
  All "upstream" numbers here are CPython `difflib` (same algorithm, pure
  Python), plus numpy and plain-Python baselines measured in-session.
- Anything calling itself "vs upstream difflib-ng" elsewhere in the repo should
  be read as "vs CPython difflib" until a numbers run against real
  difflib-ng exists.

## Ranked hotspot table

Time is best-of-N in-session (bench.py repeats; splits use the same convention).
"bytes touched" and "flops" follow the arithmetic in the "How the bytes/flops
were counted" section below. f/b = flops per byte. Rows marked **[SLOWER]**
are slower than the CPython difflib reference for the same work.

| rank | function | input size | time (ms) | bytes touched | flops | flops/byte | bound | glue vs kernel | next phase |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `_encode_general` on both seqs (b2j bench) | 21,822 + 21,797 elems | 7.292 | 174,476 B (4 B/elem output only; Python input not flat) | 348,952 (8/elem) | 2.00 | compute | **glue** (per-element Python loop) | **none** (see note) |
| 2 | `dn_match_blocks` kernel (`get_matching_blocks`, 40 edits) | 10,867 / 10,875 chars, 171 blocks | 13.420 | 16,683,468 B (top-level lower bound; sub-ranges add more) | 4,200,000 (top-level lower bound) | 0.252 | memory | **kernel** | **parallel** (see note) |
| 3 | `_encode_general` on 2,000 candidates (close-matches bench) | 2,000 x 40 chars = 80,000 elems | 14.991 | 320,000 B (4 B/elem output only) | 640,000 (8/elem) | 2.00 | compute | **glue** (per-element Python loop) | **none** (see note) |
| 4 | `dn_find_longest_match`, autojunk OFF (out-of-bench probe) | 20,000 / 20,001 chars, buckets ~10k wide | 1,864 | 3.2e9 B (2e8 visits x 16 B) | 8e8 (4/visit) | 0.250 | memory | **kernel** | **parallel** (see note) |
| 5 | `dn_find_longest_match` (bench row) | 20,000 / 20,001 chars, autojunk purged to 1 live item | 0.105 | 320,028 B | 60,000 (3/elem) | 0.187 | memory | **kernel** (0.034 is `reset_j2`) | **none** |
| 6 | `_Scratch` alloc (construct) | nb=21,797 na=21,822, 27 buckets, 8 arrays | 0.079-0.252 | 1,567,812 B zeroed (j2/jgen/kbuf/stack/blocks/tmp/res/fullcount) | ~0 (zero-fill, not flops) | ~0 | memory | **glue** (numpy zeros) | **none** |
| 7 | `dn_build_b2j` kernel | nb=21,797, ncodes=27 | 0.071 | 196,285 B | 108,985 (5/elem) | 0.555 | memory | **kernel** | **none** (buried under glue) |
| 8 | `dn_bincount` kernel | nb=21,797, ncodes=27 | 0.028 | 87,300 B | 43,594 (2/elem) | 0.499 | memory | **kernel** | **none** |
| 9 | `np.fromiter` junk flags (`_never_junk` per-elem call) | nb=21,797 | 2.067 | 21,797 B output (Python input not flat) | unbilled here | — | compute | **glue** (per-element Python call) | **none** (see note) |
| 10 | `dn_close_matches_range`, 1 chunk whole set | 2,000 x 40 chars vs 40-char target | 1.107 | 1,781,500 B | 320,000 (4/elem) | 0.180 | memory | **kernel** | **none** (encode is 13x bigger) |
| 11 | `dn_quick_ratio` kernel | na=20,000, fullcount 9 | 0.032 | 80,036 B | 80,000 (4/elem) | 1.00 | memory | **kernel** (0.0014 us floor inside) | **none** |
| 12 | `dn_ratio` kernel | nblk=2 | 0.003 | 24 B | 6 | 0.250 | memory | **kernel** (94% is ctypes) | **none** |
| 13 | `_real_quick_ratio` (Python) | na=20,000 nb=5 | 0.000 | 0 | ~2 (min + div) | — | compute | **glue** | **none** |
| 14 | `get_opcodes` rebuild (171 blocks, Python only) | 171 blocks | 0.033 | 2,052 B output | ~1,026 (6/block) | 0.500 | memory | **glue** (pure Python, no kernel) | **none** |
| 15 | `get_grouped_opcodes` (171 blocks, Python only) | 171 blocks | 0.068 | 2,052 B output | ~1,800 | 0.88 | memory | **glue** (pure Python, no kernel) | **none** |
| 16 **[SLOWER]** | `SequenceMatcher` construct (b2j bench, full) `"b2j build"` | na=21,822 nb=21,797 | 10.093 vs difflib 3.458 (**0.34x**) | 174,476 + 21,797 + scratch 1,567,812 B ([same count as above]) | [same as above] | — | compute | **glue** (encode 7.29 + junk 2.07 + list 0.12 + scratch 0.08) | **none** |
| 17 **[SLOWER]** | `get_matching_blocks`, 2,000 unrelated, construct + blocks (bench form) | 2,000 / 2,000 chars, 1 block | 1.036 (construct 1.008) vs difflib 0.607 (**0.59x**) | 16,000 + 2,000 + scratch ~33,000 B | [kernels ~5/elem; construct ~8/elem] | — | compute | **glue** (construct) | **none** |
| 18 **[SLOWER]** | `find_longest_match`, construct + scan (bench form) | 20,000 / 20,001 chars | 9.458 (construct 9.353 + kernel 0.105) vs 8.397 (**0.89x**) | 160,004 + scratch 1.6 MB | — | — | compute | **glue** (construct) | **none** |
| 19 | `get_matching_blocks` (bench form, 40 edits) | 10,867 / 10,875 chars | 23.244 (construct 5.392 + cached-matcher 15.970, kernel 13.420) vs 634.106 (**27.3x**) | [same as rank 2] | [same as rank 2] | 0.252 | memory | **kernel** (83% of bench row) | **parallel** |
| 20 (baseline) | numpy `np.bincount` for `dn_bincount` | nb=21,797 | 0.050 | 87,164 B | ~43,594 | 0.500 | memory | numpy (kernel-grade) | **none** (kernel beat it: 0.028 vs 0.050) |
| 21 (baseline) | Python dict `setdefault` build for `dn_build_b2j` output | nb=21,797, ncodes=27 | 3.495 | unbilled (dict churn) | ~217,970 | — | compute | **glue** (pure Python) | **none** (kernel: 0.071, **49x faster**) |
| 22 (baseline) | numpy quick_ratio (`bincount + minimum`) | na=20,000 nb=5 | 0.038 | 160,000 B | ~80,000 | 0.500 | memory | numpy | **none** (kernel 0.032, 1.19x faster) |
| 23 (baseline) | encode candidates via concat + `np.unique` | 80,000 chars in 1 pass | 2.181 | 320,000 B + utf-32 320,000 B | ~640,000 | 1.00 | memory | numpy (kernel-grade) | **none** (vs glue encode: **6.9x faster** than rank 3) |
| 24 | ctypes floor (empty `dn_ratio`, precomputed addrs) | nblk=0 | 0.00141 ms/call | 0 | 0 | — | — | **glue** (call overhead) | **none** |

Notes: rank 1+3+9 "next phase = none" means this phase assigns no
unroll/simd/parallel/gpu: the win is in the Python seam (the vectorised numpy path
that already exists but is never reached for `SequenceMatcher` — see
sanity checks) and cannot be assigned to a Mojo kernel phase by its flops/byte.
They are measured here because they dominate the bench table and must not be
misread as "kernel slow". Rank 2+4 "parallel" is behind the f/b figures below.
Rank 19's bench-form headline (27.3x here, 30.7x official) is diluted by the
construct cost folded into it; rank 2 is the pure kernel row.

## How the bytes/flops were counted

Per kernel, bytes touched = buffer reads + writes actually presented at the ABI,
flops = the same instruction sketch the Mojo source is written as.

- `dn_bincount`: reads nb x 4 B codes, writes ncodes x 4 B counts (RMW).
  Flops 2/elem (range test + increment).
- `dn_build_b2j`: reads nb x 4 B codes + nb x 1 B junk, writes (ncodes+1) x 4 B
  start + nb x 4 B items (upper bound; real live items fewer under purge),
  4 B cursor RMW. Flops 5/elem (junk test, range test, add/bounds).
- `dn_find_longest_match` (bench/purged): reads na x 4 B codes, start/items
  negligible (1 live item), `reset_j2` writes nb x 4 x 2 (j2+jgen) = 160 KB at
  nb=20,001. Flops 3/elem (loads + stamp compare + write).
- `dn_find_longest_match` (autojunk OFF probe): ~2e8 bucket visits
  (na=20,000 x avg live slice ~10,000, the `0.86x` row's twin).
  16 B/visit (items 4 + jgen 4 + j2 4 + kbuf 4), 4 flops/visit.
- `dn_match_blocks` (rank 2): lower bound counts only the top-level scan.
  na=10,867 positions x avg in-window live-bucket slice ~96 (8 live buckets,
  widths 83-103; the other 19 of 27 codes are autojunk-purged) ~ 1.04M visits
  x 16 B, 4 flops. Sub-ranges add work, so real bytes/flops are *higher* at the
  same f/b; the 13.420 ms is the measured whole-kernel time, not a slice.
- `dn_quick_ratio`: reads na x 4 B, RMW fullcount. 4 flops/elem
  (bound check + dec + compare + add).
- `dn_ratio`: 3 x nblk x 4 B read; 2 flops/block + 1 divide.
- `dn_close_matches_range`: reads cand flat 80,000 x 4 B + offs/lens 4 KB, b
  codes 160 B, per-quicksurviving candidate (225/2,000) a fullcount refill
  (ncounts x 4 write + nb x 4 read) + full match scan on 40x40.
- Encode rows (rank 1/3): bytes are the output codes only (4 B/elem); Python
  input is not a flat buffer here so it is not counted. Flops 8/elem
  (hash + compare + table).
- Scratch/allocation bytes are zero-fill bytes (numpy `zeros`/`full`), not
  usable flops; not shown for GPU verdict purposes.

A kernel is **memory-bound** when flops/byte < 2 (below the ~2 flops/B rule the
project itself quotes: "A CPU-only port is a legitimate answer when a kernel is
below roughly 2 flops per byte") and **compute-bound** at or above. Every
kernel with a real inner loop measures 0.18-1.00 flops/byte, i.e. memory-bound
at this problem size. Opcode regrouping are pure-Python glue (no kernel), so
their flops/byte is shown for completeness only.

## Findings, with line anchors

### A. The two **[SLOWER]** losses are glue, not kernel

**b2j build (0.25x official, 0.34x here).** The compiled `dn_build_b2j` (0.071 ms)
is **49x faster** than the pure-Python dict build (3.495 ms) doing the same
work, and `dn_bincount` (0.028 ms) edges out numpy (0.050 ms). The loss is the
shim wrapped around them: `_encode_general`, a per-element Python
dict-lookup loop, runs **7.292 ms** on the two bench sequences, and the junk
flags run **2.067 ms** (`np.fromiter` calling `isjunk` once per element,
`_matcher.py:206`, even though `isjunk=None` here). Construct spends 9.359 ms
on glue + 0.099 ms on kernels; the kernels are 13% of the win and 87% of the
loss.

The chunk the README (README.md Performance, lines 94-101) calls "structural"
matches the measurement exactly ("does strictly more Python-level per-element
work"). The same text says the vectorised path "so the per-element Python cost
is one dict lookup per *distinct* character" — that path exists
(`_encode_text`, `_matcher.py:103`) but is **unreachable for `SequenceMatcher`**
(see below).

**Unrelated 2,000 (0.53x official, 0.59x here).** The kernel is 0.025 ms and
fully adequate; the bench form's loss is entirely the construct (1.008 ms,
encode + junk + scratch on 2,000 chars). Same story, no new cause.

**find_longest_match bench (0.86x official, 0.89x here).** Also construct
driven: construct 9.353 ms + purged kernel 0.105 ms. The purged kernel itself is
0.105 ms with `reset_j2` contributing 0.034 ms (32%). It is not the scan; the
construct is.

### B. The biggest *kernel* cost is the fragmented-match queue, and it is real

`dn_match_blocks` on the 40-edit fragmented case is 13.420 ms pure kernel
(83% of the bench row) and the official row is a 30.67x win over difflib. It
scales with fragmentation, not with length: 0 edits -> 0.36 ms, 40 edits ->
13.1 ms, 200 edits -> 19.2 ms at the same na=10,867. The reason is structural
to Ratcliff/Obershelp: every stack pop runs `flm` over its sub-range, and the
top-level lower-bound estimate is ~1M bucket visits; the queue multiplies that
across sub-ranges (the 171-block result needs ~341 pops). `dn_match_blocks` is
one ctypes call per matcher (16 args, `lib_match_blocks`, `match_blocks` in
`_lib.py:73`); the Python glue around it is 12 address extractions (0.004 ms
of 13.42 ms) plus `tolist`+`Match` rebuild (0.086 ms, 0.6%).

flops/byte 0.252 (memory-bound) double-edged: the inner loop is a chain of
dependent loads (`jgen` stamp compare then `j2` load then `kbuf` write/store),
which is why the same visit count runs 1.3 ns/visit vs 0.6 ns/visit a flat
copy achieves. A SIMD investigation does not apply (no contiguous vectorisable
op inside the chained loop); the sequential dependency is the limiting factor
and parallelising across sub-ranges is the only axis that changes the exponent.
That would be a big change (stack state replicated per thread + final sorted
merge), which is why this document names the phase rather than recommending it
cheaply.

**The autojunk-OFF probe (rank 4) is the same shape at its extreme:**
1e8-2e8 visits at 0.25 flops/byte, 1,864 ms measured, which no one hits in the
bench (`autojunk=True` purges the buckets and the purged kernel is 0.105 ms).
It exists to size the worst case behind "memory-bound flm": saturating 3.2 GB
of traffic at 1.7 GB/s effective.

### C. `get_close_matches`: the kernel is not the hotspot

End to end is 17.746 ms, of which the encode of the 2,000 candidates is
**14.991 ms (84%)** and the whole chunked kernel is **1.107 ms (6%)**.
Encode per candidate is `np.unique` on a 40-element subarray at ~7.5 us each
(2,000 calls, latency-bound, small array); the candidates never reach the
vectorised path anyway because each candidate is 40 < `_VECTORISE_AT=256`
(`_matcher.py:68`, `encode` at line 96). The concat+one-`np.unique` numpy
baseline is 2.181 ms of numpy for the same work — a 6.9x gap against the shim.
The threaded kernel buys nothing at this budget: 8 threads 17.746 ms measured
vs 16.410 ms forced 1-thread (thread pool spawn 0.525 ms, and the kernel
sum 1.338 ms sequential across the 8 chunks vs 1.107 ms single-chunk — Amdahl
vs spawn overhead loses). `_thread_count` caps at 8 threads
(`_matcher.py:549`) even on a 72-thread box, which caps the only legitimate
kernel lever, but at 1.1 ms no lever matters here.

### D. The ratios are fine and small; the row is a construct discount

`ratio` 0.007 ms (kernel 0.003 ms, ctypes floor 0.0014 ms), `quick_ratio`
0.034 ms (kernel 0.032 ms, 1.19x ahead of the numpy multiset baseline),
`real_quick_ratio` 0.000 ms (pure Python, correct). The bench row's 1.69x
officially is the same kernel work plus a 3.476 ms construct the bench repeats
fold into the timed lambda; difflib pays 11.718 ms for its construct + three
ratios, so the row is honestly won, mostly on construct.

Moe `ratio()` does rebuild `np.asarray(blocks).reshape(-1)` per call
(`_matcher.py:432`); at nblk=2 it costs 0.002 ms of the 0.007 ms wrapper. The
blocks are already in the kernel output buffer (`tmp`), so that copy is
avoidable glue, but at this size it is 0.002/4.67 ms of the row.

### E. No per-element ctypes re-entry anywhere

`grep` for lib calls in the Python shim found 7 exported kernel symbols
(`_lib.py:31`): `bincount`, `build_b2j`, `reset_scratch`,
`find_longest_match`, `match_blocks`, `ratio`, `quick_ratio`,
`close_matches_range` — eight call sites, each invoked once per matcher/call,
not once per element. The heaviest caller, `get_close_matches`, spawns the
ThreadPoolExecutor but the foreign call per chunk is one core call
(`_matcher.py:499`). Any next phase that "unrolls" a Python loop into a
kernel must not re-introduce a per-element ctypes crossing; the current code
does not have that disease. What it has instead is per-element Python in
*encoding* (rank 1/3) and in junk-flagging (rank 9) — both pure Python,
no ctypes involved. That is where the glue time actually is.

### F. Per-call allocations and copies

- `set_seq1`/`set_seq2` re-import `_Scratch` twice per construct: `set_seq1`
  calls `_rescratch` (`_matcher.py:188`), then `set_seq2` calls `_build_b2j`
  then `_rescratch` again (`_matcher.py:198`). For the b2j bench each
  `_Scratch(pb,na,n)` zeros 8 arrays = 1.57 MB in 0.079-0.252 ms; the
  throwaway first scratch wastes 0.08-0.25 ms per construct (1.6-2.5% of a
  b2j row, largest share of anything in the unrelated row after encode).
  Zero-fill is required (`j2`/`jgen` start values), so the bytes are
  unavoidable; the double alloc is not.
- `counts.sum()` to size `items` (`_matcher.py:219`): 1.44 us, negligible.
- `i32()` (`_lib.py:84`) is `np.ascontiguousarray(..., dtype=int32)` and is
  a no-op (returns the same object, `is` check True) when encode already
  returned int32; no copies here.
- `quick_ratio()` copies `_fullcount` per call (`_matcher.py:445`, because the
  kernel consumes it destructively): 36 B (nbuckets=9) or 108 B (27) = 0.4 us;
  unavoidable under the destructive design, negligible.
- `close_matches` makes 3 float64 scratch buffers (24 KB total), 8 `_Scratch`
  sets (0.04 ms total); the allocation is `i32(np.concatenate([...]))`
  (80,000 codes) once per call — that is the flat buffer, not redundant
  copies.
- Nothing in the kernel path re-allocates per candidate; `dn_close_matches_range`
  reuses `fullcount` by refilling it in-kernel (ncounts clear + nb scan per
  candidate, negligible vs the 1.1 ms total).

### G. Bytes, time and the verdicts per function

- `dn_bincount` 0.499 f/b, `dn_build_b2j` 0.555 f/b — not GPU candidates, and
  both are <1% of their rows anyway.
- `dn_match_blocks` 0.252 f/b, purged `dn_find_longest_match` 0.187 f/b —
  memory-bound at bench sizes. The top-level slice is the lower bound; the
  real traffic is higher at the same f/b, which is why the measured kernel
  time (13.42 ms) sits 22x above copy-time-at-BW for that slice.
- `dn_find_longest_match` OFF 0.25 f/b — the same memory-bound shape at
  3.2 GB of traffic.
- `dn_quick_ratio` 1.00 f/b, `dn_ratio` 0.25 f/b, `dn_close_matches_range`
  0.180 f/b — all at or below 1.0 f/b, no GPU verdict survives the number.
- No kernel in this port reaches even 1.0 flops/byte. By the project's own
  rule quoted above (roughly 2 flops/byte as the GPU-worthiness line), **no
  GPU phase is warranted for any function** in this analysis. The honest
  assignment is: **parallel** for the two shapes where sub-range fan-out
  exists (`dn_match_blocks` queue, `dn_find_longest_match` scan), **none**
  (or wiring, not compute) for the glue rows. No `simd` or `unroll` is
  named anywhere, because the loops that have a serial dependency (j2len
  chain, stack queue) cannot be unrolled and there is no contiguous reduction
  big enough to vectorise; the loops that could be vectorised
  (bincount counting, quick_ratio counting) already run inside the kernel and
  are <1% of their rows.

## Per-entry-point table — public API at real caller sizes

| function | sizes real callers pass | time (ms) | bytes touched | flops | f/b | bound | glue / kernel |
|---|---|---|---|---|---|---|---|
| `SequenceMatcher` construct | 10,867-21,822 chars (bench); 2,000 chars unrelated | 5.392-10.093 (b2j); 1.008 (unrelated); 9.353 (flm) | 174,476 + scratch ~1.57 MB | ~349k | ~2.0 (encode) | compute | **glue** |
| `set_seq1` / `set_seq2` | same | inside construct (encode 7.292 + junk 2.067) | same | same | same | compute | **glue** |
| `_build_b2j` (helper) | ncodes 27-28 | 0.099 (kernels) inside construct | 283,585 | 152,579 | 0.54 | memory | **kernel** |
| `find_longest_match` | 20,000 / 20,001 (bench); 10,867 blk | 0.105 (purged, kernel); 0.402 (top-level gmb range); 1,864 (OFF probe) | 320,028; 16.7 MB lower bound; 3.2 GB | 60k; 4.2M; 8e8 | 0.187; 0.252; 0.250 | memory | **kernel** |
| `get_matching_blocks` | 10,867 / 10,875, 171 blocks | 13.420 kernel; 15.970 cached-matcher; 23.244 bench | 16.7 MB+ | 4.2M+ | 0.252 | memory | **kernel** |
| `ratio` | nblk=2 (bench), up to 171 | 0.007 wrapper / 0.003 kernel | 24 B | 6 | 0.250 | memory | 94% glue (ctypes) |
| `quick_ratio` | na=20,000, nbuckets=9 | 0.034 wrapper / 0.032 kernel | 80,036 B | 80,000 | 1.00 | memory | kernel |
| `real_quick_ratio` | same | 0.000 | 0 | ~2 | — | compute | glue (pure Python) |
| `get_close_matches` | 2,000 x 40 chars vs 40 | 17.746 (8t) / 16.410 (1t); kernel 1.107 | encode 320 KB; kernels 1.78 MB | encode 640k; kernels 320k | 2.00; 0.180 | compute→memory | **glue** (encode) |
| `get_opcodes` | 171 blocks | 0.033 | 2,052 B | ~1,026 | 0.50 | memory | glue (pure Python) |
| `get_grouped_opcodes` | 171 blocks | 0.068 | 2,052 B | ~1,800 | 0.88 | memory | glue (pure Python) |

## Caveats

- Sub-20 ms rows move by up to a factor of two between repeats on this shared
  box (the project says the same above in README.md Performance). The big
  rows (gmb 30.67x) do not move.
- Everything has been through the bench's correctness gate (`bench.py` checks
  parity before timing). No kernel was changed, so no numbers were lost to a
  wrong-but-fast rewrite.

## What a next phase should act on, in evidence order

1. **unblock the encode seam** (rank 1/3/9 — measured 9.4 ms of b2j's 10.1 ms and
   15.0 ms of close's 17.7 ms is per-element Python encode + junk flagging).
   The numpy vectorised path exists and, when actually fed str, is 6.3x faster
   than the general loop on 20k text; feeding it and batching candidates once
   at the top level are the fix. This is Python work, not a Mojo kernel phase,
   which is why `none` is the flops-based answer and the win is named
   `none` rather than `simd`.
2. **`dn_match_blocks` queue fan-out** (rank 2) — only if the following were to be
   attempted; f/b 0.252 cannot support GPU; parallelising across sub-ranges
   needs per-thread stack + merge and is a large change.
3. **`dn_find_longest_match` scan** (rank 5, purged) — none at bench sizes
   (0.105 ms); rank 4 (un-purged) is the shape that would justify parallel
   work (3.2 GB, 0.25 f/b), but autojunk prevents it in real callers.
4. **`dn_build_b2j`/`dn_bincount`** — none; already 49x/1.8x ahead of the
   Python/numpy baselines and <1% of the rows.
5. **GPU** — none, by flops/byte. No kernel crosses 1.0 f/b.

## Live-sanity checks (this session)

- `set_seq1`/`set_seq2` convert both inputs with `list(...)`
  (`_matcher.py:184,193`) before `[0]` encodes `self.a` (`self.a` is already a
  list), so `encode` always sees a `list`, never a `str`, for
  `SequenceMatcher` — the vectorised `_encode_text` path (and the
  `_VECTORISE_AT=256` threshold comment at `_matcher.py:65`) only fires for
  `get_close_matches`'s candidates, and there it fires never (40 < 256).
  The "vectorised" optimisation the README describes is currently dead code
  for both public entry points.
- `dn_find_longest_match` with autojunk purged has 1 live item total
  (`start=[0,0,0,1]`); with autojunk off the same input is 1,864 ms. The
  0.86x bench row is not the kernel; it is the construct around it.
- `bench/bench.py` ratios case: `_mutate` splits on words; `a` is a single
  20,000-char token with no spaces, so `b` ends up as a single word of length
  3-8 (`"the"` this run). na=20,000_nb=5 is the actual ratio geometry,
  not 20k vs 20k.
