"""Normalize compact public questions into the existing scored-item format.

The policy is deterministic and never consults reference labels: use the first
whole-word, exact expression occurrence in the abstract and the first whole-word
answer occurrence within that expression. Sentence boundaries use a conservative
punctuation heuristic; expression spans can never be cut by those boundaries.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import re

from .dataset import occurrences


COMPACT_FIELDS = {"expression_text", "options", "answer_id"}
LEGACY_FIELDS = {
    "question_id", "expression_type", "expression_span", "context_span",
    "blank_span", "learning_point",
}
INFERENCE_POLICY = {
    "version": "compact-first-sentence-v1",
    "expression_occurrence": "first whole-word exact occurrence in abstract",
    "answer_occurrence": "first whole-word exact occurrence inside selected expression",
    "context": "containing sentence interval; punctuation/newline boundaries with abbreviation and decimal protection",
    "offsets": "Python Unicode code points; zero-based start inclusive, end exclusive",
    "question_id": "SHA-256 of exact paper_id plus one-based question position",
}

# Treat periods in common academic abbreviations as internal punctuation. This
# intentionally prefers extra context over cutting a phrase after 'e.g.'/'Fig.'.
_ABBREVIATIONS = {
    "e.g", "i.e", "cf", "vs", "viz", "etc", "al", "approx", "resp",
    "fig", "figs", "eq", "eqs", "ref", "refs", "sec", "secs", "sect",
    "ch", "vol", "vols", "no", "nos", "pp", "p", "dr", "mr", "mrs",
    "ms", "prof", "sr", "jr", "st", "inc", "dept",
}
_BOUNDARY = re.compile(r"[.!?]+(?:[\"'”’\)\]]+)?(?=\s|$)|(?:\r\n|\r|\n)+")


def question_format(question: dict) -> str:
    """Recognize legacy metadata while rejecting compact/legacy hybrids.

    New string choices or a numeric answer key select the compact contract even
    if someone supplied legacy metadata. Fatal validation then rejects the extra
    fields instead of silently using their caller-supplied spans or identifiers.
    """
    options = question.get("options")
    compact_hint = (
        isinstance(options, list) and any(isinstance(option, str) for option in options)
    ) or isinstance(question.get("answer_id"), (int, float))
    if not (set(question) & LEGACY_FIELDS) or compact_hint:
        return "compact-v1"
    return "legacy-v1"


def generated_question_id(paper_id: str, position: int) -> str:
    # Include the full digest, not a truncated prefix. The sequence index is
    # scoped to each paper, so reordering the papers does not change identities.
    # Unknown IDs are rejected by the caller. Surrogate-pass also keeps that
    # validation path from throwing while inspecting malformed Unicode input.
    digest = hashlib.sha256(paper_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"plc-{digest}-{position + 1}"


def _abbreviation_period(text: str, position: int) -> bool:
    prefix = text[:position + 1]
    token_match = re.search(r"([A-Za-z]+(?:\.[A-Za-z]+)*)\.$", prefix)
    token = token_match.group(1) if token_match else ""
    if token.casefold() in _ABBREVIATIONS:
        return True
    # Dotted initialisms (U.S., a.k.a.) and single name initials (A. Smith).
    if re.search(r"(?:\b[A-Za-z]\.){2,}$", prefix):
        return True
    return bool(re.search(r"(?:^|\s|[\(\[])[A-Z]\.$", prefix))


def sentence_span(abstract: str, expression_start: int, expression_end: int) -> dict:
    """Return the nearest sentence interval containing the entire expression.

    A literal expression that crosses punctuation or a newline gets all of the
    containing sentences. Decimal points are never cuts because they are not
    followed by whitespace. Spaces inside the inferred interval are untouched.
    """
    if not 0 <= expression_start < expression_end <= len(abstract):
        raise ValueError("Expression span must be inside the abstract")
    boundaries = {0, len(abstract)}
    for match in _BOUNDARY.finditer(abstract):
        token = match.group(0)
        if token.startswith(".") and not token.startswith(".."):
            if _abbreviation_period(abstract, match.start()):
                continue
        boundaries.add(match.end())
    start = max(boundary for boundary in boundaries if boundary <= expression_start)
    end = min(boundary for boundary in boundaries if boundary >= expression_end)
    while start < expression_start and abstract[start].isspace():
        start += 1
    while end > expression_end and abstract[end - 1].isspace():
        end -= 1
    return {"start": start, "end": end}


def normalize_question(question: dict, abstract: str, paper_id: str, position: int) -> tuple[dict, list[dict]]:
    """Normalize after fatal contract validation; retain scoreable failures."""
    if question_format(question) == "legacy-v1":
        item = deepcopy(question)
        item["input_format"] = "legacy-v1"
        return item, []

    issues = []
    text = question["expression_text"]
    raw_options = question.get("options")
    raw_answer_id = question.get("answer_id")
    options = deepcopy(raw_options)
    if isinstance(raw_options, list):
        options = [
            {"id": "ABCD"[index] if index < 4 else f"extra-{index + 1}", "text": deepcopy(value)}
            for index, value in enumerate(raw_options)
        ]
    answer_valid = type(raw_answer_id) is int and 0 <= raw_answer_id <= 3
    answer_id = "ABCD"[raw_answer_id] if answer_valid else None
    if not answer_valid:
        issues.append({"code": "invalid_answer_index", "message": "answer_id는 0~3 사이의 정수여야 합니다. 문자열·실수·bool은 허용하지 않습니다."})

    expression_positions = occurrences(abstract, text, whole_words=True)
    expression_span = None
    context_span = None
    blank_span = None
    answer_positions = []
    if expression_positions:
        start = expression_positions[0]
        expression_span = {"start": start, "end": start + len(text)}
        context_span = sentence_span(abstract, expression_span["start"], expression_span["end"])

    if answer_valid and isinstance(raw_options, list) and raw_answer_id < len(raw_options):
        answer = raw_options[raw_answer_id]
        if isinstance(answer, str) and answer.strip():
            answer_positions = occurrences(text, answer, whole_words=True)
            if not answer_positions:
                issues.append({"code": "answer_not_in_expression", "message": "정답 보기 문자열이 학습 표현 안에 온전한 단어·구절로 정확히 존재하지 않습니다."})
            elif expression_span:
                start = expression_span["start"] + answer_positions[0]
                blank_span = {"start": start, "end": start + len(answer)}

    item = {
        "question_id": generated_question_id(paper_id, position),
        "expression_text": text,
        "expression_span": expression_span,
        "context_span": context_span,
        "blank_span": blank_span,
        "options": options,
        "answer_id": answer_id,
        "input_format": "compact-v1",
        "submitted_question": deepcopy(question),
        "inference": {
            "policy_version": INFERENCE_POLICY["version"],
            "question_position": position + 1,
            "expression_occurrence": 0 if expression_positions else None,
            "expression_occurrence_count": len(expression_positions),
            "answer_occurrence": 0 if answer_positions else None,
            "answer_occurrence_count": len(answer_positions),
        },
    }
    return item, issues
