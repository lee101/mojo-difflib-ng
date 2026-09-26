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
import random

import pytest

import mojo_difflib_ng as M


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


# -- get_matching_blocks: the queue, the sort and the collapse -------------


@pytest.mark.parametrize("alpha", ["ab", "abcde ", "abcdefghij"])
@pytest.mark.parametrize("seed", range(12))
def test_random_blocks_opcodes_ratios(alpha, seed):
    rnd = random.Random(seed * 977 + len(alpha))
    for _ in range(60):
        a, b, isjunk, autojunk = rand_case(rnd, alpha)
        r, m = both(isjunk, a, b, autojunk=autojunk)
        assert list(m.get_matching_blocks()) == list(r.get_matching_blocks())
        assert m.get_opcodes() == r.get_opcodes()
        assert m.ratio() == r.ratio()
        assert m.quick_ratio() == r.quick_ratio()
        assert m.real_quick_ratio() == r.real_quick_ratio()
        assert m.find_longest_match() == r.find_longest_match()


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


# -- ratios ---------------------------------------------------------------


def test_quick_ratio_is_an_upper_bound():
    rnd = random.Random(3)
    for _ in range(200):
        a = "".join(rnd.choice("abcd") for _ in range(rnd.randint(0, 40)))
        b = "".join(rnd.choice("abcd") for _ in range(rnd.randint(0, 40)))
        r, m = both(None, a, b)
        assert m.quick_ratio() == r.quick_ratio()
        assert m.real_quick_ratio() == r.real_quick_ratio()
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


def test_constructor_default_is_no_junk():
    m = M.SequenceMatcher(a="a", b="b")
    assert m.isjunk("a") is False
    m = M.SequenceMatcher(lambda x: x == "z", a="a", b="b")
    assert m.isjunk("z") is True
