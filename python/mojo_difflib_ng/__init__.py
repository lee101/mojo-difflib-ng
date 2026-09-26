"""Compute-oriented subset of difflib-ng, the compiled Ratcliff/Obershelp matcher.

difflib-ng is a drop-in replacement for CPython's ``difflib`` whose distinguishing
piece is a C implementation of ``SequenceMatcher``. That matcher is the whole of
this package: ``mojo_difflib_ng.SequenceMatcher`` and ``mojo_difflib_ng
.get_close_matches`` are the covered surface, and the scan, the b2j index, the
three similarity ratios and the batched candidate scoring all run in Mojo.

The string-formatting half of difflib -- ``ndiff``, ``unified_diff``,
``context_diff``, ``HtmlDiff``, ``Differ`` -- is forwarded to the real ``difflib``
module, which is the same text these functions are defined by.
"""

from __future__ import annotations

import difflib as _difflib

from ._matcher import Match, SequenceMatcher, get_close_matches

__version__ = "0.1.0"

# Line- and character-junk predicates, straight from the stdlib: they are
# policy, not compute, and difflib owns the canonical definitions.
IS_LINE_JUNK = _difflib.IS_LINE_JUNK
IS_CHARACTER_JUNK = _difflib.IS_CHARACTER_JUNK

# Formatting and IO plumbing, forwarded to the real difflib.
Differ = _difflib.Differ
HtmlDiff = _difflib.HtmlDiff
ndiff = _difflib.ndiff
unified_diff = _difflib.unified_diff
context_diff = _difflib.context_diff
ndiff_bytes = getattr(_difflib, "ndiff_bytes", None)
unified_diff_bytes = getattr(_difflib, "unified_diff_bytes", None)
diff_bytes = getattr(_difflib, "diff_bytes", None)

__all__ = [
    "Match",
    "SequenceMatcher",
    "get_close_matches",
    "IS_LINE_JUNK",
    "IS_CHARACTER_JUNK",
    "Differ",
    "HtmlDiff",
    "ndiff",
    "unified_diff",
    "context_diff",
    "ndiff_bytes",
    "unified_diff_bytes",
    "diff_bytes",
    "__version__",
]
