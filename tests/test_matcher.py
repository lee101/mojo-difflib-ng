"""Parity tests for the compiled Ratcliff/Obershelp matcher.

difflib-ng is a C reimplementation of ``difflib.SequenceMatcher`` that is not
installable on this toolchain (it has been withdrawn from PyPI and its source
host is unreachable here), so the reference is CPython's own ``difflib``,
whose ``SequenceMatcher`` semantics difflib-ng reproduces exactly: same queue,
same tie-breaks in find_longest_match, same adjacency collapse. Every test
below is an equality against that reference, not against a stored blob, and
each one is aimed at a specific place the compiled code can be wrong.
"""

from __future__ import annotations

import difflib
import math
import random

import pytest

import mojo_difflib_ng as M

# The three ratios are 2.0 * matches / total in Float64 on both sides
# (difflib's _calculate_ratio, dn_ratio/dn_quick_ratio), so a ported
# kernel doing the same integer matches and the same division must agree
# to the last bit; the tolerance pins that contract without depending on
# exact float equality happening to hold on a future platform.
RATIO_TOL = 1e-12


def assert_ratio_close(matcher, ref, fn, msg=""):
    got = getattr(matcher, fn)()
    want = getattr(ref, fn)()
    assert got == pytest.approx(want, abs=RATIO_TOL), (fn, got, want, msg)


def both(isjunk, a, b, autojunk=True):
    return difflib.SequenceMatcher(isjunk, a, b, autojunk=autojunk), M.SequenceMatcher(
        isjunk, a, b, autojunk=autojunk
    )


def rand_case(rnd, alpha, maxlen=60):
    a = "".join(rnd.choice(alpha) for _ in range(rnd.randint(0, maxlen)))
    b = "".join(rnd.choice(alpha) for _ in range(rnd.randint(0, maxlen)))
    isjunk = rnd.choice([None, None, lambda x: x == " "])
    autojunk = rnd.choice([True, False])
    return a, b, isjunk, autojunk


# -- the algorithm's own documented answers --------------------------------


def test_documented_matching_blocks():
    # The example in difflib's get_matching_blocks docstring.
    r, m = both(None, "abxcd", "abcd")
    assert list(m.get_matching_blocks()) == list(r.get_matching_blocks()) == [
        M.Match(a=0, b=0, size=2),
        M.Match(a=3, b=2, size=2),
        M.Match(a=5, b=4, size=0),
    ]


def test_documented_find_longest_match():
    r, m = both(None, " abcd", "abcd abcd")
    assert m.find_longest_match() == r.find_longest_match() == M.Match(a=0, b=4, size=5)
    r, m = both(lambda x: x == " ", " abcd", "abcd abcd")
    assert m.find_longest_match() == r.find_longest_match() == M.Match(a=1, b=0, size=4)
    r, m = both(None, "ab", "c")
    assert m.find_longest_match() == r.find_longest_match() == M.Match(a=0, b=0, size=0)


def test_documented_opcodes_and_ratios():
    r, m = both(None, "qabxcd", "abycdf")
    assert m.get_opcodes() == r.get_opcodes() == [
        ("delete", 0, 1, 0, 0),
        ("equal", 1, 3, 0, 2),
        ("replace", 3, 4, 2, 3),
        ("equal", 4, 6, 3, 5),
        ("insert", 6, 6, 5, 6),
    ]
    r, m = both(lambda x: x == " ", "private Thread currentThread;",
                "private volatile Thread currentThread;")
    assert round(m.ratio(), 3) == round(r.ratio(), 3) == 0.866


# -- find_longest_match, which is where the j2len scan lives ---------------


def test_prefix_is_not_stripped():
    # difflib refuses to strip a common prefix because that would turn "ab"
    # into a worse match than it is. A kernel that stripped would find size 1.
    r, m = both(None, "ab", "acab")
    assert m.find_longest_match() == r.find_longest_match() == M.Match(a=0, b=2, size=2)


def test_j2len_does_not_read_its_own_write():
    # "aaa" against "aa": at i=0 the run length must stay 1, because j2len is
    # replaced by a fresh dict each iteration. A read-write single array grows
    # a run of 2 out of nothing and reports a match at a=-1.
    r, m = both(None, "aaa", "aa")
    assert m.find_longest_match() == r.find_longest_match() == M.Match(a=0, b=0, size=2)
    assert list(m.get_matching_blocks()) == list(r.get_matching_blocks())


def test_junk_is_absent_from_the_core_but_absorbed_at_the_edges():
    # The core match is junk-free, then adjacent matching junk is absorbed, so
    # the answer is longer than the longest junk-free run.
    r, m = both(lambda x: x == " ", " abcd", "abcd abcd")
    assert m.find_longest_match() == r.find_longest_match() == M.Match(a=1, b=0, size=4)
    r, m = both(lambda x: x == " ", "  ab  ", "  ab  ")
    assert m.find_longest_match() == r.find_longest_match() == M.Match(a=0, b=0, size=6)


def test_autojunk_purges_a_popular_code_from_b2j():
    # 'x' fills b, so autojunk drops it from the index and the long run is no
    # longer findable through a bucket at all. A kernel that ignores the purge
    # (or applies it with the wrong cutoff) matches the run anyway.
    a = "bcd" + "x" * 300
    b = "y" + "x" * 300 + "bcd"
    r_on, m_on = both(None, a, b, autojunk=True)
    r_off, m_off = both(None, a, b, autojunk=False)
    assert list(m_on.get_matching_blocks()) == list(r_on.get_matching_blocks())
    assert list(m_off.get_matching_blocks()) == list(r_off.get_matching_blocks())
    assert list(m_on.get_matching_blocks()) != list(m_off.get_matching_blocks())
    assert m_on.ratio() != m_off.ratio()


def test_autojunk_threshold_is_b_at_least_200():
    # 200 is the size at which the heuristic switches on, and n//100+1 is the
    # cutoff. Exercise both sides of the size gate.
    for n in (199, 200, 201):
        a = "a" * n
        b = "b" + "a" * n
        r, m = both(None, a, b, autojunk=True)
        assert m.ratio() == r.ratio()
        r, m = both(None, a, b, autojunk=False)
        assert m.ratio() == r.ratio()


def test_element_absent_from_b_has_no_bucket():
    # a's codes run past the number of buckets b defines; the kernel must skip
    # those rather than index out of the start/items arrays.
    r, m = both(None, "qwerty", "abc")
    assert m.find_longest_match() == r.find_longest_match() == M.Match(a=0, b=0, size=0)
    assert list(m.get_matching_blocks()) == list(r.get_matching_blocks())


def test_empty_sequences():
    for a, b in [("", ""), ("", "abc"), ("abc", ""), ("", " ")]:
        r, m = both(None, a, b)
        assert list(m.get_matching_blocks()) == list(r.get_matching_blocks())
        assert m.get_opcodes() == r.get_opcodes()
        assert m.ratio() == r.ratio()
        assert m.quick_ratio() == r.quick_ratio()
        assert m.real_quick_ratio() == r.real_quick_ratio()
        assert m.find_longest_match() == r.find_longest_match()
        # Tolerance form of the same parity (RATIO_TOL; see its comment).
        assert_ratio_close(m, r, "ratio")
        assert_ratio_close(m, r, "quick_ratio")
        assert_ratio_close(m, r, "real_quick_ratio")


# -- degenerate inputs ------------------------------------------------------


def test_single_element_and_duplicate_heavy_parity():
    # difflib's get_matching_blocks/get_opcodes/ratio/quick_ratio/
    # real_quick_ratio are exercised at the shortest lengths and on
    # maximally duplicated inputs, with the documented degenerate
    # answers pinned as well as compared to the reference.
    cases = [("a", "a"), ("a", "b"), ("aaaa", "aa"), ("aa", "aaaa"),
             ("ababab", "baba"), ("", "a"), ("a", ""), ("", "")]
    for a, b in cases:
        r, m = both(None, a, b)
        assert list(m.get_matching_blocks()) == list(r.get_matching_blocks())
        assert m.get_opcodes() == r.get_opcodes()
        assert m.find_longest_match() == r.find_longest_match()
        assert_ratio_close(m, r, "ratio")
        assert_ratio_close(m, r, "quick_ratio")
        assert_ratio_close(m, r, "real_quick_ratio")
    # Pinned degenerate answers (CPython difflib semantics, which
    # difflib-ng reproduces): identical singletons match for 1, distinct
    # singletons match for 0 but real_quick_ratio only looks at lengths.
    r, m = both(None, "a", "a")
    assert m.ratio() == 1.0 and m.quick_ratio() == 1.0
    assert m.real_quick_ratio() == 1.0 and m.ratio() == r.ratio()
    r, m = both(None, "a", "b")
    assert m.ratio() == 0.0 and m.quick_ratio() == 0.0
    assert m.real_quick_ratio() == 1.0, "2*min(1,1)/2 does not see content"
    assert m.ratio() == r.ratio() and m.quick_ratio() == r.quick_ratio()
    r, m = both(None, "", "")
    assert m.ratio() == 1.0 and m.quick_ratio() == 1.0
    assert m.real_quick_ratio() == 1.0, "2*0/0 -> 1.0 by _calculate_ratio"
    r, m = both(None, "aaaa", "aa")
    assert m.ratio() == pytest.approx(2 / 3, abs=RATIO_TOL)
    assert abs(m.ratio() - r.ratio()) <= RATIO_TOL
    assert abs(m.quick_ratio() - r.quick_ratio()) <= RATIO_TOL


def test_float_elements_nan_and_inf_parity():
    # difflib compares elements by hash/eq, so float NaN only ever
    # matches itself (same object: dict identity) and distinct NaN
    # objects never match; inf is an ordinary value. The port interns
    # by dict as well, so the same dict-key semantics apply. Comparing
    # against difflib pins both the "same object" and "distinct" cases.
    nan1, nan2 = float("nan"), float("nan")
    cases = [
        ([nan1], [nan1]),
        ([nan1], [nan2]),
        ([float("inf")], [float("inf")]),
        ([1.0, float("inf")], [float("inf"), 1.0]),
        ([nan1, 1.0], [1.0, nan1]),
        ([float("-inf")], [float("inf")]),
    ]
    for a, b in cases:
        r, m = both(None, a, b)
        assert list(m.get_matching_blocks()) == list(r.get_matching_blocks())
        assert m.get_opcodes() == r.get_opcodes()
        assert_ratio_close(m, r, "ratio")
        assert_ratio_close(m, r, "quick_ratio")
    # Same NaN object is the same dict key on both sides -> full match.
    r, m = both(None, [nan1], [nan1])
    assert m.ratio() == 1.0 and m.quick_ratio() == 1.0
    assert m.ratio() == r.ratio() == 1.0
    # Distinct NaN objects compare unequal in Python, so: no match.
    r, m = both(None, [nan1], [nan2])
    assert m.ratio() == 0.0 and m.quick_ratio() == 0.0
    assert m.ratio() == r.ratio() == 0.0


def test_mismatched_and_unhashable_elements_raise_like_difflib():
    # difflib builds b2j by hashing every element of b, so an unhashable
    # element of b and a non-iterable b raise TypeError; an unhashable
    # element of a only fails when the scan hashes it. Both
    # implementations raise the same class once the matcher is used,
    # so construction and first use are driven inside each block.
    for a, b in [(["a"], [[1]]), ([[1]], [[1]]), ([[1]], ["a"]),
                 (42, "abc"), ("abc", 42)]:
        with pytest.raises(TypeError):
            difflib.SequenceMatcher(None, a, b).get_matching_blocks()
        with pytest.raises(TypeError):
            M.SequenceMatcher(None, a, b).get_matching_blocks()


def test_non_string_sequence_types_parity():
    # difflib accepts any sequence of hashable elements; the port
    # interns lists/tuples/bytes/ranges the same way.
    for a, b in [(("a", "b", "c"), ("b", "c", "d")), (b"abc", b"bc"),
                 (range(5), range(5)), ([1, 2, 3], [2, 3])]:
        r, m = both(None, a, b)
        assert list(m.get_matching_blocks()) == list(r.get_matching_blocks())
        assert m.get_opcodes() == r.get_opcodes()
        assert_ratio_close(m, r, "ratio")
        assert_ratio_close(m, r, "quick_ratio")


# -- get_matching_blocks: the queue, the sort and the collapse -------------


def _check_all(r, m):
    assert list(m.get_matching_blocks()) == list(r.get_matching_blocks())
    assert m.get_opcodes() == r.get_opcodes()
    assert m.ratio() == r.ratio()
    assert m.quick_ratio() == r.quick_ratio()
    assert m.real_quick_ratio() == r.real_quick_ratio()
    assert m.find_longest_match() == r.find_longest_match()
    assert_ratio_close(m, r, "ratio")
    assert_ratio_close(m, r, "quick_ratio")
    assert_ratio_close(m, r, "real_quick_ratio")
    assert m._b2j_dict() == r.b2j


@pytest.mark.parametrize("alpha", ["ab", "abcde ", "abcdefghij"])
@pytest.mark.parametrize("seed", range(12))
def test_random_blocks_opcodes_ratios(alpha, seed):
    rnd = random.Random(seed * 977 + len(alpha))
    for _ in range(60):
        a, b, isjunk, autojunk = rand_case(rnd, alpha)
        r, m = both(isjunk, a, b, autojunk=autojunk)
        _check_all(r, m)


def test_blocks_are_sorted_and_non_adjacent():
    # difflib guarantees the blocks are monotonically increasing and that
    # adjacent equal blocks have been collapsed. A sort that merely permutes
    # the array, or a collapse that is skipped, breaks both invariants.
    r, m = both(None, "xabcyabzabc", "abqabyabwabc")
    blocks = list(m.get_matching_blocks())
    assert blocks == list(r.get_matching_blocks())
    for p, q in zip(blocks, blocks[1:-1]):
        assert (p.a, p.b) < (q.a, q.b)
        assert not (p.a + p.size == q.a and p.b + p.size == q.b)
    assert blocks[-1] == M.Match(a=len("xabcyabzabc"), b=len("abqabyabwabc"), size=0)


def test_many_blocks_sort_path():
    # Two unrelated binary runs fragment into one block per element, which
    # crosses the insertion-sort threshold into the merge sort. A merge that
    # drops, duplicates or mis-orders a run shows up as a wrong block list.
    # Two unrelated binary runs of 198 characters, chosen because they
    # fragment into 25 matching blocks. Ratcliff/Obershelp is good enough on
    # small alphabets that this needs a search to find, hence the fixture.
    a = "ababbabbaaabaaaababbaaaabbababbaababbabbbabbbaabbbabbaaaaabaabaaababbbbbaabbabaabababbbaababbaabbababbaaabaaabbaaabbabbabbaaabaaaabaabbaaaababbabbbabbabbabbbbbaaabaabbbaabababbbbababaaaabbbbbbbabbbb"
    b = "abbbbbbabaaaaaabaaabbbbbbaabbbabbbaaaaabbbabaabbababbaabaabaaabbbabaababaabaaaabbbaababbbbbaababbbbbbaabbaaaabbbabababaabbaabbaababbbbaaaabaabaabbaaaabbabbabbaaababbbabaabbaabbaabaabbabaabbbabaabbbb"
    r, m = both(None, a, b)
    assert len(list(m.get_matching_blocks())) == 26  # 25 blocks + sentinel
    assert list(m.get_matching_blocks()) == list(r.get_matching_blocks())
    # A merge that lost or duplicated a run would break the ordering the
    # matching-block contract guarantees.
    blocks = list(m.get_matching_blocks())
    for p, q in zip(blocks, blocks[1:]):
        assert (p.a, p.b) <= (q.a, q.b)


def test_token_sequences():
    # Elements need not be characters; they are interned, so identity is the
    # code and any hashable works.
    vocab = ["alpha", "beta", "gamma", "delta", "eps", "zeta"]
    rnd = random.Random(31)
    for _ in range(120):
        a = [rnd.choice(vocab) for _ in range(rnd.randint(0, 50))]
        b = [rnd.choice(vocab) for _ in range(rnd.randint(0, 50))]
        isjunk = rnd.choice([None, None, lambda x: x == "eps"])
        r, m = both(isjunk, a, b)
        assert list(m.get_matching_blocks()) == list(r.get_matching_blocks())
        assert m.get_opcodes() == r.get_opcodes()
        assert_ratio_close(m, r, "ratio")
        assert_ratio_close(m, r, "quick_ratio")


def test_grouped_opcodes():
    a = "\n".join(f"line {i}" for i in range(40))
    b = "\n".join(f"line {i}" for i in range(40)).replace("line 20", "LINE 20")
    r, m = both(None, a, b)
    # difflib's get_grouped_opcodes edits the cached opcode list in place while
    # trimming the leading and trailing equal runs, so both sides are driven
    # through the same call order and the parity is only meaningful in step.
    for n in (0, 1, 3, 8):
        assert [list(g) for g in m.get_grouped_opcodes(n)] == [
            list(g) for g in r.get_grouped_opcodes(n)
        ]
        assert m.get_opcodes() == r.get_opcodes()


def test_grouped_opcodes_documented_example():
    # The example in difflib's get_grouped_opcodes docstring: an
    # insertion, a replacement, a deletion and another replacement over
    # 39 elements. The exact groups are pinned AND compared, so a change
    # to the context-trimming maths fails against the documented answer
    # as well as against the reference.
    a = [str(i) for i in range(1, 40)]
    b = a[:]
    b[8:8] = ["i"]
    b[20] += "x"
    b[23:28] = []
    b[30] += "y"
    expected = [
        [("equal", 5, 8, 5, 8), ("insert", 8, 8, 8, 9), ("equal", 8, 11, 9, 12)],
        [("equal", 16, 19, 17, 20), ("replace", 19, 20, 20, 21),
         ("equal", 20, 22, 21, 23), ("delete", 22, 27, 23, 23),
         ("equal", 27, 30, 23, 26)],
        [("equal", 31, 34, 27, 30), ("replace", 34, 35, 30, 31),
         ("equal", 35, 38, 31, 34)],
    ]
    r, m = both(None, a, b)
    assert [list(g) for g in m.get_grouped_opcodes()] == expected
    assert [list(g) for g in r.get_grouped_opcodes()] == expected
    # Identical inputs: difflib emits no groups (the single remaining
    # group would be all-equal).
    r, m = both(None, "abc", "abc")
    assert [list(g) for g in m.get_grouped_opcodes()] == [
        list(g) for g in r.get_grouped_opcodes()
    ] == []
    r, m = both(None, "", "")
    assert [list(g) for g in m.get_grouped_opcodes()] == [
        list(g) for g in r.get_grouped_opcodes()
    ] == []


# -- ratios ---------------------------------------------------------------


def test_quick_ratio_is_an_upper_bound():
    rnd = random.Random(3)
    for _ in range(200):
        a = "".join(rnd.choice("abcd") for _ in range(rnd.randint(0, 40)))
        b = "".join(rnd.choice("abcd") for _ in range(rnd.randint(0, 40)))
        r, m = both(None, a, b)
        assert m.quick_ratio() == r.quick_ratio()
        assert m.real_quick_ratio() == r.real_quick_ratio()
        assert_ratio_close(m, r, "quick_ratio")
        assert_ratio_close(m, r, "real_quick_ratio")
        # Both cheap ratios bound ratio from above. real_quick_ratio is not
        # itself bounded by quick_ratio: it only looks at the lengths.
        assert m.ratio() <= m.quick_ratio() + 1e-12
        assert m.ratio() <= m.real_quick_ratio() + 1e-12


def test_quick_ratio_does_not_consume_the_fullcount():
    # dn_quick_ratio eats its count table. Calling it twice must give the same
    # answer, and so must interleaving it with a full match.
    m = M.SequenceMatcher(None, "abcdab", "abdcab")
    first = m.quick_ratio()
    assert m.quick_ratio() == first
    m.get_matching_blocks()
    assert m.quick_ratio() == first
    assert first == difflib.SequenceMatcher(None, "abcdab", "abdcab").quick_ratio()


def test_ratio_of_identical_and_disjoint():
    m = M.SequenceMatcher(None, "abcdef", "abcdef")
    assert m.ratio() == 1.0
    m = M.SequenceMatcher(None, "abc", "xyz")
    assert m.ratio() == 0.0


# -- get_close_matches ----------------------------------------------------


@pytest.mark.parametrize("cutoff", [0.0, 0.3, 0.6, 0.9])
def test_close_matches_matches_stdlib(cutoff):
    word = "appel"
    possibilities = ["ape", "apple", "peach", "puppy"]
    assert M.get_close_matches(word, possibilities, cutoff=cutoff) == (
        difflib.get_close_matches(word, possibilities, cutoff=cutoff)
    )


def test_close_matches_ties_break_on_the_candidate():
    # heapq.nlargest over (score, candidate) means equal scores are ordered by
    # the candidate itself. Both sides have to agree on which one that is.
    word = "ab"
    possibilities = ["ab", "ba", "aa", "bb"]
    assert M.get_close_matches(word, possibilities, n=4) == (
        difflib.get_close_matches(word, possibilities, n=4)
    )


def test_close_matches_rejects_bad_arguments():
    with pytest.raises(ValueError):
        M.get_close_matches("a", ["b"], n=0)
    with pytest.raises(ValueError):
        M.get_close_matches("a", ["b"], cutoff=1.5)
    with pytest.raises(ValueError):
        M.get_close_matches("a", ["b"], cutoff=-0.1)
    # The same ValueErrors come out of difflib's `not n > 0` /
    # `not 0.0 <= cutoff <= 1.0` guards, including for NaN: NaN fails
    # both comparisons there, and the port's identical guards must too.
    with pytest.raises(ValueError):
        difflib.get_close_matches("a", ["b"], cutoff=float("nan"))
    with pytest.raises(ValueError):
        M.get_close_matches("a", ["b"], cutoff=float("nan"))
    with pytest.raises(ValueError):
        difflib.get_close_matches("a", ["b"], cutoff=float("inf"))
    with pytest.raises(ValueError):
        M.get_close_matches("a", ["b"], cutoff=float("inf"))
    with pytest.raises(ValueError):
        difflib.get_close_matches("a", ["b"], n=0)
    with pytest.raises(ValueError):
        difflib.get_close_matches("a", ["b"], n=-2)


@pytest.mark.parametrize("cutoff", [0.0, 1.0])
def test_close_matches_cutoff_boundaries(cutoff):
    # cutoff=0.0 keeps every possibility (all ratios are >= 0, and
    # difflib's `>= cutoff` chain keeps even empty ones), cutoff=1.0
    # keeps only exact matches. n larger than the candidate list is
    # fine on both sides.
    poss = ["abc", "abd", "xyz", ""]
    assert M.get_close_matches("abc", poss, n=10, cutoff=cutoff) == (
        difflib.get_close_matches("abc", poss, n=10, cutoff=cutoff)
    )
    assert len(M.get_close_matches("abc", poss, n=10, cutoff=0.0)) == 4
    assert M.get_close_matches("abc", poss, n=10, cutoff=1.0) == ["abc"]
    # Duplicate candidates are preserved (nlargest over the list, not
    # over a set), and empty-string matches work.
    assert M.get_close_matches("", ["", ""], n=10, cutoff=0.0) == ["", ""]
    assert M.get_close_matches("abc", ["abc", "abc", "abd"], n=10) == (
        difflib.get_close_matches("abc", ["abc", "abc", "abd"], n=10)
    )
    # A one-shot iterator of possibilities is consumed exactly once on
    # both sides and yields the same answer.
    assert M.get_close_matches("abc", (x for x in ["abc", "abd", "xyz"])) == (
        difflib.get_close_matches("abc", (x for x in ["abc", "abd", "xyz"]))
    )
    # Duplicates with equal scores order by the candidate itself.
    assert M.get_close_matches("ab", ["ba", "ba", "ab"], n=3) == (
        difflib.get_close_matches("ab", ["ba", "ba", "ab"], n=3)
    )


def test_close_matches_n_and_cutoff_edge_types():
    # n=1 returns the single best; n larger than the candidate count
    # returns every keeper. Both are the documented n semantics
    # ("the maximum number of close matches to return").
    word, poss = "appel", ["ape", "apple", "peach", "puppy"]
    assert M.get_close_matches(word, poss, n=1) == difflib.get_close_matches(
        word, poss, n=1
    ) == ["apple"]
    assert M.get_close_matches(word, poss, n=99) == difflib.get_close_matches(
        word, poss, n=99
    )
    # A float n is not an int: both sides raise TypeError
    # (difflib: slice indices must be integers; the port's heapq
    # nlargest raises the same error on the same path).
    with pytest.raises(TypeError):
        difflib.get_close_matches("abc", ["abc"], n=2.5)
    with pytest.raises(TypeError):
        M.get_close_matches("abc", ["abc"], n=2.5)


def test_close_matches_batched_equals_one_at_a_time():
    # The batched kernel splits candidates across a thread pool. Chunking must
    # not change a score, and the cheap upper bounds must still short-circuit
    # exactly where difflib's `and` chain would.
    from mojo_difflib_ng import _matcher

    rnd = random.Random(19)
    word = "".join(rnd.choice("abcde") for _ in range(60))
    possibilities = [
        "".join(rnd.choice("abcde") for _ in range(rnd.randint(60, 120)))
        for _ in range(200)
    ]
    assert _matcher._thread_count(len(possibilities), 120) > 1
    one_at_a_time = [
        x for x in possibilities
        if difflib.SequenceMatcher(None, x, word).ratio() >= 0.6
    ]
    assert set(M.get_close_matches(word, possibilities, n=len(possibilities))) == set(
        one_at_a_time
    )


def test_close_matches_empty_and_single():
    assert M.get_close_matches("abc", []) == []
    assert M.get_close_matches("abc", ["abc"]) == ["abc"]
    assert M.get_close_matches("", ["", "a"]) == difflib.get_close_matches("", ["", "a"])


# -- plumbing -------------------------------------------------------------


def test_formatting_helpers_are_forwarded():
    assert list(M.ndiff("qabxcd", "abycdf")) == list(difflib.ndiff("qabxcd", "abycdf"))
    assert list(M.unified_diff(["a\n", "b\n"], ["a\n", "c\n"])) == list(
        difflib.unified_diff(["a\n", "b\n"], ["a\n", "c\n"])
    )
    assert M.IS_LINE_JUNK is difflib.IS_LINE_JUNK


def test_set_seqs_reuse():
    # get_close_matches drives the matcher the other way round, so set_seq1
    # after set_seq2 has to invalidate the b2j-derived caches correctly.
    m = M.SequenceMatcher()
    m.set_seq2("abcd")
    m.set_seq1("abxd")
    assert list(m.get_matching_blocks()) == list(
        difflib.SequenceMatcher(None, "abxd", "abcd").get_matching_blocks()
    )
    m.set_seq1("xabc")
    assert list(m.get_matching_blocks()) == list(
        difflib.SequenceMatcher(None, "xabc", "abcd").get_matching_blocks()
    )
    assert m.quick_ratio() == difflib.SequenceMatcher(None, "xabc", "abcd").quick_ratio()
    assert_ratio_close(m, difflib.SequenceMatcher(None, "xabc", "abcd"), "quick_ratio")


def test_set_seq1_keeps_fullbcount_set_seq2_rebuilds_it():
    # difflib's quick_ratio counts against fullbcount (b's multiset):
    # set_seq1 must NOT rebuild it, set_seq2 must. The two directions
    # are pinned against a fresh reference matcher.
    r, m = both(None, "ab", "ab")
    assert m.quick_ratio() == r.quick_ratio() == 1.0
    r.set_seq1("zz")
    m.set_seq1("zz")
    assert m.quick_ratio() == r.quick_ratio() == 0.0
    assert m.quick_ratio() == difflib.SequenceMatcher(
        None, "zz", "ab"
    ).quick_ratio(), "b's counts survive a set_seq1"
    r.set_seq2("zz")
    m.set_seq2("zz")
    assert m.quick_ratio() == r.quick_ratio() == 1.0
    assert m.quick_ratio() == difflib.SequenceMatcher(
        None, "zz", "zz"
    ).quick_ratio(), "set_seq2 rebuilds b's counts"


def test_set_seqs_and_result_caching():
    # set_seqs is set_seq1+set_seq2; cached get_matching_blocks /
    # get_opcodes return the same list object on both sides
    # (difflib returns its cached list itself, and so does the port),
    # invalidated by set_seq1 and set_seq2.
    r, m = both(None, "abcd", "bcde")
    assert m.get_matching_blocks() is m.get_matching_blocks()
    assert r.get_matching_blocks() is r.get_matching_blocks()
    assert m.get_opcodes() is m.get_opcodes()
    assert r.get_opcodes() is r.get_opcodes()
    r.set_seqs("bcde", "bcde")
    m.set_seqs("bcde", "bcde")
    assert m.ratio() == r.ratio() == 1.0
    assert m.get_opcodes() == r.get_opcodes()
    # Named-argument construction matches positional construction.
    m_kw = M.SequenceMatcher(isjunk=None, a="abcd", b="bcde")
    assert m_kw.ratio() == difflib.SequenceMatcher(
        None, "abcd", "bcde"
    ).ratio() == 0.75


def test_constructor_default_is_no_junk():
    m = M.SequenceMatcher(a="a", b="b")
    assert m.isjunk("a") is False
    m = M.SequenceMatcher(lambda x: x == "z", a="a", b="b")
    assert m.isjunk("z") is True


# -- b2j index and find_longest_match windows ------------------------------


def test_b2j_dict_matches_reference():
    # difflib's __chain_b maps each non-junk, non-popular element of b
    # to its positions, ascending. The port's counting sort lays the
    # same mapping out flat; _b2j_dict reconstructs it for comparison.
    # Junk purge, popular purge (autojunk at >= 200), empty b, bytes
    # keys, and an a-only element whose code has no bucket must agree.
    cases = [
        (None, "abc", "abc", True),
        (lambda x: x == " ", " ax bx ", " ax bx ", True),
        (None, "bcd" + "x" * 300, "y" + "x" * 300 + "bcd", True),
        (None, "bcd" + "x" * 300, "y" + "x" * 300 + "bcd", False),
        (None, "", "", True),
        (None, "a", "", True),
        (lambda x: x == "x", "k" * 10, "x" * 250 + "k" * 10, True),
        (None, b"abc", b"bc", True),
        (None, "zabc", "abc", True),
    ]
    for isjunk, a, b, autojunk in cases:
        r, m = both(isjunk, a, b, autojunk=autojunk)
        assert m._b2j_dict() == r.b2j, (a, b, autojunk)
        # The index must also be right through the public surface.
        assert list(m.get_matching_blocks()) == list(
            r.get_matching_blocks()
        )


def test_find_longest_match_windows():
    # Every in-range sub-window is compared on a small pair with junk
    # on and off: the tie-break (earliest a, then earliest b) and the
    # junk-absorption rules are per-window behaviour, not just
    # whole-sequence behaviour.
    rnd = random.Random(1234)
    for _ in range(40):
        la, lb = rnd.randint(0, 8), rnd.randint(0, 8)
        a = "".join(rnd.choice("ab ") for _ in range(la))
        b = "".join(rnd.choice("ab ") for _ in range(lb))
        for isjunk in [None, lambda x: x == " "]:
            r, m = both(isjunk, a, b)
            for alo in range(la + 1):
                for ahi in range(alo, la + 1):
                    for blo in range(lb + 1):
                        for bhi in range(blo, lb + 1):
                            assert m.find_longest_match(
                                alo, ahi, blo, bhi
                            ) == r.find_longest_match(alo, ahi, blo, bhi)
    # Explicit documented windows.
    r, m = both(None, " abcd", "abcd abcd")
    assert m.find_longest_match(0, 5, 0, 9) == r.find_longest_match(
        0, 5, 0, 9
    ) == M.Match(a=0, b=4, size=5)
    # Empty and reversed windows return (alo, blo, 0) on both sides.
    r, m = both(None, "abc", "abc")
    for args in [(2, 2, 3, 3), (5, 5, 0, 3), (0, 2, 3, 1), (3, 1, 0, 2)]:
        assert m.find_longest_match(*args) == r.find_longest_match(*args)


def test_find_longest_match_out_of_range_raises_and_returns_like_difflib():
    # difflib indexes a[i] directly, so a scan window reaching past
    # the end of a raises IndexError, while a b window past the end of
    # b only raises if the junk/popular extension actually indexes
    # past b; otherwise the value is returned. Negative indices wrap
    # in Python and are live scan positions. Each case below is a
    # probed difflib result, pinned against the port.
    cases_value = [
        ("abc", "abc", (0, 3, 0, 4), M.Match(0, 0, 3)),
        ("abc", "abc", (0, 3, 0, 100), M.Match(0, 0, 3)),
        ("abc", "abcd", (0, 3, 0, 4), M.Match(0, 0, 3)),
        ("abc", "abc", (-1, 3, 0, 3), M.Match(0, 0, 3)),
        ("abc", "abc", (-3, 3, 0, 3), M.Match(-3, 0, 3)),
        ("abc", "abc", (0, 3, -1, 3), M.Match(0, 0, 3)),
        ("abc", "abc", (0, 3, 0, -1), M.Match(0, 0, 0)),
    ]
    for a, b, args, want in cases_value:
        r, m = both(None, a, b)
        assert r.find_longest_match(*args) == want
        assert m.find_longest_match(*args) == want
    cases_raise = [
        ("abc", "abc", (0, 100, 0, 100)),
        ("abc", "abc", (-4, 3, 0, 3)),
        ("abcd", "abc", (0, 4, 0, 100)),
        ("abcd", "abc", (0, 4, 0, 4)),
    ]
    for a, b, args in cases_raise:
        with pytest.raises(IndexError):
            difflib.SequenceMatcher(None, a, b).find_longest_match(*args)
        with pytest.raises(IndexError):
            M.SequenceMatcher(None, a, b).find_longest_match(*args)


def test_ratios_use_documented_tolerance():
    # The three ratios agree to within RATIO_TOL (1e-12), pinned here
    # over the autojunk threshold on both sides of the 200 gate.
    for n in (199, 200, 201):
        a = "a" * n
        b = "b" + "a" * n
        for autojunk in (True, False):
            r, m = both(None, a, b, autojunk=autojunk)
            assert_ratio_close(m, r, "ratio")
            assert_ratio_close(m, r, "quick_ratio")
            assert_ratio_close(m, r, "real_quick_ratio")
    # The documented docstring ratios: 0.75, and round(..., 3) == 0.866.
    r, m = both(None, "abcd", "bcde")
    assert m.ratio() == r.ratio() == 0.75
    assert abs(m.ratio() - 0.75) <= RATIO_TOL
    assert_ratio_close(m, r, "quick_ratio")
    assert_ratio_close(m, r, "real_quick_ratio")
