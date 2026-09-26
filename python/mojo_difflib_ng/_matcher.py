"""SequenceMatcher, the Ratcliff/Obershelp matcher, on the Mojo kernels.

The Python layer owns every array and all policy: which elements are junk,
which are "popular" under the autojunk heuristic, and how much scratch the
compiled queue needs. The compiled side owns the scan itself --
dn_build_b2j, dn_find_longest_match and dn_match_blocks -- plus the three
similarity ratios and the batched get_close_matches scoring.
"""

from __future__ import annotations

import heapq
import os
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

import numpy as np

from ._lib import addr, i32
from ._lib import bincount as lib_bincount
from ._lib import build_b2j as lib_build_b2j
from ._lib import close_matches_range as lib_close_matches_range
from ._lib import find_longest_match as lib_find_longest_match
from ._lib import match_blocks as lib_match_blocks
from ._lib import quick_ratio as lib_quick_ratio
from ._lib import ratio as lib_ratio

Match = namedtuple("Match", "a b size")


def _real_quick_ratio(la: int, lb: int) -> float:
    if la + lb:
        return 2.0 * min(la, lb) / (la + lb)
    return 1.0


class _Scratch:
    """Caller-owned scratch for one compiled matcher.

    Never shared between threads: dn_quick_ratio and the j2len array are both
    consumed destructively, so two matchers in flight would corrupt each other.
    """

    __slots__ = ("nb", "na", "stackcap", "j2", "jgen", "kbuf", "stack", "blocks",
                 "tmp", "res", "fullcount")

    def __init__(self, nb: int, na: int, nbuckets: int = 0):
        nb = max(nb, 1)
        na = max(na, 0)
        cap = 2 * na + 16
        self.nb = nb
        self.na = na
        self.stackcap = cap
        self.j2 = np.zeros(nb, dtype=np.int32)
        self.jgen = np.full(nb, -1, dtype=np.int32)
        self.kbuf = np.zeros(nb, dtype=np.int32)
        self.stack = np.zeros(4 * cap, dtype=np.int32)
        self.blocks = np.zeros(3 * (na + 1), dtype=np.int32)
        self.tmp = np.zeros(3 * (na + 2), dtype=np.int32)
        self.res = np.zeros(3, dtype=np.int32)
        self.fullcount = np.zeros(nbuckets, dtype=np.int32)


# Below this length a str sequence goes through the plain loop: numpy's sort
# and its loop over the distinct values cost more than they save. Measured on
# this box, the crossover sits between 40 and 256 characters.
_VECTORISE_AT = 256


class _Interner:
    """Element -> dense Int32 code, shared by both sequences of a matcher.

    A code is assigned from one dict, so two elements are equal to the matcher
    exactly when difflib would consider them equal: hash, then eq, which is
    what its b2j keys use.

    A long str sequence takes a vectorised path, because that is the shape
    difflib is actually used in and the one where the per-element Python cost
    dominates everything else. Each character becomes a code point in a uint32
    array, np.unique turns that into distinct points plus an inverse index in
    C, and the only Python work left is one dict lookup per *distinct*
    character. For a 20-word English vocabulary over 20000 characters that is
    20 lookups instead of 20000. Below _VECTORISE_AT characters, and for any
    non-str sequence, the general loop is faster: numpy's sort plus its Python
    loop over the distinct values has a fixed cost a short sequence never
    amortises. The dict is what decides which elements share a code on both
    paths, so the choice of path cannot change the answer.
    """

    __slots__ = ("codes",)

    def __init__(self) -> None:
        self.codes: dict[Any, int] = {}

    def encode(self, seq: Any) -> np.ndarray:
        if isinstance(seq, str) and len(seq) >= _VECTORISE_AT:
            return self._encode_text(seq)
        if len(seq) == 0:
            return np.zeros(0, dtype=np.int32)
        return self._encode_general(seq, len(seq))

    def _encode_text(self, text: str) -> np.ndarray:
        if not text:
            return np.zeros(0, dtype=np.int32)
        points = np.frombuffer(text.encode("utf-32-le", "surrogatepass"),
                               dtype=np.uint32)
        uniq, inverse = np.unique(points, return_inverse=True)
        codes = self.codes
        nxt = len(codes)
        lut = np.empty(len(uniq), dtype=np.int32)
        for i, cp in enumerate(uniq.tolist()):
            key = chr(cp)
            c = codes.get(key)
            if c is None:
                c = nxt
                codes[key] = c
                nxt += 1
            lut[i] = c
        return lut[inverse]

    def _encode_general(self, seq: Sequence[Any], n: int) -> np.ndarray:
        get = self.codes.get
        codes = self.codes
        nxt = len(codes)
        out = np.empty(n, dtype=np.int32)
        for i, e in enumerate(seq):
            c = get(e)
            if c is None:
                c = nxt
                codes[e] = c
                nxt += 1
            out[i] = c
        return out


def _never_junk(element: Any) -> bool:
    return False


class SequenceMatcher:
    """Same contract as ``difflib.SequenceMatcher``, with the scan in Mojo.

    Sequences may hold any hashable elements. Both are interned to Int32 codes
    through one dict, so element identity becomes an integer compare; an
    element that never occurs in b simply has no bucket in b2j, exactly as in
    difflib, where ``b2j.get(a[i], nothing)`` is the same empty bucket.
    """

    def __init__(
        self,
        isjunk: Callable[[Any], bool] | None = None,
        a: Sequence[Any] = "",
        b: Sequence[Any] = "",
        autojunk: bool = True,
    ) -> None:
        self.isjunk = isjunk if isjunk is not None else _never_junk
        self.autojunk = autojunk
        self._intern = _Interner()
        self.a: list = []
        self.b: list = []
        self._acodes = np.zeros(0, dtype=np.int32)
        self._bcodes = np.zeros(0, dtype=np.int32)
        self._bjunk = np.zeros(0, dtype=np.uint8)
        self._nbuckets = 0
        self._start = np.zeros(1, dtype=np.int32)
        self._items = np.zeros(0, dtype=np.int32)
        self._fullcount = np.zeros(0, dtype=np.int32)
        self._scratch: _Scratch | None = None
        self.matching_blocks: list[Match] | None = None
        self.opcodes: list[tuple] | None = None
        if a or b:
            self.set_seqs(a, b)

    # -- sequence plumbing ------------------------------------------------

    def set_seqs(self, a: Sequence[Any], b: Sequence[Any]) -> None:
        self.set_seq1(a)
        self.set_seq2(b)

    def set_seq1(self, a: Sequence[Any]) -> None:
        if a is self.a:
            return
        self.a = list(a) if not isinstance(a, list) else a
        self._acodes = i32(self._intern.encode(self.a))
        self.matching_blocks = None
        self.opcodes = None
        self._rescratch()

    def set_seq2(self, b: Sequence[Any]) -> None:
        if b is self.b:
            return
        self.b = list(b) if not isinstance(b, list) else b
        self._bcodes = i32(self._intern.encode(self.b))
        self.matching_blocks = None
        self.opcodes = None
        self._build_b2j()
        self._rescratch()

    def _rescratch(self) -> None:
        self._scratch = _Scratch(len(self.b), len(self.a), self._nbuckets)

    def _build_b2j(self) -> None:
        nb = len(self.b)
        ncodes = max(len(self._intern.codes), 1)
        junk = np.fromiter(
            (1 if self.isjunk(e) else 0 for e in self.b), dtype=np.uint8, count=nb
        )
        self._bjunk = junk
        self._nbuckets = ncodes
        # fullbcount: every element of b, junk and popular included. That is
        # quick_ratio's multiplicity table, and it is not the bucket count.
        counts = np.zeros(ncodes, dtype=np.int32)
        lib_bincount(addr(self._bcodes), nb, addr(counts), ncodes)
        start = np.zeros(ncodes + 1, dtype=np.int32)
        cursor = np.zeros(ncodes, dtype=np.int32)
        # The junk-free total is at most the full total, so this is a safe
        # upper bound; the kernel returns the exact count it used.
        items = np.zeros(int(counts.sum()), dtype=np.int32)
        lib_build_b2j(
            addr(self._bcodes),
            nb,
            addr(junk),
            ncodes,
            1 if self.autojunk else 0,
            addr(start),
            addr(cursor),
            addr(items),
        )
        self._start = start
        self._items = items
        self._fullcount = counts

    def _b2j_dict(self) -> dict:
        """Reconstruct difflib's b2j mapping from the flat counting-sort arrays.

        Only what difflib keeps: non-junk, non-popular elements mapped to their
        positions in b, ascending. Useful for checking the index directly
        rather than only through the blocks it produces.
        """
        out: dict[Any, list[int]] = {}
        start = self._start.tolist()
        items = self._items.tolist()
        inverse = {code: element for element, code in self._intern.codes.items()}
        for pos, code in enumerate(self._bcodes.tolist()):
            lo, hi = start[code], start[code + 1]
            element = inverse[code]
            if hi > lo and element not in out:
                out[element] = items[lo:hi]
        return out

    def _sc(self) -> _Scratch:
        if self._scratch is None:
            self._rescratch()
        return self._scratch

    # -- matching ---------------------------------------------------------

    def find_longest_match(
        self, alo: int = 0, ahi: int | None = None, blo: int = 0,
        bhi: int | None = None,
    ) -> Match:
        """Longest junk-free match in a[alo:ahi] against b[blo:bhi]."""
        a = self.a
        b = self.b
        if ahi is None:
            ahi = len(a)
        if bhi is None:
            bhi = len(b)
        sc = self._sc()
        sc.res[0] = alo
        sc.res[1] = blo
        sc.res[2] = 0
        lib_find_longest_match(
            addr(self._acodes),
            addr(self._bcodes),
            addr(self._start),
            addr(self._items),
            addr(self._bjunk),
            addr(sc.j2),
            addr(sc.jgen),
            addr(sc.kbuf),
            self._nbuckets,
            alo,
            ahi,
            blo,
            bhi,
            addr(sc.res),
        )
        return Match(int(sc.res[0]), int(sc.res[1]), int(sc.res[2]))

    def get_matching_blocks(self) -> list[Match]:
        """Non-overlapping matching subsequences, terminated by the sentinel."""
        if self.matching_blocks is not None:
            return self.matching_blocks
        sc = self._sc()
        na, nb = len(self.a), len(self.b)
        lib_match_blocks(
            addr(self._acodes),
            addr(self._bcodes),
            na,
            nb,
            addr(self._start),
            addr(self._items),
            addr(self._bjunk),
            addr(sc.j2),
            addr(sc.jgen),
            addr(sc.kbuf),
            self._nbuckets,
            addr(sc.stack),
            sc.stackcap,
            addr(sc.blocks),
            addr(sc.tmp),
            addr(sc.res),
        )
        count = int(sc.res[0])
        if count < 0:
            raise RuntimeError("difflib-ng: matching block queue overflowed")
        flat = sc.tmp[: 3 * count].tolist()
        self.matching_blocks = [Match(*flat[t * 3 : t * 3 + 3]) for t in range(count)]
        return self.matching_blocks

    def get_opcodes(self) -> list[tuple]:
        """Edit script as (tag, i1, i2, j1, j2) tuples."""
        if self.opcodes is not None:
            return self.opcodes
        i = j = 0
        answer: list[tuple] = []
        for ai, bj, size in self.get_matching_blocks():
            tag = ""
            if i < ai and j < bj:
                tag = "replace"
            elif i < ai:
                tag = "delete"
            elif j < bj:
                tag = "insert"
            if tag:
                answer.append((tag, i, ai, j, bj))
            i, j = ai + size, bj + size
            if size:
                answer.append(("equal", ai, i, bj, j))
        self.opcodes = answer
        return answer

    def get_grouped_opcodes(self, n: int = 3):
        """Opcodes clustered into change groups with n lines of context."""
        # Note the deliberate absence of a defensive copy: difflib's own
        # get_grouped_opcodes edits self.opcodes in place when it trims the
        # leading and trailing equal runs, so calling it changes what a later
        # get_opcodes() returns. A drop-in replacement has to reproduce that,
        # including the surprise, so the cached list is edited here too.
        codes = self.get_opcodes()
        if not codes:
            codes = [("equal", 0, 1, 0, 1)]
        if codes[0][0] == "equal":
            tag, i1, i2, j1, j2 = codes[0]
            codes[0] = tag, max(i1, i2 - n), i2, max(j1, j2 - n), j2
        if codes[-1][0] == "equal":
            tag, i1, i2, j1, j2 = codes[-1]
            codes[-1] = tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)

        nn = n + n
        group = []
        for tag, i1, i2, j1, j2 in codes:
            if tag == "equal" and i2 - i1 > nn:
                group.append((tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)))
                yield group
                group = []
                i1, j1 = max(i1, i2 - n), max(j1, j2 - n)
            group.append((tag, i1, i2, j1, j2))
        if group and not (len(group) == 1 and group[0][0] == "equal"):
            yield group

    # -- ratios -----------------------------------------------------------

    def ratio(self) -> float:
        """2 * (matched elements) / (len(a) + len(b))."""
        blocks = self.get_matching_blocks()
        flat = np.asarray(blocks, dtype=np.int32).reshape(-1)
        out = np.zeros(1, dtype=np.float64)
        lib_ratio(addr(flat), len(blocks), len(self.a) + len(self.b), addr(out))
        return float(out[0])

    def quick_ratio(self) -> float:
        """Upper bound on ratio: the order-blind multiset intersection."""
        out = np.zeros(1, dtype=np.float64)
        # dn_quick_ratio consumes the count array destructively, so it works
        # on a copy and self._fullcount stays pristine for the next call.
        lib_quick_ratio(
            addr(self._acodes),
            len(self.a),
            addr(self._fullcount.copy()),
            len(self._fullcount),
            len(self.a) + len(self.b),
            addr(out),
        )
        return float(out[0])

    def real_quick_ratio(self) -> float:
        """Cheapest upper bound: 2 * min(len(a), len(b)) / (len(a) + len(b))."""
        return _real_quick_ratio(len(self.a), len(self.b))

    def get_close_matches(self, word, possibilities, n: int = 3, cutoff: float = 0.6):
        return get_close_matches(word, possibilities, n=n, cutoff=cutoff)


def get_close_matches(word, possibilities, n: int = 3, cutoff: float = 0.6) -> list:
    """Best "good enough" matches for word among possibilities.

    The scoring loop is one compiled pass over the whole candidate set, split
    across a thread pool when the batch is big enough to pay for it. Each
    worker owns its scratch, so the chunking cannot change a score.
    """
    if n <= 0:
        raise ValueError(f"n must be > 0: {n!r}")
    if not 0.0 <= cutoff <= 1.0:
        raise ValueError(f"cutoff must be in [0.0, 1.0]: {cutoff!r}")

    poss = list(possibilities)
    ncand = len(poss)
    if ncand == 0:
        return []

    sm = SequenceMatcher(None, b=word)
    intern = sm._intern
    flat = i32(np.concatenate([intern.encode(p) for p in poss]))
    lens = np.fromiter((len(p) for p in poss), dtype=np.int32, count=ncand)
    offs = np.zeros(ncand, dtype=np.int32)
    if ncand > 1:
        offs[1:] = np.cumsum(lens[:-1])
    maxna = int(lens.max()) if ncand else 0

    out_rqr = np.zeros(ncand, dtype=np.float64)
    out_qr = np.zeros(ncand, dtype=np.float64)
    out_ratio = np.zeros(ncand, dtype=np.float64)

    nthreads = _thread_count(ncand, maxna)
    parts = _chunks(ncand, nthreads)
    scratchers = [
        _Scratch(len(sm.b), maxna, sm._nbuckets) for _ in range(len(parts))
    ]

    def run(idx: int):
        lo, hi = parts[idx]
        sc = scratchers[idx]
        lib_close_matches_range(
            addr(flat),
            addr(offs),
            addr(lens),
            lo,
            hi,
            addr(sm._bcodes),
            len(sm.b),
            addr(sm._start),
            addr(sm._items),
            addr(sm._bjunk),
            addr(sc.j2),
            addr(sc.jgen),
            addr(sc.kbuf),
            sm._nbuckets,
            addr(sc.fullcount),
            sm._nbuckets,
            float(cutoff),
            addr(sc.stack),
            sc.stackcap,
            addr(sc.blocks),
            addr(sc.tmp),
            addr(sc.res),
            addr(out_rqr),
            addr(out_qr),
            addr(out_ratio),
        )

    if len(parts) == 1:
        run(0)
    else:
        with ThreadPoolExecutor(max_workers=len(parts)) as ex:
            list(ex.map(run, range(len(parts))))

    scored = [
        (r, p) for r, p in zip(out_ratio.tolist(), poss) if r >= cutoff
    ]
    return [x for _score, x in heapq.nlargest(n, scored)]


def _chunks(ncand: int, nthreads: int) -> list[tuple[int, int]]:
    step = (ncand + nthreads - 1) // nthreads
    return [(lo, min(lo + step, ncand)) for lo in range(0, ncand, step)]


def _thread_count(ncand: int, maxna: int) -> int:
    if ncand < 64:
        return 1
    if ncand * max(1, maxna) < 20_000:
        return 1
    return max(1, min(8, os.cpu_count() or 1, ncand // 8))
