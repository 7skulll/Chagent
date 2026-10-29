"""Edit matching that tolerates copy drift but never guesses a location.

An ``edit_file`` call carries several ``old_text``/``new_text`` pairs, and every
``old_text`` is matched against the *original* file, not against the result of
the previous pair. Three consequences follow, and all three are deliberate:

* one call can change several places, so the model does not need one turn per
  edit;
* a pair that only becomes matchable after an earlier pair is rejected instead
  of silently applied to shifted text;
* the file is written once, after every pair resolved, so a partial edit never
  reaches disk.

Matching walks a fixed ladder: exact, then folded (curly quotes, dash variants,
odd spaces, full-width forms), then whole-line with trailing whitespace ignored,
then a near-identical line window. Anything less precise than "exactly one
location" is an error with the surrounding lines attached, because a wrong edit
costs more than a failed one.
"""

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
import unicodedata


# Characters that models routinely substitute when copying source text. Every
# entry maps one character to exactly one character, so folded offsets equal
# original offsets and a folded match can be spliced without a translation table.
_FOLD = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u3000": " ",
})
_fold_cache: dict[str, str] = {}

# A near-identical window must be clearly the only candidate: below this the
# tool reports a miss and shows the region instead of editing the wrong lines.
SIMILARITY_FLOOR = 0.97

# Scoring a window is quadratic in the size of old_text, and the near-match
# stage scores one window per line: a 20-line miss on a 400-line file costs
# ~5s, a 40-line one ~19s. Past this many compared characters the stage stops
# scanning every window and scores only the ones an exactly matching line
# points at. The bound is measured, not guessed: ~21ns per compared character
# puts this limit near 0.6s worst case.
SIMILARITY_WORK_LIMIT = 30_000_000
# With the bound in force the reported region is the best of a handful of
# candidates rather than the global best; that is the right trade for a
# diagnostic that is only ever shown alongside "nothing was written".
SIMILARITY_MAX_CANDIDATES = 8
# A short line ("}", "    return x") appears in every window and votes for
# nothing. Only lines long enough to be distinctive can pick a location.
ANCHOR_MIN_CHARS = 8


@dataclass(frozen=True, slots=True)
class Edit:
    old_text: str
    new_text: str


@dataclass(frozen=True, slots=True)
class AppliedEdit:
    index: int
    start: int
    end: int
    strategy: str


class EditError(ValueError):
    """A failed edit with a model-facing explanation already formatted."""


def fold_char(character: str) -> str:
    """Normalize one character to another single character.

    NFKC is applied only when it keeps the length, so the folded text always has
    the same offsets as the original.
    """
    folded = _fold_cache.get(character)
    if folded is None:
        folded = character.translate(_FOLD)
        if folded == character:
            normalized = unicodedata.normalize("NFKC", character)
            folded = normalized if len(normalized) == 1 else character
        _fold_cache[character] = folded
    return folded


def fold_text(text: str) -> str:
    return "".join(fold_char(character) for character in text)


def _line_spans(original: str) -> tuple[list[str], list[int]]:
    """Split keeping endings, and return the start offset of every line."""
    lines = original.splitlines(keepends=True)
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    return lines, starts


def _window_span(original: str, lines: list[str], starts: list[int], first: int, count: int,
                 *, keep_final_newline: bool) -> tuple[int, int]:
    last = first + count - 1
    start = starts[first]
    end = starts[last] + len(lines[last])
    if not keep_final_newline:
        end -= len(lines[last]) - len(lines[last].rstrip("\r\n"))
    return start, end


def _quote_lines(lines: list[str], first: int, count: int, *, mark: int | None = None) -> str:
    """Render an excerpt with 1-based line numbers; ``mark`` highlights one line."""
    if not lines:
        return "  [file is empty]"
    first = max(0, min(first, len(lines) - 1))
    last = min(len(lines), first + count)
    width = len(str(last))
    rendered = []
    for number in range(first + 1, last + 1):
        pointer = "->" if mark == number else "  "
        rendered.append(f"  {pointer} L{str(number).rjust(width)} | {lines[number - 1]}")
    return "\n".join(rendered)


def _occurrences(haystack: str, needle: str) -> list[int]:
    positions: list[int] = []
    start = haystack.find(needle)
    while start != -1:
        positions.append(start)
        start = haystack.find(needle, start + 1)
    return positions


def _line_of(original: str, offset: int) -> int:
    return original.count("\n", 0, offset) + 1


def _normalized_lines(lines: list[str]) -> list[str]:
    return [fold_text(line).rstrip() for line in lines]


def _score_windows(normalized: list[str], target: list[str], starts) -> tuple[int, float]:
    """Best similarity over the given window starts, plus the start itself."""
    count = len(target)
    joined = "\n".join(target)
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq1(joined)
    best_index, best_score = -1, 0.0
    for start in starts:
        matcher.set_seq2("\n".join(normalized[start:start + count]))
        score = matcher.ratio()
        if score > best_score:
            best_index, best_score = start, score
    return best_index, best_score


def _anchor_windows(normalized: list[str], target: list[str], limit: int) -> list[int]:
    """Window starts whose offset the target's distinctive lines agree on.

    A near-identical block keeps most of its lines exact, so each distinctive
    line votes for ``line_number - position_in_target`` and the real region is
    the offset with the most votes. One pass over the file, then only a handful
    of windows need scoring.
    """
    count = len(target)
    span = len(normalized) - count
    if span < 0:
        return []
    seen = Counter(target)
    positions = {line: position for position, line in enumerate(target)
                 if len(line) >= ANCHOR_MIN_CHARS and seen[line] == 1}
    if not positions:
        return []
    votes: dict[int, int] = {}
    for index, line in enumerate(normalized):
        position = positions.get(line)
        if position is None:
            continue
        start = index - position
        if 0 <= start <= span:
            votes[start] = votes.get(start, 0) + 1
    ranked = sorted(votes.items(), key=lambda item: (-item[1], item[0]))
    return [start for start, _ in ranked[:limit]]


def near_match(normalized: list[str], target: list[str]) -> tuple[int, float] | None:
    """Closest line window and its similarity, or ``None`` when it is not worth it.

    Both branches compare at most ``SIMILARITY_WORK_LIMIT`` characters, so the
    cost is bounded by the budget rather than by the file or by ``old_text``.
    ``None`` means "no region worth quoting": nothing in the file shares a
    distinctive line with ``target``, so no window is a candidate at all.
    """
    count = len(target)
    if count == 0 or count > len(normalized):
        return None
    windows = len(normalized) - count + 1
    joined = "\n".join(target)
    # One full ratio() compares every character of the window with every
    # character of the target, so this is the unit the budget is spent in.
    cost = max(len(joined) ** 2, 1)
    if windows * cost <= SIMILARITY_WORK_LIMIT:
        return _score_windows(normalized, target, range(windows))
    affordable = max(1, SIMILARITY_WORK_LIMIT // cost)
    candidates = _anchor_windows(normalized, target, min(SIMILARITY_MAX_CANDIDATES, affordable))
    if not candidates:
        return None
    return _score_windows(normalized, target, candidates)


def _miss(lines: list[str], index: int, path: str, near: tuple[int, float] | None,
          window: int) -> EditError:
    head = (
        f"edits[{index}].old_text was not found in {path} ({len(lines)} lines).\n"
        "Quotes, dashes and spaces are folded, and trailing whitespace per line is ignored.\n"
    )
    tail = (
        "Re-read the file and copy the exact lines into old_text, indentation included. "
        "Nothing was written."
    )
    if near is None or near[0] < 0:
        return EditError(head + "No region is close enough to quote.\n" + tail)
    first, score = near
    excerpt = _quote_lines(lines, first, max(window, 1) + 2)
    return EditError(
        head + f"The closest region scores {score:.2f} similarity.\n"
        f"Closest region (lines {first + 1}-{min(len(lines), first + window + 2)}):\n{excerpt}\n" + tail
    )


def _ambiguous(original: str, edit: Edit, index: int, path: str, offsets: list[int]) -> EditError:
    listed = ", ".join(f"L{_line_of(original, offset)}" for offset in offsets[:3])
    more = "" if len(offsets) <= 3 else f" (and {len(offsets) - 3} more)"
    return EditError(
        f"edits[{index}].old_text matches {len(offsets)} places in {path}: {listed}{more}.\n"
        "Add surrounding lines so exactly one place matches. Nothing was written."
    )


def locate(original: str, edit: Edit, index: int, path: str) -> AppliedEdit:
    """Resolve one edit against the original text or raise a diagnostic error."""
    old = edit.old_text
    if not old:
        raise EditError(f"edits[{index}].old_text must not be empty. Nothing was written.")

    exact = _occurrences(original, old)
    if len(exact) == 1:
        return AppliedEdit(index, exact[0], exact[0] + len(old), "exact")
    if len(exact) > 1:
        raise _ambiguous(original, edit, index, path, exact)

    folded_original, folded_old = fold_text(original), fold_text(old)
    folded = _occurrences(folded_original, folded_old)
    if len(folded) == 1:
        start = folded[0]
        return AppliedEdit(index, start, start + len(old), "folded")
    if len(folded) > 1:
        raise _ambiguous(original, edit, index, path, folded)

    # Whole-line stages: the model usually drops trailing spaces and may drift
    # on indentation, so compare line images without them.
    lines, starts = _line_spans(original)
    normalized = _normalized_lines(lines)
    target = _normalized_lines(old.splitlines())
    keep_final_newline = old.endswith("\n")
    near = None
    if target:
        count = len(target)
        windows = [index_ for index_ in range(len(normalized) - count + 1)
                   if normalized[index_:index_ + count] == target]
        if len(windows) == 1:
            start, end = _window_span(original, lines, starts, windows[0], count,
                                      keep_final_newline=keep_final_newline)
            return AppliedEdit(index, start, end, "lines")
        if len(windows) > 1:
            raise _ambiguous(original, edit, index, path,
                             [_window_span(original, lines, starts, window, count,
                                           keep_final_newline=keep_final_newline)[0]
                              for window in windows])
        # Computed once: the failure message quotes the same search, and paying
        # for it twice made every miss cost double what it already costs.
        near = near_match(normalized, target)
        if near is not None and near[0] >= 0 and near[1] >= SIMILARITY_FLOOR:
            start, end = _window_span(original, lines, starts, near[0], count,
                                      keep_final_newline=keep_final_newline)
            return AppliedEdit(index, start, end, "similar")

    raise _miss(lines, index, path, near, len(target))


def apply_edits(original: str, edits: list[Edit], path: str) -> tuple[str, list[AppliedEdit]]:
    """Apply every edit to ``original`` and return the new text plus outcomes.

    Raises before any write when an edit cannot be placed unambiguously or two
    edits claim overlapping text.
    """
    if not edits:
        raise EditError("edits must contain at least one {old_text, new_text} pair. Nothing was written.")
    applied = [locate(original, edit, index, path) for index, edit in enumerate(edits)]
    ordered = sorted(applied, key=lambda item: (item.start, item.end))
    for previous, current in zip(ordered, ordered[1:]):
        if current.start < previous.end:
            raise EditError(
                f"edits[{current.index}] overlaps edits[{previous.index}] in {path}: "
                f"the first covers characters {previous.start}-{previous.end}, "
                f"the second starts at {current.start}. Merge them into one pair. Nothing was written."
            )
    updated = original
    for item in sorted(applied, key=lambda entry: entry.start, reverse=True):
        edit = edits[item.index]
        updated = updated[:item.start] + edit.new_text + updated[item.end:]
    return updated, applied
