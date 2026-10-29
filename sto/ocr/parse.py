"""Turn raw OCR text from the mobiGlas app bar into an aUEC balance.

The app bar shows the balance as "¤ 1,234,567" with the player name under it.
OCR mixes up some glyphs (O/0, l/1, S/5, Z/2, B/8) and may read the ¤ sign as
a letter, so tokens are mapped and checked for proper thousands grouping.
"""
from __future__ import annotations

import re

# Letters OCR returns in place of digits.
CONFUSIONS = str.maketrans({
    "O": "0", "o": "0", "D": "0", "Q": "0", "U": "0",
    "l": "1", "I": "1", "i": "1", "|": "1", "!": "1", "L": "1",
    "Z": "2", "z": "2",
    "S": "5", "s": "5",
    "B": "8",
    "G": "6", "b": "6",
    "T": "7",
    "g": "9", "q": "9",
})
SEPARATORS = ",.'’  "
UNIT = re.compile(r"a?uec", re.IGNORECASE)
CURRENCY = re.compile(r"[¤¢$€£]")


def _grouped_value(token: str) -> int | None:
    """Value of one token if, after mapping look-alike letters, it is a plain
    number ("150000") or a properly grouped one ("1,234,567" / "1.234.567")."""
    mapped = token.translate(CONFUSIONS)
    if not mapped or not re.fullmatch(rf"[0-9{re.escape(SEPARATORS)}]+", mapped):
        return None
    groups = re.split(rf"[{re.escape(SEPARATORS)}]", mapped)
    if any(g == "" for g in groups):
        return None
    # Groups after the first must be whole thousands groups. OCR drops
    # separators ("2257.549", "2.275177") but that never loses a digit; a group
    # of 1, 2, 4 or 5 digits means a digit was lost or invented.
    if len(groups) > 1 and any(len(g) % 3 for g in groups[1:]):
        return None
    return int("".join(groups))


def _token_value(token: str) -> int | None:
    # A letter glued in front of a real digit is most likely the ¤ sign misread.
    if len(token) > 1 and token[0].isalpha() and token[1].isdigit():
        rest = _grouped_value(token[1:])
        if rest is not None:
            return rest
    return _grouped_value(token)


def candidates(text: str) -> list[tuple[int, int]]:
    """(digit count, value) for every number-like run in the text."""
    out = []
    for line in text.splitlines():
        line = CURRENCY.sub(" ", UNIT.sub(" ", line))
        line = re.sub(rf"\s*([{re.escape(SEPARATORS)}])\s*", r"\1", line)
        tokens = line.split()
        i = 0
        while i < len(tokens):
            value = _token_value(tokens[i])
            if value is None:
                i += 1
                continue
            # Space-grouped thousands: "2 257 549".
            digits = str(value)
            j = i + 1
            if len(tokens[i].translate(CONFUSIONS)) <= 3:
                while j < len(tokens) and len(tokens[j]) == 3 and _grouped_value(tokens[j]) is not None:
                    digits += tokens[j].translate(CONFUSIONS)
                    j += 1
            out.append((len(digits.lstrip("0")) or 1, int(digits)))
            i = j
    return out


def parse_balance(text: str) -> int | None:
    """The balance in the text, or None when nothing in it reads as a number.
    With several numbers (a whole-screen test image), the longest one wins."""
    found = [c for c in candidates(text or "") if c]
    if not found:
        return None
    return max(found)[1]
