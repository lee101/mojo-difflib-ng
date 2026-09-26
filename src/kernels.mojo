"""Ratcliff/Obershelp matching kernels for the Python-facing SequenceMatcher.

Every exported symbol takes buffer addresses as plain `Int` values and rebuilds
the pointer inside the body, because `@export` rejects parametric functions and
an inferred pointer origin would make the symbol parametric.

Sequences arrive as dense integer codes: the Python shim interns every element
of both sequences through one dict, so `a[i] == b[i]` becomes an Int32 compare
and an element's identity is exactly its code. Elements that do not occur in
`b` carry a code at or above `nbuckets`, and `find_longest_match` skips them,
which is the same thing CPython's `b2j.get(a[i], nothing)` does.

The dict `b2j` CPython builds maps a non-junk, non-"popular" element of `b` to
its positions in ascending order. `dn_build_b2j` lays that out as a counting
sort into two flat arrays, which is what the inner scan below walks.
"""

from std.sys.info import simd_width_of

comptime W = simd_width_of[DType.int32]()
comptime I32 = Pointer[Int32, AnyOrigin[mut=True]]
comptime F64 = Pointer[Float64, AnyOrigin[mut=True]]
comptime U8 = Pointer[UInt8, AnyOrigin[mut=True]]


def ip(addr: Int) -> I32:
    return I32(unsafe_from_address=addr)


def up(addr: Int) -> U8:
    return U8(unsafe_from_address=addr)


def fp(addr: Int) -> F64:
    return F64(unsafe_from_address=addr)


# --------------------------------------------------------------------------
# Histogram and index construction
# --------------------------------------------------------------------------


@export("dn_bincount")
def dn_bincount(codes_addr: Int, n: Int, counts_addr: Int, ncounts: Int) abi("C"):
    """Count occurrences of every code, i.e. counts[c] over c in range(ncounts)."""
    var codes = ip(codes_addr)
    var counts = ip(counts_addr)
    for i in range(n):
        var c = codes[unsafe_offset=i]
        if c >= 0 and c < Int32(ncounts):
            counts[unsafe_offset=c] += 1


@export("dn_build_b2j")
def dn_build_b2j(
    bcodes_addr: Int,
    nb: Int,
    junk_addr: Int,
    ncounts: Int,
    autojunk: Int,
    start_addr: Int,
    cursor_addr: Int,
    items_addr: Int,
) abi("C") -> Int32:
    """Build difflib's b2j index as a flat counting sort.

    A code's bucket holds only its non-junk positions, ascending, so the
    junk test has to be applied while counting and not only while scattering:
    a bucket sized from the junk-inclusive count would be wider than its real
    contents and expose uninitialised slots as positions. When `autojunk` is
    set and b is at least 200 long, a code occurring more than nb//100 + 1
    times is "popular" and gets no bucket either, exactly as difflib's
    set_seq2 purges them.

    start[c] .. start[c+1] delimits code c's position list in `items`, and
    start[ncounts] is the total. `cursor` is scratch of at least ncounts.
    Returns the total.
    """
    var b = ip(bcodes_addr)
    var junk = up(junk_addr)
    var start = ip(start_addr)
    var cursor = ip(cursor_addr)
    var items = ip(items_addr)

    var ntest = 0
    if autojunk != 0 and nb >= 200:
        ntest = nb // 100 + 1

    for c in range(ncounts):
        cursor[unsafe_offset=c] = 0
    for i in range(nb):
        if junk[unsafe_offset=i] != 0:
            continue
        var code = Int(b[unsafe_offset=i])
        if code < 0 or code >= ncounts:
            continue
        cursor[unsafe_offset=code] += 1

    var total = 0
    for c in range(ncounts):
        var keep = Int(cursor[unsafe_offset=c])
        if ntest > 0 and keep > ntest:
            # Popular: no bucket at all. cursor = -1 marks it for the scatter.
            cursor[unsafe_offset=c] = -1
            keep = 0
        else:
            cursor[unsafe_offset=c] = Int32(total)
        start[unsafe_offset=Int32(c)] = Int32(total)
        total += keep
    start[unsafe_offset=ncounts] = Int32(total)

    # Scanning b in order fills each bucket in ascending position order, which
    # is what find_longest_match's `if j >= bhi: break` depends on.
    for i in range(nb):
        if junk[unsafe_offset=i] != 0:
            continue
        var code = Int(b[unsafe_offset=i])
        if code < 0 or code >= ncounts:
            continue
        var q = Int(cursor[unsafe_offset=code])
        if q >= 0:
            items[unsafe_offset=q] = Int32(i)
            cursor[unsafe_offset=code] = Int32(q + 1)

    return Int32(total)


# --------------------------------------------------------------------------
# find_longest_match
# --------------------------------------------------------------------------


def reset_j2(j2: I32, jgen: I32, nb: Int):
    """Mark every length as absent.

    -1 is not a value any iteration writes (generations are >= 0), so it can
    never satisfy the `jgen[j-1] == i-1` presence test below.
    """
    for k in range(nb):
        j2[unsafe_offset=k] = 0
        jgen[unsafe_offset=k] = -1


def flm(
    a: I32,
    aoff: Int,
    b: I32,
    start: I32,
    items: I32,
    bjunk: U8,
    j2: I32,
    jgen: I32,
    kbuf: I32,
    nbuckets: Int,
    stamp_base: Int,
    alo: Int,
    ahi: Int,
    blo: Int,
    bhi: Int,
    res: I32,
):
    """The find_longest_match inner scan, writing (i, j, size) to res.

    CPython keeps a dict `j2len` holding, for the current i, the length of the
    longest junk-free match ending with a[i-1] and b[j], and it swaps in a
    fresh dict on every iteration of i. A value written for this i must not be
    readable as the previous i's value, which rules out a single array used
    read-write. Instead the scan runs in two passes: pass one reads only, and
    pass two writes. `jgen[j]` records which i last wrote j, so "was j present
    in iteration i-1" is a stamp compare rather than a value compare, and a
    slot left over from an earlier scan is simply not stamped for i-1.

    Writing j while reading j-1 in one pass is the bug this shape exists to
    prevent: a="aaa" against b="aa" would otherwise grow a length-2 run at i=0
    that no real match supports.
    """
    var besti = alo
    var bestj = blo
    var bestsize = 0

    for i in range(alo, ahi):
        var stamp = stamp_base + (i - alo)
        var c = a[unsafe_offset=aoff + i]
        if c < 0 or c >= Int32(nbuckets):
            # a[i] has no bucket: it does not occur in b, or it is junk or
            # popular, and in every one of those cases j2len stays empty.
            continue
        var lo = Int(start[unsafe_offset=c])
        var hi = Int(start[unsafe_offset=c + 1])
        # Positions are ascending, so the in-window slice is one contiguous run.
        var s0 = 0
        while lo + s0 < hi and Int(items[unsafe_offset=lo + s0]) < blo:
            s0 += 1
        var s1 = s0
        while lo + s1 < hi and Int(items[unsafe_offset=lo + s1]) < bhi:
            s1 += 1
        var m = s1 - s0
        if m <= 0:
            continue

        var t = 0
        while t < m:
            var j = Int(items[unsafe_offset=lo + s0 + t])
            var k = 1
            if i > alo and j > 0 and jgen[unsafe_offset=j - 1] == Int32(stamp - 1):
                k = Int(j2[unsafe_offset=j - 1]) + 1
            kbuf[unsafe_offset=t] = Int32(k)
            t += 1

        var s = 0
        while s < m:
            var j = Int(items[unsafe_offset=lo + s0 + s])
            var k = Int(kbuf[unsafe_offset=s])
            j2[unsafe_offset=j] = Int32(k)
            jgen[unsafe_offset=j] = Int32(stamp)
            if k > bestsize:
                besti = i - k + 1
                bestj = j - k + 1
                bestsize = k
            s += 1

    # Extend the best by non-junk elements on each end. "Popular" non-junk
    # elements are deliberately absent from b2j, so the best match found above
    # contains neither junk nor popular elements, and this pass is what picks
    # the popular ones up.
    while (
        besti > alo
        and bestj > blo
        and bjunk[unsafe_offset=bestj - 1] == 0
        and a[unsafe_offset=aoff + besti - 1] == b[unsafe_offset=bestj - 1]
    ):
        besti -= 1
        bestj -= 1
        bestsize += 1
    while (
        besti + bestsize < ahi
        and bestj + bestsize < bhi
        and bjunk[unsafe_offset=bestj + bestsize] == 0
        and a[unsafe_offset=aoff + besti + bestsize] == b[unsafe_offset=bestj + bestsize]
    ):
        bestsize += 1

    # Then absorb adjacent matching junk on each side.
    while (
        besti > alo
        and bestj > blo
        and bjunk[unsafe_offset=bestj - 1] != 0
        and a[unsafe_offset=aoff + besti - 1] == b[unsafe_offset=bestj - 1]
    ):
        besti -= 1
        bestj -= 1
        bestsize += 1
    while (
        besti + bestsize < ahi
        and bestj + bestsize < bhi
        and bjunk[unsafe_offset=bestj + bestsize] != 0
        and a[unsafe_offset=aoff + besti + bestsize] == b[unsafe_offset=bestj + bestsize]
    ):
        bestsize += 1

    res[unsafe_offset=0] = Int32(besti)
    res[unsafe_offset=1] = Int32(bestj)
    res[unsafe_offset=2] = Int32(bestsize)


@export("dn_reset_scratch")
def dn_reset_scratch(j2_addr: Int, jgen_addr: Int, nb: Int) abi("C"):
    var j2 = ip(j2_addr)
    var jgen = ip(jgen_addr)
    reset_j2(j2, jgen, nb)


@export("dn_find_longest_match")
def dn_find_longest_match(
    acodes_addr: Int,
    bcodes_addr: Int,
    start_addr: Int,
    items_addr: Int,
    bjunk_addr: Int,
    j2_addr: Int,
    jgen_addr: Int,
    kbuf_addr: Int,
    nbuckets: Int,
    alo: Int,
    ahi: Int,
    blo: Int,
    bhi: Int,
    res_addr: Int,
) abi("C"):
    var a = ip(acodes_addr)
    var b = ip(bcodes_addr)
    var start = ip(start_addr)
    var items = ip(items_addr)
    var bjunk = up(bjunk_addr)
    var j2 = ip(j2_addr)
    var jgen = ip(jgen_addr)
    var kbuf = ip(kbuf_addr)
    var res = ip(res_addr)
    reset_j2(j2, jgen, bhi if bhi > blo else blo)
    flm(
        a, 0, b, start, items, bjunk, j2, jgen, kbuf, nbuckets, 0, alo, ahi,
        blo, bhi, res,
    )


# --------------------------------------------------------------------------
# get_matching_blocks: the queue, the sort and the adjacency collapse
# --------------------------------------------------------------------------


@always_inline
def blk_lt(x: I32, xi: Int, y: I32, yi: Int) -> Bool:
    var p = x[unsafe_offset=xi]
    var q = y[unsafe_offset=yi]
    if p != q:
        return p < q
    p = x[unsafe_offset=xi + 1]
    q = y[unsafe_offset=yi + 1]
    if p != q:
        return p < q
    return x[unsafe_offset=xi + 2] < y[unsafe_offset=yi + 2]


@always_inline
def blk_copy(dst: I32, dk: Int, src: I32, sk: Int):
    dst[unsafe_offset=dk] = src[unsafe_offset=sk]
    dst[unsafe_offset=dk + 1] = src[unsafe_offset=sk + 1]
    dst[unsafe_offset=dk + 2] = src[unsafe_offset=sk + 2]


@always_inline
def blk_gt_key(x: I32, xi: Int, k0: Int32, k1: Int32, k2: Int32) -> Bool:
    """True when the held-back key (k0, k1, k2) sorts after x[xi]."""
    var p = x[unsafe_offset=xi]
    if p != k0:
        return k0 < p
    p = x[unsafe_offset=xi + 1]
    if p != k1:
        return k1 < p
    return k2 < x[unsafe_offset=xi + 2]


def sort_blocks(blocks: I32, tmp: I32, n: Int):
    """Sort n (i, j, size) triples lexicographically, leaving them in `blocks`.

    Small runs use insertion sort. Larger ones use bottom-up merge sort, because
    a heavily fragmented diff produces close to one block per element and an
    insertion sort would go quadratic exactly there.
    """
    if n < 2:
        return
    if n <= 24:
        for i in range(1, n):
            # The key is held back and written at its final slot; shifting on
            # the fly without that write duplicates the element it passed.
            var k0 = blocks[unsafe_offset=3 * i]
            var k1 = blocks[unsafe_offset=3 * i + 1]
            var k2 = blocks[unsafe_offset=3 * i + 2]
            var src = 3 * i
            while src >= 3 and blk_gt_key(blocks, src - 3, k0, k1, k2):
                blk_copy(blocks, src, blocks, src - 3)
                src -= 3
            blocks[unsafe_offset=src] = k0
            blocks[unsafe_offset=src + 1] = k1
            blocks[unsafe_offset=src + 2] = k2
        return
    var s = blocks
    var t = tmp
    var width = 1
    while width < n:
        var lo = 0
        while lo < n:
            var mid = lo + width
            if mid > n:
                mid = n
            var hi = lo + 2 * width
            if hi > n:
                hi = n
            var i = lo
            var j = mid
            var k = lo
            while i < mid and j < hi:
                if blk_lt(s, 3 * j, s, 3 * i):
                    blk_copy(t, 3 * k, s, 3 * j)
                    j += 1
                else:
                    blk_copy(t, 3 * k, s, 3 * i)
                    i += 1
                k += 1
            while i < mid:
                blk_copy(t, 3 * k, s, 3 * i)
                i += 1
                k += 1
            while j < hi:
                blk_copy(t, 3 * k, s, 3 * j)
                j += 1
                k += 1
            lo = hi
        var swap = s
        s = t
        t = swap
        width *= 2
    if s != blocks:
        for i in range(3 * n):
            blocks[unsafe_offset=i] = s[unsafe_offset=i]


def match_blocks_into(
    a: I32,
    aoff: Int,
    b: I32,
    na: Int,
    nb: Int,
    start: I32,
    items: I32,
    bjunk: U8,
    j2: I32,
    jgen: I32,
    kbuf: I32,
    nbuckets: Int,
    stack: I32,
    stackcap: Int,
    blocks: I32,
    tmp: I32,
    res: I32,
) -> Bool:
    """Ratcliff/Obershelp over an explicit stack. True on success.

    Nonzero blocks land in `blocks` as 3*count Int32 values, sorted and with
    adjacent equal blocks collapsed, and the (na, nb, 0) sentinel appended in
    `tmp`. The count goes to res[0].
    """
    reset_j2(j2, jgen, nb)
    stack[unsafe_offset=0] = 0
    stack[unsafe_offset=1] = Int32(na)
    stack[unsafe_offset=2] = 0
    stack[unsafe_offset=3] = Int32(nb)
    # `top` is the element offset just past the live region, so a pending
    # range is stack[top-4 .. top-1]. Element offsets rather than entry counts,
    # so the push and the pop cannot drift apart by a factor.
    var top = 4
    var nblk = 0
    var overflow = False
    var stamp = 0

    while top > 0:
        var base = top - 4
        var alo = Int(stack[unsafe_offset=base])
        var ahi = Int(stack[unsafe_offset=base + 1])
        var blo = Int(stack[unsafe_offset=base + 2])
        var bhi = Int(stack[unsafe_offset=base + 3])
        top = base
        flm(
            a, aoff, b, start, items, bjunk, j2, jgen, kbuf, nbuckets, stamp,
            alo, ahi, blo, bhi, res,
        )
        stamp += ahi - alo
        var mi = Int(res[unsafe_offset=0])
        var mj = Int(res[unsafe_offset=1])
        var mk = Int(res[unsafe_offset=2])
        if mk != 0:
            blocks[unsafe_offset=3 * nblk] = Int32(mi)
            blocks[unsafe_offset=3 * nblk + 1] = Int32(mj)
            blocks[unsafe_offset=3 * nblk + 2] = Int32(mk)
            nblk += 1
            if top + 8 > stackcap:
                overflow = True
                break
            if alo < mi and blo < mj:
                stack[unsafe_offset=top] = Int32(alo)
                stack[unsafe_offset=top + 1] = Int32(mi)
                stack[unsafe_offset=top + 2] = Int32(blo)
                stack[unsafe_offset=top + 3] = Int32(mj)
                top += 4
            if mi + mk < ahi and mj + mk < bhi:
                stack[unsafe_offset=top] = Int32(mi + mk)
                stack[unsafe_offset=top + 1] = Int32(ahi)
                stack[unsafe_offset=top + 2] = Int32(mj + mk)
                stack[unsafe_offset=top + 3] = Int32(bhi)
                top += 4

    if overflow:
        res[unsafe_offset=0] = -1
        return False

    sort_blocks(blocks, tmp, nblk)

    # Collapse adjacent equal blocks and append the sentinel, into tmp.
    var count = 0
    var i1 = 0
    var j1 = 0
    var k1 = 0
    for t in range(nblk):
        var i2 = Int(blocks[unsafe_offset=3 * t])
        var j2 = Int(blocks[unsafe_offset=3 * t + 1])
        var k2 = Int(blocks[unsafe_offset=3 * t + 2])
        if i1 + k1 == i2 and j1 + k1 == j2:
            k1 += k2
        else:
            if k1 != 0:
                tmp[unsafe_offset=3 * count] = Int32(i1)
                tmp[unsafe_offset=3 * count + 1] = Int32(j1)
                tmp[unsafe_offset=3 * count + 2] = Int32(k1)
                count += 1
            i1 = i2
            j1 = j2
            k1 = k2
    if k1 != 0:
        tmp[unsafe_offset=3 * count] = Int32(i1)
        tmp[unsafe_offset=3 * count + 1] = Int32(j1)
        tmp[unsafe_offset=3 * count + 2] = Int32(k1)
        count += 1
    tmp[unsafe_offset=3 * count] = Int32(na)
    tmp[unsafe_offset=3 * count + 1] = Int32(nb)
    tmp[unsafe_offset=3 * count + 2] = 0
    count += 1
    res[unsafe_offset=0] = Int32(count)
    return True


@export("dn_match_blocks")
def dn_match_blocks(
    acodes_addr: Int,
    bcodes_addr: Int,
    na: Int,
    nb: Int,
    start_addr: Int,
    items_addr: Int,
    bjunk_addr: Int,
    j2_addr: Int,
    jgen_addr: Int,
    kbuf_addr: Int,
    nbuckets: Int,
    stack_addr: Int,
    stackcap: Int,
    blocks_addr: Int,
    tmp_addr: Int,
    res_addr: Int,
) abi("C"):
    """get_matching_blocks; the result is 3*res[0] Int32 values in `tmp`."""
    var ok = match_blocks_into(
        ip(acodes_addr),
        0,
        ip(bcodes_addr),
        na,
        nb,
        ip(start_addr),
        ip(items_addr),
        up(bjunk_addr),
        ip(j2_addr),
        ip(jgen_addr),
        ip(kbuf_addr),
        nbuckets,
        ip(stack_addr),
        stackcap,
        ip(blocks_addr),
        ip(tmp_addr),
        ip(res_addr),
    )
    if not ok:
        ip(res_addr)[unsafe_offset=0] = -1


# --------------------------------------------------------------------------
# Similarity ratios
# --------------------------------------------------------------------------


@export("dn_ratio")
def dn_ratio(blocks_addr: Int, nblk: Int, total: Int, res_addr: Int) abi("C"):
    """Return 2 * (sum of block sizes) / total, or 1.0 when total is zero."""
    var blocks = ip(blocks_addr)
    var res = fp(res_addr)
    var matches = 0
    for t in range(nblk):
        matches += Int(blocks[unsafe_offset=3 * t + 2])
    if total > 0:
        res[unsafe_offset=0] = 2.0 * Float64(matches) / Float64(total)
    else:
        res[unsafe_offset=0] = 1.0


@export("dn_quick_ratio")
def dn_quick_ratio(
    acodes_addr: Int,
    na: Int,
    fullcount_addr: Int,
    ncounts: Int,
    total: Int,
    res_addr: Int,
) abi("C"):
    """Return the multiset intersection of a and b as 2 * matches / total.

    `fullcount` holds the multiplicity of every code in b, junk and popular
    elements included, which is difflib's fullbcount. It is consumed
    destructively, exactly as difflib's `avail` is.
    """
    var a = ip(acodes_addr)
    var full = ip(fullcount_addr)
    var res = fp(res_addr)
    var matches = 0
    for i in range(na):
        var c = Int(a[unsafe_offset=i])
        if c >= 0 and c < ncounts:
            var numb = Int(full[unsafe_offset=c])
            full[unsafe_offset=c] = Int32(numb - 1)
            if numb > 0:
                matches += 1
    if total > 0:
        res[unsafe_offset=0] = 2.0 * Float64(matches) / Float64(total)
    else:
        res[unsafe_offset=0] = 1.0


# --------------------------------------------------------------------------
# get_close_matches: one b, many candidates
# --------------------------------------------------------------------------


@export("dn_close_matches_range")
def dn_close_matches_range(
    cand_addr: Int,
    off_addr: Int,
    len_addr: Int,
    c0: Int,
    c1: Int,
    bcodes_addr: Int,
    nb: Int,
    start_addr: Int,
    items_addr: Int,
    bjunk_addr: Int,
    j2_addr: Int,
    jgen_addr: Int,
    kbuf_addr: Int,
    nbuckets: Int,
    fullcount_addr: Int,
    ncounts: Int,
    cutoff: Float64,
    stack_addr: Int,
    stackcap: Int,
    blocks_addr: Int,
    tmp_addr: Int,
    res_addr: Int,
    out_rqr_addr: Int,
    out_qr_addr: Int,
    out_ratio_addr: Int,
) abi("C") -> Int32:
    """Score candidates c0..c1 against b, writing three ratios per candidate.

    A candidate that fails either cheap upper bound gets ratio 0.0, which is
    what difflib's `if real_quick_ratio() >= cutoff and quick_ratio() >=
    cutoff and ratio() >= cutoff` short-circuit amounts to, since the caller
    only keeps candidates at or above the cutoff.
    """
    var cand = ip(cand_addr)
    var off = ip(off_addr)
    var ln = ip(len_addr)
    var b = ip(bcodes_addr)
    var start = ip(start_addr)
    var items = ip(items_addr)
    var bjunk = up(bjunk_addr)
    var j2 = ip(j2_addr)
    var jgen = ip(jgen_addr)
    var kbuf = ip(kbuf_addr)
    var full = ip(fullcount_addr)
    var stack = ip(stack_addr)
    var blocks = ip(blocks_addr)
    var tmp = ip(tmp_addr)
    var res = ip(res_addr)
    var out_rqr = fp(out_rqr_addr)
    var out_qr = fp(out_qr_addr)
    var out_ratio = fp(out_ratio_addr)

    for c in range(c0, c1):
        var lo = Int(off[unsafe_offset=c])
        var na = Int(ln[unsafe_offset=c])
        var total = na + nb

        var rqr = 1.0
        if total > 0:
            rqr = 2.0 * Float64(min(na, nb)) / Float64(total)
        out_rqr[unsafe_offset=c] = rqr

        if rqr < cutoff:
            out_qr[unsafe_offset=c] = 0.0
            out_ratio[unsafe_offset=c] = 0.0
            continue

        # quick_ratio consumes fullcount destructively, so refill it from b.
        for k in range(ncounts):
            full[unsafe_offset=k] = 0
        for k in range(nb):
            var code = Int(b[unsafe_offset=k])
            if code >= 0 and code < ncounts:
                full[unsafe_offset=code] += 1

        var matches = 0
        for i in range(na):
            var code = Int(cand[unsafe_offset=lo + i])
            if code >= 0 and code < ncounts:
                var numb = Int(full[unsafe_offset=code])
                full[unsafe_offset=code] = Int32(numb - 1)
                if numb > 0:
                    matches += 1
        var qr = 1.0
        if total > 0:
            qr = 2.0 * Float64(matches) / Float64(total)
        out_qr[unsafe_offset=c] = qr

        if qr < cutoff:
            out_ratio[unsafe_offset=c] = 0.0
            continue

        var cap = stackcap
        if na * 2 + 16 < cap:
            cap = na * 2 + 16
        var ok = match_blocks_into(
            cand,
            lo,
            b,
            na,
            nb,
            start,
            items,
            bjunk,
            j2,
            jgen,
            kbuf,
            nbuckets,
            stack,
            cap,
            blocks,
            tmp,
            res,
        )
        var ratio = 0.0
        if ok:
            var count = Int(res[unsafe_offset=0])
            var m = 0
            for t in range(count):
                m += Int(tmp[unsafe_offset=3 * t + 2])
            if total > 0:
                ratio = 2.0 * Float64(m) / Float64(total)
            else:
                ratio = 1.0
        out_ratio[unsafe_offset=c] = ratio

    return Int32(c1 - c0)
