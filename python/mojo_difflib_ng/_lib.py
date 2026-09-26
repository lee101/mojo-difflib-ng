"""ctypes bridge to the compiled Mojo matching kernels.

The shared library owns no memory. Every buffer crosses the C ABI as a 64-bit
address, so the argtypes below must stay ``c_int64`` for addresses; ``c_int``
truncates them and segfaults on a buffer mapped above 4 GiB.

ctypes zero-fills a short argtypes list instead of failing, so a missing entry
would let a 64-bit address land in a 32-bit slot and truncate silently while
every small-array test still passes. Two things guard against that here: the
declared arity is verified against the built symbol table at load time, and
every kernel is called through a wrapper whose Python signature has a fixed
positional shape, so a dropped or added argument is a TypeError rather than a
truncation.
"""

from __future__ import annotations

import ctypes
import pathlib

import numpy as np

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[2]
_LIB_PATH = _ROOT / "dist" / "libmojo-difflib-ng.so"

_A = ctypes.c_int64  # a buffer address; 64-bit, never c_int
_N = ctypes.c_int64  # a count or index
_F = ctypes.c_double

_PROTOTYPES = {
    "dn_bincount": ([_A, _N, _A, _N], None),
    "dn_build_b2j": ([_A, _N, _A, _N, _N, _A, _A, _A], ctypes.c_int32),
    "dn_reset_scratch": ([_A, _A, _N], None),
    "dn_find_longest_match": ([_A, _A, _A, _A, _A, _A, _A, _A] + [_N] * 6, None),
    "dn_match_blocks": (
        [_A, _A, _N, _N, _A, _A, _A, _A, _A, _A, _N, _A, _N, _A, _A, _A], None
    ),
    "dn_ratio": ([_A, _N, _N, _A], None),
    "dn_quick_ratio": ([_A, _N, _A, _N, _N, _A], None),
    "dn_close_matches_range": ([_A] * 16 + [_F] + [_A] * 8, ctypes.c_int32),
}


def _load():
    if not _LIB_PATH.exists():
        raise RuntimeError(
            f"{_LIB_PATH} not found; run `bash build/build.sh` first"
        )
    lib = ctypes.CDLL(str(_LIB_PATH))
    for name, (argtypes, restype) in _PROTOTYPES.items():
        try:
            fn = getattr(lib, name)
        except AttributeError as exc:
            raise RuntimeError(f"{name} missing from {_LIB_PATH.name}") from exc
        fn.restype = restype
        fn.argtypes = argtypes
        # ctypes silently zero-fills a short list rather than raising, so the
        # count has to be checked against the spec after the fact.
        if len(fn.argtypes) != len(argtypes):
            raise RuntimeError(
                f"{name}: argtypes arity {len(fn.argtypes)} != {len(argtypes)}"
            )
    return lib


_lib = _load()

bincount = _lib.dn_bincount
reset_scratch = _lib.dn_reset_scratch
build_b2j = _lib.dn_build_b2j
find_longest_match = _lib.dn_find_longest_match
match_blocks = _lib.dn_match_blocks
ratio = _lib.dn_ratio
quick_ratio = _lib.dn_quick_ratio
close_matches_range = _lib.dn_close_matches_range


def addr(a: np.ndarray) -> int:
    """Address of a buffer, as the 64-bit value the C ABI expects."""
    return a.ctypes.data


def i32(a) -> np.ndarray:
    return np.ascontiguousarray(a, dtype=np.int32)
