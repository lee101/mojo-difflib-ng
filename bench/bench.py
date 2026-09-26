"""Correctness-gated benchmark for mojo-difflib-ng.

The reference is CPython's own ``difflib``, which is the same
Ratcliff/Obershelp algorithm difflib-ng exists to move into C. That is the
fairest available baseline: it is the pure-Python form of exactly this work,
not a strawman.

Every case checks that the two agree on the result before it times anything,
so a kernel that got faster by being wrong fails the run instead of posting a
good number. Timings are best-of-N wall clock in one process.
"""

from __future__ import annotations

import difflib
import pathlib
import random
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "python"))

import mojo_difflib_ng as M  # noqa: E402
from mojo_difflib_ng import _matcher  # noqa: E402


def _time(fn, repeats=5):
    """Best-of-N wall clock. The box is shared, so the fast cases take more
    samples: a single sample of a 6 ms case is mostly scheduler noise."""
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


_VOCAB = [
    "the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog", "and",
    "then", "runs", "away", "into", "forest", "where", "many", "other",
    "animals", "were", "waiting",
]


def _text(nwords: int, seed: int, vocab=None) -> str:
    vocab = vocab or _VOCAB
    rnd = random.Random(seed)
    return " ".join(rnd.choice(vocab) for _ in range(nwords))


def _mutate(text: str, edits: int, seed: int) -> str:
    """Substitute `edits` whole words, so the diff fragments like a real one."""
    rnd = random.Random(seed)
    words = text.split()
    for _ in range(edits):
        words[rnd.randrange(len(words))] = rnd.choice(_VOCAB)
    return " ".join(words)


# -- cases ----------------------------------------------------------------


def bench_b2j(nchars: int = 20_000):
    """Building the b2j index alone, against difflib's set_seq2 dict build."""
    a = _text(nchars // 5, 1)
    b = _text(nchars // 5, 2)

    ref = difflib.SequenceMatcher(None, a, b)
    ref.set_seq2(b)
    expect = {k: list(v) for k, v in ref.b2j.items()}

    m = M.SequenceMatcher(None, a, b)
    got = m._b2j_dict()
    assert got == expect, "b2j index mismatch"

    def run_ref():
        s = difflib.SequenceMatcher(None, a, b)
        s.set_seq2(b)

    def run_mine():
        s = M.SequenceMatcher(None, a, b)

    return f"b2j build, {len(b)} chars", _time(run_ref, 15), _time(run_mine, 15)


def bench_match_blocks(nwords: int = 2_000, edits: int = 40):
    """The whole Ratcliff/Obershelp queue on a realistic prose diff."""
    a = _text(nwords, 3)
    b = _mutate(a, edits, 4)

    ref = difflib.SequenceMatcher(None, a, b)
    expect = list(ref.get_matching_blocks())
    m = M.SequenceMatcher(None, a, b)
    got = list(m.get_matching_blocks())
    assert got == expect, "matching blocks differ"
    assert len(expect) > 100, "diff is not fragmented enough to be interesting"

    return f"get_matching_blocks, {len(a)} chars", _time(
        lambda: difflib.SequenceMatcher(None, a, b).get_matching_blocks(), 3
    ), _time(lambda: M.SequenceMatcher(None, a, b).get_matching_blocks(), 3)


def bench_match_blocks_unrelated(nchars: int = 2_000):
    """Worst case: no long common run, so the queue recurses into many ranges."""
    rnd = random.Random(5)
    alpha = "abcdefghijklmnopqrstuvwxyz"
    a = "".join(rnd.choice(alpha) for _ in range(nchars))
    b = "".join(rnd.choice(alpha) for _ in range(nchars))

    ref = difflib.SequenceMatcher(None, a, b)
    expect = list(ref.get_matching_blocks())
    m = M.SequenceMatcher(None, a, b)
    assert list(m.get_matching_blocks()) == expect, "matching blocks differ"

    return f"get_matching_blocks, {nchars} unrelated", _time(
        lambda: difflib.SequenceMatcher(None, a, b).get_matching_blocks(), 15
    ), _time(lambda: M.SequenceMatcher(None, a, b).get_matching_blocks(), 15)


def bench_longest_match(nchars: int = 20_000):
    """One find_longest_match over a highly repetitive corpus, autojunk on.

    This is the inner scan at its most expensive: with popular elements purged
    the buckets are short but nearly every position of a is visited, and the
    j2len chain is long.
    """
    rnd = random.Random(6)
    a = "".join(rnd.choice("ab") for _ in range(nchars))
    b = a[: nchars // 2] + "c" + a[nchars // 2 :]

    ref = difflib.SequenceMatcher(None, a, b)
    expect = ref.find_longest_match()
    m = M.SequenceMatcher(None, a, b)
    assert m.find_longest_match() == expect, "find_longest_match differs"

    return f"find_longest_match, {nchars} chars", _time(
        lambda: difflib.SequenceMatcher(None, a, b).find_longest_match(), 9
    ), _time(lambda: M.SequenceMatcher(None, a, b).find_longest_match(), 9)


def bench_close_matches(ncand: int = 2_000, clen: int = 40):
    """Batch scoring of many candidates against one target.

    The Mojo side scores the whole candidate set in one compiled pass split
    over a thread pool; difflib scores them one at a time in Python.
    """
    rnd = random.Random(7)
    vocab = list("abcdefghijklmnopqrstuvwxyz")
    word = "".join(rnd.choice(vocab) for _ in range(clen))
    possibilities = [
        "".join(rnd.choice(vocab) for _ in range(clen)) for _ in range(ncand)
    ]
    # Keep the near-misses in so the cutoff does not reject the batch outright.
    for i in range(0, ncand, 10):
        possibilities[i] = word[: clen // 2] + possibilities[i][clen // 2 :]

    expect = difflib.get_close_matches(word, possibilities, 3, 0.6)
    got = M.get_close_matches(word, possibilities, 3, 0.6)
    assert got == expect, "get_close_matches differs"

    nthreads = _matcher._thread_count(ncand, clen)
    return (f"get_close_matches, {ncand}x{clen} ({nthreads}t)", _time(
        lambda: difflib.get_close_matches(word, possibilities, 3, 0.6), 7
    ), _time(lambda: M.get_close_matches(word, possibilities, 3, 0.6), 7))


def bench_ratios(nchars: int = 20_000):
    """ratio, quick_ratio and real_quick_ratio over a long pair."""
    rnd = random.Random(8)
    a = "".join(rnd.choice("abcde") for _ in range(nchars))
    b = _mutate(a, 50, 9)

    ref = difflib.SequenceMatcher(None, a, b)
    mine = M.SequenceMatcher(None, a, b)
    assert mine.ratio() == ref.ratio()
    assert mine.quick_ratio() == ref.quick_ratio()
    assert mine.real_quick_ratio() == ref.real_quick_ratio()

    def run_ref():
        s = difflib.SequenceMatcher(None, a, b)
        return s.ratio(), s.quick_ratio(), s.real_quick_ratio()

    def run_mine():
        s = M.SequenceMatcher(None, a, b)
        return s.ratio(), s.quick_ratio(), s.real_quick_ratio()

    return f"all three ratios, {nchars} chars", _time(run_ref, 5), _time(run_mine, 5)


CASES = (
    bench_b2j,
    bench_match_blocks,
    bench_match_blocks_unrelated,
    bench_longest_match,
    bench_close_matches,
    bench_ratios,
)


def main():
    print(f"{'case':<44}{'difflib (ref)':>16}{'mojo-difflib-ng':>18}{'ratio':>9}")
    print("-" * 87)
    for fn in CASES:
        label, ref, got = fn()
        ratio = ref / got if got else float("nan")
        print(f"{label:<44}{ref*1e3:>14.2f}ms{got*1e3:>16.2f}ms{ratio:>8.2f}x")


if __name__ == "__main__":
    main()
