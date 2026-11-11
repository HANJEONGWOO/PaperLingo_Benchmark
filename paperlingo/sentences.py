"""Conservative sentence counting for the abstract-length eligibility rule.

This does not alter source text or the existing question-context inference.
Line wraps, common academic abbreviations, decimals and TeX math punctuation
are not sentence boundaries. A period followed by lowercase prose is treated
as an abbreviation/continuation. A final unpunctuated sentence is counted too.
"""

import re
from functools import lru_cache


SENTENCE_POLICY_VERSION = "abstract-sentences-v1"
_ABBREVIATIONS = {
    "e.g", "i.e", "cf", "vs", "viz", "etc", "al", "approx", "resp",
    "fig", "figs", "eq", "eqs", "ref", "refs", "sec", "secs", "sect",
    "ch", "vol", "vols", "no", "nos", "pp", "p", "dr", "mr", "mrs",
    "ms", "prof", "sr", "jr", "st", "inc", "dept", "thm", "prop",
    "cor", "def", "defs", "lem", "phys", "rev", "lett", "math", "sci",
}
_MATH = re.compile(r"\$\$[\s\S]*?\$\$|(?<!\\)\$(?:\\.|[^$])*?(?<!\\)\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]")
_ENDING = re.compile(r"[.!?]+(?:[\"'”’\)\]]+)?(?=\s|$)")
_SENTENCE_STARTERS = {
    "We", "The", "This", "These", "Those", "It", "Our", "In", "For", "From",
    "As", "At", "An", "A", "Using", "To", "By", "However", "Thus", "Finally",
    "Moreover", "Here", "Both", "Such", "They", "There", "Results",
}


def _groups(text: str) -> list[tuple[int, int]]:
    stack, groups = [], []
    for index, char in enumerate(text):
        if char in "([":
            stack.append((char, index))
        elif char in ")]" and stack and stack[-1][0] == {")": "(", "]": "["}[char]:
            _, start = stack.pop()
            groups.append((start, index))
    return groups


def abstract_sentences(abstract: str) -> list[str]:
    """Return source excerpts; never count a standalone heading/formula."""
    masked = _MATH.sub(lambda match: " " * len(match.group()), abstract)
    groups = _groups(masked)
    result, start = [], 0
    for match in _ENDING.finditer(masked):
        punctuation = match.group()
        if any(left < match.end() <= right for left, right in groups):
            continue
        if punctuation.startswith(".."):
            continue
        if punctuation.startswith("."):
            if re.match(r"\s*[\"'“‘\(\[]*[a-z]", abstract[match.end():]):
                continue
            prefix = masked[:match.start() + 1]
            token = re.search(r"([A-Za-z]+(?:\.[A-Za-z]+)*)\.$", prefix)
            word = token.group(1) if token else ""
            if word.casefold() in _ABBREVIATIONS and (word.islower() or word.isupper() or word.istitle()):
                continue
            if re.search(r"(?:\b[A-Za-z]\.){2,}$", prefix):
                continue
            if re.search(r"(?:^|\s|[\(\[])[A-Z]\.$", prefix):
                following = re.match(r"\s*([A-Z][a-z]+)\b", abstract[match.end():])
                if not following or following.group(1) not in _SENTENCE_STARTERS:
                    continue
        end = match.end()
        if len(re.findall(r"[A-Za-z]+", masked[start:end])) >= 2:
            result.append(abstract[start:end].strip())
        start = end
    if len(re.findall(r"[A-Za-z]+", masked[start:])) >= 2:
        result.append(abstract[start:].strip())
    return result


@lru_cache(maxsize=4096)
def sentence_count(abstract: str) -> int:
    return len(abstract_sentences(abstract))
