"""Station-name cleaning, shared by the renderer and the schedule matcher.

Both need to strip the same decorations, and for different reasons: the map
needs a label short enough to place, and the matcher needs a canonical form so
that a stop and the schematic node it belongs to compare equal. Keeping one set
of rules means a name that draws as "Expo / Crenshaw" also matches as it.

Feeds decorate stop names heavily. LA alone has:

    "Union Station - Metro B & D Lines"
    "7th Street / Metro Center Station - Metro A & E Lines"
    "Expo / Crenshaw K-Line Station"
    "Willowbrook - Rosa Parks Station - Metro A-Line"
"""

from __future__ import annotations

import re
import unicodedata

# " - Metro B & D Lines", " - Metro A-Line"
_LINE_LIST = re.compile(r"\s*[-–]\s*Metro\s+[A-Z0-9&,\s–-]*?Lines?\b", re.IGNORECASE)
# "Expo / Crenshaw K-Line Station" -- the designator sits mid-name
_LINE_TAG = re.compile(r"\s+[A-Z0-9]{1,3}[-–]Line\b", re.IGNORECASE)
_SUFFIX = re.compile(r"\s+Station$", re.IGNORECASE)

_NONWORD = re.compile(r"[^a-z0-9]+")
# Deliberately narrow: dropping words like "metro" would mangle real LA station
# names such as "7th Street/Metro Center".
_NOISE = re.compile(r"\b(station|stop|platform)\b")


def strip_decorations(label: str) -> str:
    """Remove line designators a feed appends to a stop name."""
    out = _LINE_LIST.sub("", label)
    out = _LINE_TAG.sub("", out)
    return re.sub(r"\s*[-–]\s*$", "", out).strip()


def display_name(label: str) -> str:
    """The name to draw on the map."""
    out = strip_decorations(label)
    # Keep the suffix when it is load-bearing: LA's "Union Station" is not
    # called "Union", and a bare one-word remnant reads as a mistake.
    trimmed = _SUFFIX.sub("", out).strip()
    if trimmed and len(trimmed.split()) > 1:
        out = trimmed
    return out or label.strip()


def normalize_name(name: str) -> str:
    """Fold a name for matching: decorations, accents, case, punctuation."""
    s = strip_decorations(name)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = _NONWORD.sub(" ", s)
    s = _NOISE.sub(" ", s)
    return " ".join(s.split())
