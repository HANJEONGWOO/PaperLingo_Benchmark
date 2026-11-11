"""Deterministic exact-expression scoring and question integrity checks.

Quality judgements are separate from expression matching. This module never
assigns an LLM quality score or silently removes a failed generated question.
"""

from __future__ import annotations

from copy import deepcopy
import math

from .dataset import Dataset, domain_code, occurrences, word_boundary
from .code_review import CODE_REVIEW_POLICY, code_quality, normalize_option
from .submission import COMPACT_FIELDS, INFERENCE_POLICY, MAX_QUESTIONS_PER_PAPER, generated_question_id, normalize_question, question_format


METRICS_VERSION = "exact-rq-v3"
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


def evaluate(dataset: Dataset, predictions: list[dict], paper_ids: list[str], scope_name: str) -> dict:
    """Score a fixed scope; missing papers remain in the reference denominator."""
    _fatal_validation(dataset, predictions, paper_ids, scope_name)
    predictions_by_id = {prediction["paper_id"]: prediction for prediction in predictions}
    report = {
        "metrics_version": METRICS_VERSION,
        "max_questions_per_paper": MAX_QUESTIONS_PER_PAPER,
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
        seen = set()
        items = []
        for position, raw_question in enumerate(predictions_by_id.get(paper_id, {}).get("questions", [])):
            question, normalization_issues = normalize_question(raw_question, paper["abstract"], paper_id, position)
            formats.add(question["input_format"])
            text = question["expression_text"]
            duplicate = text in seen
            seen.add(text)
            item = _validate_item(question, paper["abstract"], duplicate=duplicate, initial_issues=normalization_issues)
            # A bad span or option does not undo a correct expression selection.
            item["matched"] = bool(not duplicate and text in remaining and occurrences(paper["abstract"], text, whole_words=True))
            if item["matched"]:
                remaining.remove(text)
            items.append(item)
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

    V3 uses Recall × matched quality. Earlier or unversioned reports retain
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
    matched_items = [item for item in items if item["matched"]]
    gold = sum(len(paper["gold_expressions"]) for paper in papers)
    predicted, matched = len(items), len(matched_items)
    submitted = sum(paper["submitted"] for paper in papers)
    complete = sum(_complete(item) for item in items)
    errors = sum(item["quality"].get("status") == "error" for item in items)
    needs_review = sum(item["quality"].get("status") == "needs_review" for item in items)
    temporary = sum(item["quality"].get("source") == "temporary" for item in items)
    review_recommended = sum(item["quality"].get("review_recommended") is True for item in items)
    pending = predicted - complete - errors - needs_review
    all_done = complete == predicted
    matched_done = all(_complete(item) for item in matched_items)

    precision = matched / predicted if predicted else 0.0
    recall = matched / gold if gold else None
    f1 = 2 * matched / (predicted + gold) if predicted + gold else None
    quality_all = sum(item["quality"]["q"] for item in items) / predicted if all_done and predicted else None
    quality_matched = sum(item["quality"]["q"] for item in matched_items) / matched if matched_done and matched else None
    final = None
    if all_done and gold:
        final = recall * quality_matched if matched else 0.0
        if report.get("metrics_version") != "exact-rq-v3":
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
