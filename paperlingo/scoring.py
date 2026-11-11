"""Deterministic expression scoring (exact plus partial credit) and question integrity checks.

Quality judgements are separate from expression matching. This module never
assigns an LLM quality score or silently removes a failed generated question.
"""

from __future__ import annotations

from copy import deepcopy
import math
import re

from .dataset import Dataset, domain_code, occurrences, word_boundary
from .code_review import CODE_REVIEW_POLICY, code_quality, normalize_option
from .submission import COMPACT_FIELDS, INFERENCE_POLICY, MAX_QUESTIONS_PER_PAPER, generated_question_id, normalize_question, question_format


METRICS_VERSION = "soft-rq-v4"
# Partial credit compares word tokens at their positions in the source abstract.
# Function-word weighting follows tests/soft_match.py; positions replace its lemmas.
FUNCTION_WEIGHT = 0.3
FUNCTION_WORDS = frozenset("""
a an the this that these those such
of in on at to for from by with about as into onto upon within without
through over under between among against during before after above below
and or but nor so yet if than then because while whereas although though
is am are was were be been being have has had do does did
it its they them their we our us he she his her i you your
which who whom whose what where when how there here
not no can could may might must shall should will would
also very more most less least much many some any each every all both
""".split())
MATCH_POLICY = {
    "version": "weighted-span-jaccard-v2",
    "unit": "word tokens at their positions in the abstract; function words weigh 0.3, others 1.0",
    "credit": "w(submitted ∩ reference) / w(submitted ∪ reference); exact text = 1.0",
    "case": "ignored for exact and partial matching",
    "guard": "no credit unless the overlap has a content word, spans two or more words, or contains the whole reference",
    "assignment": "one-to-one; exact text first, then the assignment maximizing total credit",
}
_TOKEN = re.compile(r"\w+(?:[-'’]\w+)*")
QUALITY_POLICY_VERSION = "auto-conservative-v1"
QUESTION_FIELDS = {
    "question_id", "expression_text", "expression_type", "expression_span",
    "context_span", "blank_span", "options", "answer_id", "learning_point",
}


class SubmissionError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def empty_quality(*, structural: bool = False) -> dict:
    return {
        "status": "complete" if structural else "pending",
        "source": "structural" if structural else None,
        "q": 0.0 if structural else None,
        "accepted_option_ids": [],
        "rubric": None,
        "gates": None,
        "option_assessments": [],
        "rationale": "문항 구조 검증에 실패하여 품질 점수가 0점입니다." if structural else "",
        "reviewer": None,
        "review_recommended": False,
    }


def _fatal_validation(dataset: Dataset, predictions: list[dict], paper_ids: list[str], scope_name: str) -> None:
    errors = []
    if not isinstance(paper_ids, list) or not paper_ids:
        raise SubmissionError(["평가 범위에 논문 ID가 한 개 이상 필요합니다."])
    if any(not isinstance(paper_id, str) or not paper_id for paper_id in paper_ids):
        raise SubmissionError(["평가 범위의 paper_id는 비어 있지 않은 문자열이어야 합니다."])
    if len(set(paper_ids)) != len(paper_ids):
        errors.append("평가 범위의 paper_id가 중복되었습니다.")
    unknown_scope = set(paper_ids) - set(dataset.papers)
    if unknown_scope:
        errors.append(f"평가 범위에 알 수 없는 논문 ID가 있습니다: {', '.join(sorted(unknown_scope)[:5])}")
    if not isinstance(scope_name, str) or not scope_name or len(scope_name) > 100:
        errors.append("scope_name은 1~100자의 문자열이어야 합니다.")
    if not isinstance(predictions, list):
        raise SubmissionError([*errors, "predictions는 논문 결과 객체의 배열이어야 합니다."])
    # Prevent pathological payloads before walking deeply nested records.
    if len(predictions) > len(dataset.papers):
        errors.append("제출된 논문 수가 데이터셋 전체 논문 수보다 많습니다.")
    seen_papers, seen_questions = set(), set()
    scope_ids = set(paper_ids)
    for index, prediction in enumerate(predictions):
        prefix = f"predictions[{index}]"
        if not isinstance(prediction, dict):
            errors.append(f"{prefix}: 논문 결과는 객체여야 합니다.")
            continue
        extra = set(prediction) - {"paper_id", "questions"}
        if extra:
            errors.append(f"{prefix}: 허용하지 않는 필드 {', '.join(sorted(map(str, extra)))}")
        paper_id = prediction.get("paper_id")
        if not isinstance(paper_id, str) or not paper_id or len(paper_id) > 200:
            errors.append(f"{prefix}: paper_id는 1~200자의 문자열이어야 합니다.")
        else:
            if paper_id in seen_papers:
                errors.append(f"{prefix}: 중복 paper_id {paper_id}")
            seen_papers.add(paper_id)
            if paper_id not in dataset.papers:
                errors.append(f"{prefix}: 알 수 없는 paper_id {paper_id}")
            elif paper_id not in scope_ids:
                errors.append(f"{prefix}: 평가 범위 밖의 paper_id {paper_id}")
        questions = prediction.get("questions")
        if not isinstance(questions, list):
            errors.append(f"{prefix}: questions 배열이 필요합니다.")
            continue
        if len(questions) > MAX_QUESTIONS_PER_PAPER:
            errors.append(f"{prefix}: 논문당 문항은 최대 {MAX_QUESTIONS_PER_PAPER}개입니다. 임의로 잘라내지 않습니다.")
        for position, question in enumerate(questions):
            label = f"{prefix}.questions[{position}]"
            if not isinstance(question, dict):
                errors.append(f"{label}: 문항은 객체여야 합니다.")
                continue
            input_format = question_format(question)
            allowed_fields = COMPACT_FIELDS if input_format == "compact-v1" else QUESTION_FIELDS
            extra = set(question) - allowed_fields
            if extra:
                errors.append(f"{label}: 허용하지 않는 필드 {', '.join(sorted(map(str, extra)))}")
            question_id = (
                generated_question_id(paper_id, position)
                if input_format == "compact-v1" and isinstance(paper_id, str)
                else question.get("question_id")
            )
            if not isinstance(question_id, str) or not question_id.strip() or len(question_id) > 200:
                errors.append(f"{label}: question_id는 1~200자의 문자열이어야 합니다.")
            else:
                if question_id in seen_questions:
                    errors.append(f"{label}: 중복 question_id {question_id}")
                seen_questions.add(question_id)
            text = question.get("expression_text")
            if not isinstance(text, str) or len(text) > 1000:
                errors.append(f"{label}: expression_text는 최대 1000자의 문자열이어야 합니다.")
    if errors:
        raise SubmissionError(errors[:100])


def _validate_item(question: dict, abstract: str, *, duplicate: bool, initial_issues: list[dict] | None = None) -> dict:
    item = deepcopy(question)
    issues = deepcopy(initial_issues or [])

    def issue(code: str, message: str) -> None:
        issues.append({"code": code, "message": message})

    text = question["expression_text"]
    if not text.strip():
        issue("empty_expression", "학습 표현이 비어 있습니다.")
    elif not occurrences(abstract, text):
        issue("expression_not_in_source", "학습 표현이 원문 abstract에 정확히 존재하지 않습니다.")
    elif not occurrences(abstract, text, whole_words=True):
        issue("expression_word_boundary", "학습 표현이 다른 단어의 일부로만 존재합니다.")
    if duplicate:
        issue("duplicate_expression", "같은 논문에서 이미 제출한 표현입니다. 추가 정답으로 인정하지 않습니다.")
    for name, limit in (("expression_type", 100), ("learning_point", 2000)):
        if name in question and (not isinstance(question[name], str) or len(question[name]) > limit):
            issue(f"invalid_{name}", f"{name}은 최대 {limit}자의 문자열이어야 합니다.")

    spans = {}
    for name in ("expression_span", "context_span", "blank_span"):
        value = question.get(name)
        if (
            not isinstance(value, dict) or set(value) != {"start", "end"}
            or type(value.get("start")) is not int or type(value.get("end")) is not int
            or not 0 <= value["start"] < value["end"] <= len(abstract)
        ):
            issue(f"invalid_{name}", f"{name}은 원문 안의 유효한 정수 위치 [start, end)여야 합니다.")
        else:
            spans[name] = value

    expression, context, blank = (spans.get(name) for name in ("expression_span", "context_span", "blank_span"))
    if expression:
        if abstract[expression["start"]:expression["end"]] != text:
            issue("expression_span_mismatch", "expression_span의 원문이 expression_text와 일치하지 않습니다.")
        if not word_boundary(abstract, expression["start"], expression["end"]):
            issue("expression_span_word_boundary", "expression_span이 단어 중간에서 시작하거나 끝납니다.")
    if expression and blank and not expression["start"] <= blank["start"] < blank["end"] <= expression["end"]:
        issue("blank_outside_expression", "빈칸 위치가 학습 표현 안에 있지 않습니다.")
    if blank and not word_boundary(abstract, blank["start"], blank["end"]):
        issue("blank_word_boundary", "빈칸이 단어 중간에서 시작하거나 끝납니다. 온전한 단어 또는 구절을 지정하세요.")
    if expression and context and not context["start"] <= expression["start"] < expression["end"] <= context["end"]:
        issue("expression_outside_context", "학습 표현 전체가 문제 문맥 안에 있어야 합니다.")
    item["context"] = ""
    if context and blank:
        if context["start"] <= blank["start"] < blank["end"] <= context["end"]:
            item["context"] = abstract[context["start"]:blank["start"]] + "[BLANK]" + abstract[blank["end"]:context["end"]]
            if item["context"].count("[BLANK]") != 1:
                issue("ambiguous_blank_marker", "학습자 문맥에 빈칸 표시가 두 개 이상 있습니다.")
        else:
            issue("blank_outside_context", "빈칸 위치가 문제 문맥 안에 있지 않습니다.")

    options = question.get("options")
    option_map = {}
    normalized_texts = set()
    if not isinstance(options, list) or len(options) != 4:
        issue("option_count", "선택지는 정확히 4개여야 합니다.")
    if isinstance(options, list):
        for position, option in enumerate(options):
            if not isinstance(option, dict) or set(option) != {"id", "text"}:
                issue("invalid_option", f"선택지 {position + 1}은 id, text 필드만 있는 객체여야 합니다.")
                continue
            option_id, option_text = option.get("id"), option.get("text")
            if not isinstance(option_id, str) or option_id not in ("A", "B", "C", "D"):
                issue("invalid_option_id", "선택지 ID는 A, B, C, D 중 하나여야 합니다.")
                continue
            if option_id in option_map:
                issue("duplicate_option_id", "선택지 ID가 중복되었습니다.")
            option_map[option_id] = option_text
            if not isinstance(option_text, str) or not option_text.strip() or len(option_text) > 1000:
                issue("invalid_option_text", "선택지 text는 1~1000자의 문자열이어야 합니다.")
                continue
            normalized = normalize_option(option_text)
            if normalized in normalized_texts:
                issue("duplicate_option_text", "대소문자·공백·유니코드 표기를 정규화하면 중복되는 선택지가 있습니다.")
            normalized_texts.add(normalized)
        if set(option_map) != {"A", "B", "C", "D"}:
            issue("missing_option_ids", "A, B, C, D 선택지를 각각 한 번씩 제공해야 합니다.")
    answer_id = question.get("answer_id")
    if not isinstance(answer_id, str) or answer_id not in option_map:
        issue("invalid_answer_id", "answer_id가 유효한 선택지를 가리키지 않습니다.")
    elif blank and option_map[answer_id] != abstract[blank["start"]:blank["end"]]:
        issue("answer_not_source", "정답을 빈칸에 넣어도 원문 abstract가 정확히 복원되지 않습니다.")

    item["issues"] = issues
    item["structure_valid"] = not issues
    item["quality"] = empty_quality(structural=bool(issues))
    return item


def _tokens(abstract: str) -> tuple[list[tuple[int, int]], list[float]]:
    spans = [match.span() for match in _TOKEN.finditer(abstract)]
    weights = [FUNCTION_WEIGHT if abstract[a:b].casefold() in FUNCTION_WORDS else 1.0 for a, b in spans]
    return spans, weights


def _occurrences_ignoring_case(abstract: str, text: str) -> list[tuple[int, int]]:
    """Whole-word spans of text in the abstract, ignoring case (overlaps allowed)."""
    if not text.strip():
        return []
    pattern = re.compile(f"(?=({re.escape(text)}))", re.IGNORECASE)
    return [match.span(1) for match in pattern.finditer(abstract) if word_boundary(abstract, *match.span(1))]


def _token_sets(abstract: str, spans: list[tuple[int, int]], text: str) -> list[frozenset[int]]:
    """Token indices covered by each whole-word occurrence of text, ignoring case."""
    found = []
    for start, end in _occurrences_ignoring_case(abstract, text):
        covered = frozenset(index for index, (a, b) in enumerate(spans) if a < end and b > start)
        if covered and covered not in found:
            found.append(covered)
    return found


def match_credit(submitted: list[frozenset[int]], reference: list[frozenset[int]], weights: list[float]) -> tuple[float, str | None]:
    """Best weighted token Jaccard over occurrence pairs and how the spans relate.

    A longer submission containing the reference loses credit for the extra
    words (w(ref) / w(sub)); a shorter one inside it earns its share
    (w(sub) / w(ref)). A lone shared function word earns nothing.
    """
    def weight(indices):
        return sum(weights[index] for index in indices)

    best, kind = 0.0, None
    for sub in submitted:
        for ref in reference:
            overlap = sub & ref
            if not overlap:
                continue
            if not (len(overlap) >= 2 or ref <= sub or any(weights[index] == 1.0 for index in overlap)):
                continue
            credit = weight(overlap) / weight(sub | ref)
            if credit > best:
                best = credit
                kind = "exact" if sub == ref else "superset" if ref <= sub else "subset" if sub <= ref else "overlap"
    return best, kind


def _best_assignment(credits: dict[tuple[int, int], tuple[float, str]]) -> list[tuple[int, int]]:
    """Exact one-to-one assignment maximizing total credit (≤10 × 10, sparse).

    Submissions are taken in order; ties keep the earlier-found assignment.
    """
    positions = sorted({position for position, _ in credits})
    states = {frozenset(): (0.0, ())}
    for position in positions:
        following = dict(states)
        for used, (total, pairs) in states.items():
            for (candidate, order), (credit, _) in credits.items():
                if candidate != position or order in used:
                    continue
                key, value = used | {order}, (total + credit, pairs + ((position, order),))
                if key not in following or value[0] > following[key][0] + 1e-12:
                    following[key] = value
        states = following
    best = max(states.values(), key=lambda state: state[0], default=(0.0, ()))
    for state in states.values():  # deterministic tie-break: first-found maximum
        if abs(state[0] - best[0]) <= 1e-12:
            best = state
            break
    return list(best[1])


def evaluate(dataset: Dataset, predictions: list[dict], paper_ids: list[str], scope_name: str) -> dict:
    """Score a fixed scope; missing papers remain in the reference denominator."""
    _fatal_validation(dataset, predictions, paper_ids, scope_name)
    predictions_by_id = {prediction["paper_id"]: prediction for prediction in predictions}
    report = {
        "metrics_version": METRICS_VERSION,
        "quality_policy_version": QUALITY_POLICY_VERSION,
        "dataset_version": dataset.version,
        "scope": {"name": scope_name, "paper_ids": list(paper_ids), "paper_count": len(paper_ids)},
        "counts": {},
        "metrics": {},
        "papers": [],
    }
    formats = set()
    for paper_id in paper_ids:
        paper = dataset.papers[paper_id]
        gold = [expression["text"] for expression in dataset.labels[paper_id]]
        remaining = set(gold)
        # Matching ignores case: selecting the right words is what is scored.
        gold_by_key = {}
        for text in gold:
            gold_by_key.setdefault(text.casefold(), text)
        seen = set()
        items = []
        abstract = paper["abstract"]
        spans, weights = _tokens(abstract)
        gold_tokens = {text: _token_sets(abstract, spans, text) for text in gold}
        partial_positions = []
        for position, raw_question in enumerate(predictions_by_id.get(paper_id, {}).get("questions", [])):
            question, normalization_issues = normalize_question(raw_question, paper["abstract"], paper_id, position)
            formats.add(question["input_format"])
            text = question["expression_text"]
            duplicate = text in seen
            seen.add(text)
            item = _validate_item(question, paper["abstract"], duplicate=duplicate, initial_issues=normalization_issues)
            # A bad span or option does not undo a correct expression selection.
            reference = gold_by_key.get(text.casefold())
            exact = bool(not duplicate and reference in remaining and gold_tokens[reference]
                         and _token_sets(abstract, spans, text))
            item.update(matched=exact, match_credit=1.0 if exact else 0.0,
                        match_type="exact" if exact else None, matched_expression=reference if exact else None)
            if exact:
                remaining.remove(reference)
            elif not duplicate:
                partial_positions.append(position)
            items.append(item)
        # Partial credit for the rest: each reference and submission is used once.
        credits = {}
        for position in partial_positions:
            submitted = _token_sets(abstract, spans, items[position]["expression_text"])
            for order, text in enumerate(gold):
                if text in remaining:
                    credit, kind = match_credit(submitted, gold_tokens[text], weights)
                    if credit > 0:
                        credits[(position, order)] = (credit, kind)
        for position, order in _best_assignment(credits):
            credit, kind = credits[(position, order)]
            items[position].update(matched=True, match_credit=round(credit, 6), match_type=kind, matched_expression=gold[order])
            remaining.discard(gold[order])
        report["papers"].append({
            "paper_id": paper_id,
            "paper_name": paper.get("paper_name", paper_id),
            "domain": domain_code(paper),
            "abstract": paper["abstract"],
            "submitted": paper_id in predictions_by_id,
            "gold_expressions": gold,
            "missing_expressions": [text for text in gold if text in remaining],
            "items": items,
        })
    report["match_policy"] = deepcopy(MATCH_POLICY)
    report["submission_formats"] = sorted(formats)
    if "compact-v1" in formats:
        report["inference_policy"] = deepcopy(INFERENCE_POLICY)
    return recompute(report)


def _complete(item: dict) -> bool:
    quality = item.get("quality") or {}
    value = quality.get("q")
    return (
        quality.get("status") == "complete"
        and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
    )


def _percentage(value: float | None) -> float | None:
    return None if value is None else round(100 * value, 6)


def recompute(report: dict) -> dict:
    """Recompute using the report's saved metric version, keeping matches fixed.

    V4 weights every credited question by its match credit (exact = 1, partial
    = token Jaccard): final = Σ credit × q / references = R_soft × Q_matched.
    V3 uses exact Recall × matched quality. Earlier or unversioned reports retain
    Precision × Recall × matched quality; recomputation never migrates them.
    """
    papers = report["papers"]
    items = [item for paper in papers for item in paper["items"]]
    code_only = report.get("quality_policy_version") == CODE_REVIEW_POLICY
    if code_only:
        for paper in papers:
            for item in paper["items"]:
                item["quality"] = code_quality(item, paper["abstract"])
    for item in items:
        # Structural failures cannot be overridden by human or model reviews.
        if not item.get("structure_valid") and not code_only:
            item["quality"] = empty_quality(structural=True)
    soft = report.get("metrics_version") == "soft-rq-v4"

    def credit(item: dict) -> float:
        return float(item.get("match_credit", 1.0 if item["matched"] else 0.0)) if soft else float(bool(item["matched"]))

    matched_items = [item for item in items if credit(item) > 0]
    gold = sum(len(paper["gold_expressions"]) for paper in papers)
    predicted, matched = len(items), len(matched_items)
    exact_matched = sum(credit(item) == 1.0 for item in matched_items)
    credited = sum(credit(item) for item in matched_items)
    submitted = sum(paper["submitted"] for paper in papers)
    complete = sum(_complete(item) for item in items)
    errors = sum(item["quality"].get("status") == "error" for item in items)
    needs_review = sum(item["quality"].get("status") == "needs_review" for item in items)
    temporary = sum(item["quality"].get("source") == "temporary" for item in items)
    review_recommended = sum(item["quality"].get("review_recommended") is True for item in items)
    pending = predicted - complete - errors - needs_review
    all_done = complete == predicted
    matched_done = all(_complete(item) for item in matched_items)

    precision = credited / predicted if predicted else 0.0
    recall = credited / gold if gold else None
    f1 = 2 * credited / (predicted + gold) if predicted + gold else None
    quality_all = sum(item["quality"]["q"] for item in items) / predicted if all_done and predicted else None
    quality_matched = (
        sum(credit(item) * item["quality"]["q"] for item in matched_items) / credited
        if matched_done and matched else None
    )
    final = None
    if all_done and gold:
        final = recall * quality_matched if matched else 0.0
        if report.get("metrics_version") not in ("exact-rq-v3", "soft-rq-v4"):
            final *= precision

    def unique_answer(item: dict) -> bool:
        quality = item["quality"]
        gates = quality.get("gates") or {}
        return bool(
            item["structure_valid"] and _complete(item)
            and quality.get("source") != "temporary"
            and quality.get("review_recommended") is not True
            and not any(assessment.get("verdict") == "uncertain" for assessment in quality.get("option_assessments", []))
            and quality.get("accepted_option_ids") == [item.get("answer_id")]
            and all(gates.get(name) is True for name in ("context_sufficient", "no_specialist_knowledge", "no_answer_leak"))
        )

    empty_gold = [paper for paper in papers if not paper["gold_expressions"]]
    abstentions = sum(paper["submitted"] and not paper["items"] for paper in empty_gold)
    report["counts"] = {
        "gold": gold,
        "predicted": predicted,
        "matched": matched,
        "exact_matched": exact_matched,
        "partial_matched": matched - exact_matched,
        "credited_matches": round(credited, 6),
        "submitted_papers": submitted,
        "missing_papers": len(papers) - submitted,
        "quality_complete": complete,
        "quality_pending": pending,
        "quality_error": errors,
        "needs_review": needs_review,
        "quality_temporary": temporary,
        "review_recommended": review_recommended,
    }
    report["score_mode"] = "code" if code_only else "temporary" if temporary else "reviewed"
    if code_only:
        report["counts"].update(code_passed=sum(item["quality"]["result"] == "pass" for item in items),
                                code_failed=sum(item["quality"]["result"] == "fail" for item in items))
    report["metrics"] = {
        "precision_pct": _percentage(precision),
        "recall_pct": _percentage(recall),
        "exact_recall_pct": _percentage(exact_matched / gold if gold else None),
        "f1_pct": _percentage(f1),
        "quality_all_pct": _percentage(quality_all),
        "quality_matched_pct": _percentage(quality_matched),
        "unique_answer_rate_pct": None if code_only else _percentage(sum(unique_answer(item) for item in items) / predicted if all_done and predicted else None),
        "structural_validity_pct": _percentage(sum(item["structure_valid"] for item in items) / predicted if predicted else None),
        "coverage_pct": _percentage(submitted / len(papers) if papers else None),
        "empty_gold_abstention_pct": _percentage(abstentions / len(empty_gold) if empty_gold else None),
        "final_score": _percentage(final),
    }
    if code_only:
        report["metrics"]["source_unique_answer_rate_pct"] = _percentage(
            sum(next(check["passed"] for check in item["quality"]["checks"] if check["key"] == "unique_source_answer")
                for item in items) / predicted if predicted else None)
    return report
