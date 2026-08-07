# ---------------------------------------------------------------------------
# src/agents/text_normaliser.py
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
