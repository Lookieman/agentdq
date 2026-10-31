# ---------------------------------------------------------------------------
# src/agents/text_normaliser.py
# v1.1 | 11-Aug-2026 | Package 4g. Attribute extraction for the veto: the digit
#                      groups and the material words of a normalised text. The
#                      matcher compares the two signatures and caps a pair whose
#                      signatures disagree.
# v1.0 | 04-Aug-2026 | Package 4c. One text normaliser, used by BOTH scoring
#                      rungs. The embeddings builder normalises before it
#                      encodes, and the matcher normalises before it scores, so
#                      the fuzzy score and the semantic score always describe
#                      the same text.
# ---------------------------------------------------------------------------
"""Make two texts comparable before a score is calculated.

Normalisation removes the differences that carry no meaning: letter case,
extra spaces, and punctuation. "Hex Bolt", "HEX BOLT" and "Hex - Bolt  " all
become "hex bolt".

Both scoring rungs must use this function. If the fuzzy rung and the semantic
rung see different text, their two scores describe different things, and the
weighted mean of the two means nothing. Nothing would fail, and every score
would be quietly wrong.

NORMALISATION_VERSION goes into the identity code of an embeddings artefact. A
change to the rules below changes that version, and the matcher then knows the
stored vectors were built from different text.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Change this string when the rules in normalise_text change. Every embeddings
# artefact built under the old rules then reports as out of date.
NORMALISATION_VERSION: str = "1.0"

# Punctuation becomes a SPACE, not nothing. "Hex-Bolt" must become "hex bolt"
# and not "hexbolt", because the second one hides a word boundary that both
# rungs need. \w keeps letters and digits in any language, so an accented
# description survives.
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+", re.UNICODE)


def normalise_text(value: Any) -> str:
    """Return the comparable form of one description.

    The steps, in order:

        1. An absent value becomes an empty string.
        2. Accented characters are decomposed and their marks removed, so
           "Ventil fur Pumpe" and "Ventil fuer Pumpe" do not diverge on the
           accent alone.
        3. Letters become lower case.
        4. Punctuation becomes a space.
        5. Repeated spaces become one space, and the ends are trimmed.

    An empty result is meaningful: the record carries no text worth matching,
    so it takes no part in deduplication.
    """
    text: str = ""
    decomposed: str = ""
    stripped: str = ""

    if value is None:
        return ""
    text = str(value)
    # pandas gives the string "nan" for a missing value in an object column.
    if text.strip().lower() in {"", "nan", "none"}:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    stripped = stripped.lower()
    stripped = _PUNCTUATION.sub(" ", stripped)
    stripped = _WHITESPACE.sub(" ", stripped)
    return stripped.strip()


def is_matchable(value: Any) -> bool:
    """Report whether a value holds text worth matching on.

    A record whose description normalises to nothing has no evidence of
    identity. The matcher holds such a record out of deduplication rather than
    scoring it against everything and calling it unique.
    """
    return normalise_text(value) != ""


# ---------------------------------------------------------------------------
# Attribute extraction (v1.1, Package 4g)
# ---------------------------------------------------------------------------
# Digit groups, wherever they sit. "m8" yields "8" and "5in" yields "5", so a
# unit-word twin ("5 inches" against "5in") keeps the SAME digit signature and
# is never vetoed, while "6203" against "6204" differs and is.
_DIGIT_GROUPS = re.compile(r"\d+")


def digit_signature(normalised_text: str) -> frozenset[str]:  # v1.1
    """The set of digit groups in one normalised text."""
    return frozenset(_DIGIT_GROUPS.findall(normalised_text or ""))


def material_signature(  # v1.1
    normalised_text: str,
    material_words: list[str],
) -> frozenset[str]:
    """The set of material words in one normalised text.

    Longest phrases are matched first and removed, so "stainless steel" is one
    material and never also counts as "steel".
    """
    found: set[str] = set()
    working: str = f" {normalised_text or ''} "
    word: str = ""

    for word in sorted(material_words, key=len, reverse=True):
        if f" {word} " in working:
            found.add(word)
            working = working.replace(f" {word} ", " ")
    return frozenset(found)


def signatures_disagree(  # v1.1
    left_digits: frozenset[str],
    right_digits: frozenset[str],
    left_materials: frozenset[str],
    right_materials: frozenset[str],
) -> str:
    """Return the veto reason, or an empty string for no veto.

    A veto needs evidence on BOTH sides. A side with no digits or no material
    word never vetoes, because absence is not disagreement.
    """
    if left_digits and right_digits and left_digits != right_digits:
        return "digits"
    if left_materials and right_materials and left_materials != right_materials:
        return "material"
    return ""
