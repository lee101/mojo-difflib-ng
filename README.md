# mojo-difflib-ng

The Ratcliff/Obershelp matcher of [difflib-ng](https://github.com/inoio/difflib-ng)
with the scan, the index and the similarity ratios implemented in
[Mojo](https://www.modular.com/mojo).

```python
import difflib
import mojo_difflib_ng as mdn

s = mdn.SequenceMatcher(None, "qabxcd", "abycdf")
s.get_opcodes()
# [('delete', 0, 1, 0, 0), ('equal', 1, 3, 0, 2), ('replace', 3, 4, 2, 3),
#  ('equal', 4, 6, 3, 5), ('insert', 6, 6, 5, 6)]
s.ratio()          # 0.75
mdn.get_close_matches("appel", ["ape", "apple", "peach", "puppy"])
# ['apple', 'ape']
```

The Python package is `mojo_difflib_ng`, so it installs and imports alongside
the real `difflib` and the tests compare the two directly.

## What difflib-ng is, and what the tests compare against

difflib-ng is a drop-in replacement for CPython's `difflib` whose only
substantive change is that `SequenceMatcher` is reimplemented in C. Its
observable surface is therefore difflib's: the same queue, the same
tie-breaks in `find_longest_match`, the same adjacency collapse, the same
ratio arithmetic.

**difflib-ng itself is not installable on this toolchain.** It has been
withdrawn from PyPI (`/pypi/difflib-ng/json` is a 404, and its name is absent
from the simple index), and its source host is unreachable from here. So the
parity tests run against CPython's own `difflib`, which is the algorithm
difflib-ng ports rather than a stand-in for it. Every assertion is an equality
against that reference, never against a stored blob. If difflib-ng were
installed, the same tests would be the ones to run against it.

## Covered subset

| area | implemented API | kernel |
| --- | --- | --- |
| Element index | `set_seq1` / `set_seq2` / `set_seqs` | `dn_bincount`, `dn_build_b2j` |
| Longest match | `find_longest_match` | `dn_find_longest_match` |
| Whole diff | `get_matching_blocks` | `dn_match_blocks` |
| Similarity | `ratio`, `quick_ratio`, `real_quick_ratio` | `dn_ratio`, `dn_quick_ratio` |
| Batch scoring | `get_close_matches` | `dn_close_matches_range` |
| Edit script | `get_opcodes`, `get_grouped_opcodes` | -- (plain Python over the blocks) |

`real_quick_ratio` is `2 * min(len(a), len(b)) / (len(a) + len(b))`: one `min`
and one division, so there is nothing to compile and it is a Python expression.

Not implemented, and forwarded to the real `difflib` because they are text
formatting rather than compute: `ndiff`, `unified_diff`, `context_diff`,
`HtmlDiff`, `Differ`, and the `*_bytes` variants. Also not ported:
`DiffMatchPatch`, the character-level edit-distance variant difflib-ng also
ships. That is a different algorithm (Myers diff plus semantic cleanup) with no
Ratcliff/Obershelp content, and porting it is a second project, not a subset
of this one.

## Install and run

The repository pins its own Mojo toolchain:

```bash
bash build/build.sh                            # -> dist/libmojo-difflib-ng.so
PYTHONPATH=python python -m pytest tests -q
PYTHONPATH=python python bench/bench.py
```

## Performance

Best-of-N wall clock, one process, on a shared 72-thread box (Intel Xeon
E5-2697 v4, 2x18 cores; see `bench/verify.md` for the session this table
was last measured in). The reference is CPython's `difflib` -- the
pure-Python form of exactly this work, not a strawman. Every case verifies
the two agree on the result before it times anything, so a kernel that got
faster by being wrong fails the run. Each row constructs a fresh
`SequenceMatcher` per repetition on both sides, so construct cost is inside
every number below.

| case | difflib | mojo-difflib-ng | result |
| --- | ---: | ---: | ---: |
| get_matching_blocks, 10867 chars, 40 edits, 171 blocks | 645.15 ms | 19.63 ms | **32.87x faster** |
| all three ratios, 20000 chars | 7.76 ms | 4.30 ms | 1.81x faster |
| get_close_matches, 2000 candidates x 40 chars, 8 threads | 30.21 ms | 19.38 ms | 1.56x faster |
| get_matching_blocks, 2000 unrelated chars | 0.64 ms | 1.05 ms | **0.61x, a loss** |
| find_longest_match, 20000 chars | 8.40 ms | 10.08 ms | **0.83x, a loss** |
| b2j index build, 21797 chars | 2.80 ms | 11.88 ms | **0.24x, a loss** |

Three rows in this table are losses, and they are worth being precise about.

`get_matching_blocks` is where the port earns its keep: difflib's inner loop
is a Python dict rebuilt on every position, and the compiled version does the
same work in flat Int32 arrays. The speedup grows with fragmentation, because
fragmentation is what makes the Python side rebuild the dict so often.

The **b2j index build is a loss**, and the reason is structural. difflib's
`set_seq2` does one dict operation per element of b. This shim has to intern
the elements of *both* sequences to integer codes before any of that, plus
build the junk flags, so it does strictly more Python-level per-element work
to produce the same index. The compiled counting sort is faster than difflib's
dict build, but it is not faster than the Python work wrapped around it. The
honest summary is: this port makes the scan faster and leaves the indexing
overhead where it was.

The 2000-unrelated-chars case is a small-input loss, where per-call overhead
(scratch allocation, the ctypes boundary, index construction) is the whole
cost and the scan itself is a couple of milliseconds.

The **find_longest_match row is a loss in bench form**: each repetition
constructs a fresh matcher, so the Mojo side's ~10 ms of construct cost
(`_encode_general` over both sequences plus the b2j build) is inside the
number while the compiled scan itself is ~0.1 ms. The reference pays only
~2 ms of construct cost. Call `find_longest_match` on an already-built
matcher and the port is ~50x faster on this input (0.12 ms vs ~6 ms,
measured in this session's splits); what the table shows is that the port
loses whenever construction is per-call, which is exactly the construct
cost the b2j row isolates.

The box is shared, so the sub-20 ms rows move noticeably between runs; the
~0.6 s `get_matching_blocks` row does not.

## How it works

All kernels live in `src/kernels.mojo`, one compilation unit, because shared
library build cost is largely fixed. `build/build.sh` compiles it with
`mojo build --emit shared-lib` into `dist/libmojo-difflib-ng.so`. Buffers cross
the C ABI as 64-bit addresses and are rebuilt in Mojo as
`Pointer[Int32, AnyOrigin[mut=True]]`, which keeps the exported symbols
non-parametric.

**Elements become codes.** The shim interns every element of both sequences
through one dict into dense `Int32`, so `a[i] == b[i]` is an integer compare
and an element's identity *is* its code. An element that never occurs in `b`
simply has no bucket, which is exactly what difflib's `b2j.get(a[i], nothing)`
does. Long `str` sequences take a vectorised path (code points into a `uint32`
array, `np.unique` for the distinct set and inverse index) so the per-element
Python cost is one dict lookup per *distinct* character; shorter strings and
non-string sequences use a plain loop, which is faster below a couple of
hundred characters.

**b2j is a counting sort.** difflib's `b2j` maps a non-junk, non-popular
element of `b` to its positions in ascending order. `dn_build_b2j` lays that out
as `start`/`items` flat arrays, which is what the inner scan walks without a
single hash lookup. The junk test has to be applied *while counting*, not only
while scattering: a bucket sized from the junk-inclusive count is wider than its
real contents and exposes uninitialised slots as if they were positions.

**`find_longest_match` runs in two passes.** CPython keeps a dict `j2len` of
the longest junk-free run ending at `a[i-1]` and `b[j]`, and replaces it with a
fresh dict on every `i`. A value written for this `i` must not be readable as
the previous `i`'s value, which a single array used read-write cannot do:
matching `"aaa"` against `"aa"` would grow a length-2 run at `i = 0` out of
nothing and report a match at `a = -1`. So pass one reads and pass two writes,
and `jgen[j]` records which iteration wrote `j` so that "was `j` present in the
previous iteration" is a stamp compare rather than a value compare. The stamp
is strictly increasing across the *whole* `get_matching_blocks` run, not per
sub-range, because one call drives many sub-ranges through the same arrays and
a bare `i` would let one sub-range read what another wrote under the same
number.

**`get_matching_blocks` runs the queue in Mojo.** An explicit stack, a bottom-up
merge sort over the collected triples (insertion sort below 24, because a
heavily fragmented diff approaches one block per element and an insertion sort
would go quadratic exactly there), and the adjacency collapse with the
`(len(a), len(b), 0)` sentinel, all in one call.

**`get_close_matches` is batched and threaded.** The reference loops in Python,
one `SequenceMatcher` per candidate. Here the whole candidate set is
concatenated into one code array and scored by a single exported
range-taking kernel, split across a `ThreadPoolExecutor` (ctypes releases the
GIL for the foreign call, so this genuinely parallelises). Each worker owns its
own scratch -- `dn_quick_ratio` eats its count table and the `j2len` arrays are
written in place -- so chunking cannot change a score. The kernel reproduces
difflib's `real_quick_ratio() >= cutoff and quick_ratio() >= cutoff and
ratio() >= cutoff` short-circuit exactly: a candidate that fails a cheap bound
gets ratio `0.0`, which is below any positive cutoff, so it cannot be kept.

## One deliberate oddity

`get_grouped_opcodes` is copied from difflib including a wart: it edits
`self.opcodes` in place while trimming the leading and trailing equal runs, so
calling it changes what a later `get_opcodes()` returns. That is CPython's
behaviour and a drop-in replacement has to reproduce it, surprise included. The
tests drive both sides through the same call order so the comparison stays
meaningful.

## Tests

64 tests, all parity against `difflib`. They are aimed at specific places the
compiled code can be wrong, not at coverage for its own sake:

- the documented answers from difflib's own docstrings;
- the `j2len` read-write aliasing described above (`"aaa"` vs `"aa"`);
- junk excluded from the core match but absorbed at the edges;
- autojunk on and off, on both sides of the 200-element size gate, with a case
  where the two genuinely disagree;
- a `b2j` index check that reconstructs difflib's dict and compares it;
- an element that occurs in neither sequence, so its code is past the end of
  the bucket arrays;
- a 198-character pair chosen to fragment into 25 blocks, which crosses the
  insertion-sort threshold into the merge sort;
- ordering and non-adjacency of the block list, which is the contract a broken
  sort breaks;
- batching: the threaded result against a one-at-a-time loop, at a cutoff, an
  `n`, and empty and single-candidate inputs.

## License

MIT, as upstream. See `LICENSE`.
