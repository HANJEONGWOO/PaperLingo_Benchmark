"""Immutable reference-data loader and deterministic evaluation scopes."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .sentences import SENTENCE_POLICY_VERSION, sentence_count


class DatasetError(ValueError):
    """A reference dataset fails the invariants required for scoring."""


def domain_code(paper: dict) -> str:
    value = paper.get("domain", "unknown")
    return str(value.get("code", "unknown")) if isinstance(value, dict) else str(value)


def word_boundary(abstract: str, start: int, end: int) -> bool:
    """Reject matches that extract only part of an alphanumeric word."""
    def word(char: str) -> bool:
        return char.isalnum() or char == "_"

    if not (0 <= start < end <= len(abstract)):
        return False
    return not (
        (start > 0 and word(abstract[start]) and word(abstract[start - 1]))
        or (end < len(abstract) and word(abstract[end - 1]) and word(abstract[end]))
    )


def occurrences(abstract: str, text: str, *, whole_words: bool = False) -> list[int]:
    if not text:
        return []
    positions, offset = [], 0
    while (position := abstract.find(text, offset)) >= 0:
        if not whole_words or word_boundary(abstract, position, position + len(text)):
            positions.append(position)
        offset = position + 1
    return positions


class Dataset:
    """Load original JSONL files without modifying them or repairing labels."""

    SPLIT_VERSION = "evaluation-min5sentences-v4"
    MIN_EVALUATION_SENTENCES = 5
    EXAMPLE_PAPER_IDS = ("0911.3369", "1109.3182")
    EXAMPLE_ANSWER_VERSION = "authored-demo-v1"
    EVALUATION_LABELS_FILE = "paperlingo-evaluation-labels-1044-grammar.jsonl"
    MAX_REFERENCE_EXPRESSIONS = 10

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        digest = hashlib.sha256()
        rows = {}
        filenames = ["abstract_data.jsonl", "abstract_labels_v2.jsonl"]
        if (self.data_dir / self.EVALUATION_LABELS_FILE).is_file():
            filenames.append(self.EVALUATION_LABELS_FILE)
        for filename in filenames:
            path = self.data_dir / filename
            content = path.read_bytes()
            digest.update(filename.encode() + b"\0" + content + b"\0")
            records = {}
            for line_number, line in enumerate(content.decode("utf-8-sig").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, TypeError) as exc:
                    raise DatasetError(f"{filename}:{line_number}: invalid JSON") from exc
                if not isinstance(row, dict):
                    raise DatasetError(f"{filename}:{line_number}: expected an object")
                paper_id = row.get("paper_id")
                if not isinstance(paper_id, str) or not paper_id:
                    raise DatasetError(f"{filename}:{line_number}: invalid paper_id")
                if paper_id in records:
                    raise DatasetError(f"{filename}: duplicate paper_id {paper_id}")
                records[paper_id] = row
            rows[filename] = records
        self.papers = rows["abstract_data.jsonl"]
        label_rows = rows["abstract_labels_v2.jsonl"]
        if not self.papers:
            raise DatasetError("Reference dataset is empty")
        if set(self.papers) != set(label_rows):
            missing = sorted(set(self.papers) - set(label_rows))[:5]
            extra = sorted(set(label_rows) - set(self.papers))[:5]
            raise DatasetError(f"Paper and label IDs differ: missing={missing}, extra={extra}")
        # The evaluation answer key replaces the original labels of evaluation
        # papers only; example labels still come from abstract_labels_v2.jsonl.
        evaluation_rows = rows.get(self.EVALUATION_LABELS_FILE)
        # Example selection uses the original labels so the answer key cannot move the split.
        has_source_labels = {pid: bool(row.get("expressions")) for pid, row in label_rows.items()}
        if evaluation_rows is not None:
            extra = sorted(set(evaluation_rows) - set(self.papers))[:5]
            if extra:
                raise DatasetError(f"{self.EVALUATION_LABELS_FILE}: unknown paper IDs {extra}")
            label_rows = {**label_rows, **evaluation_rows}
        self.label_sources = {
            "default": "abstract_labels_v2.jsonl",
            "evaluation": self.EVALUATION_LABELS_FILE if evaluation_rows is not None else "abstract_labels_v2.jsonl",
        }

        self.labels: dict[str, list[dict]] = {}
        repeated = []
        substring_repeat_count = 0
        partial_occurrence_count = 0
        boundary_flags = []
        canonical_variants = defaultdict(set)
        types, label_counts, abstract_groups = Counter(), Counter(), defaultdict(list)
        for paper_id, paper in self.papers.items():
            abstract = paper.get("abstract")
            if not isinstance(abstract, str) or not abstract:
                raise DatasetError(f"{paper_id}: abstract must be nonempty text")
            expressions = label_rows[paper_id].get("expressions")
            if not isinstance(expressions, list) or len(expressions) > self.MAX_REFERENCE_EXPRESSIONS:
                raise DatasetError(f"{paper_id}: expressions must contain 0–{self.MAX_REFERENCE_EXPRESSIONS} items")
            seen = set()
            for expression in expressions:
                if not isinstance(expression, dict):
                    raise DatasetError(f"{paper_id}: expression must be an object")
                text = expression.get("text")
                if not isinstance(text, str) or not text.strip() or text not in abstract:
                    raise DatasetError(f"{paper_id}: reference expression is absent from abstract")
                if text in seen:
                    raise DatasetError(f"{paper_id}: duplicate reference expression {text!r}")
                seen.add(text)
                substring_positions = occurrences(abstract, text)
                positions = occurrences(abstract, text, whole_words=True)
                substring_repeat_count += len(substring_positions) > 1
                partial_occurrence_count += len(substring_positions) - len(positions)
                if len(positions) > 1:
                    repeated.append({"paper_id": paper_id, "text": text, "occurrences": len(positions)})
                if not positions:
                    boundary_flags.append({"paper_id": paper_id, "text": text})
                canonical = expression.get("canonical_form")
                if isinstance(canonical, str):
                    canonical_variants[text].add(canonical)
                types[str(expression.get("expression_type", "unspecified"))] += 1
            self.labels[paper_id] = expressions
            label_counts[len(expressions)] += 1
            abstract_groups[" ".join(abstract.casefold().split())].append(paper_id)

        prompt_path = self.data_dir / "labeling_prompt_v2.md"
        if prompt_path.is_file():
            digest.update(prompt_path.name.encode() + b"\0" + prompt_path.read_bytes())
        self.version = digest.hexdigest()
        canonical_conflicts = [
            {"text": text, "canonical_forms": sorted(values)}
            for text, values in sorted(canonical_variants.items()) if len(values) > 1
        ]
        duplicates = [ids for ids in abstract_groups.values() if len(ids) > 1]
        warnings = []
        if canonical_conflicts:
            warnings.append("동일한 원문 표현의 canonical_form이 다른 사례가 있어 정확 문자열만 주 채점에 사용합니다.")
        if boundary_flags:
            warnings.append("단어 일부에만 해당하는 참조 표현이 있습니다. 별도 검토가 필요합니다.")
        if duplicates:
            warnings.append("정규화된 초록의 중복이 있습니다. 독립적인 최종 평가 전에 분할을 검토하세요.")
        missing_review_artifacts = [
            name for name in ("labeling_v2_run/review_flags.csv", "labeling_v2_run/expression_spans.jsonl")
            if not (self.data_dir / name).exists()
        ]
        self.audit = {
            "id_integrity": True,
            "source_text_integrity": True,
            "label_count_distribution": dict(sorted(label_counts.items())),
            "expression_type_counts": dict(sorted(types.items())),
            "repeated_expression_count": len(repeated),
            "repeated_expression_examples": repeated[:10],
            "substring_repeat_expression_count": substring_repeat_count,
            "partial_word_occurrence_count": partial_occurrence_count,
            "canonical_variant_count": len(canonical_conflicts),
            "canonical_variant_method": "Group by exact expression text; compare exact canonical_form strings.",
            "canonical_variant_examples": canonical_conflicts[:10],
            "word_boundary_flag_count": len(boundary_flags),
            "word_boundary_flags": boundary_flags[:10],
            "duplicate_abstract_groups": duplicates,
            "missing_review_artifacts": missing_review_artifacts,
            "warnings": warnings,
        }

        # Two public examples, both with reference expressions. Tiny custom
        # datasets fall back to labelled papers, then to any paper.
        preferred = [paper_id for paper_id in self.EXAMPLE_PAPER_IDS if paper_id in self.papers and has_source_labels[paper_id]]
        for paper_id in sorted(self.papers, key=lambda pid: (not has_source_labels[pid], pid)):
            if len(preferred) >= len(self.EXAMPLE_PAPER_IDS):
                break
            if paper_id not in preferred:
                preferred.append(paper_id)
        demo_ids = set(preferred)
        self.sentence_counts = {pid: sentence_count(paper["abstract"]) for pid, paper in self.papers.items()}
        source_ids = sorted(set(self.papers) - demo_ids)
        eligible_ids = [pid for pid in source_ids if self.sentence_counts[pid] >= self.MIN_EVALUATION_SENTENCES]
        self.evaluation_filter = {
            "minimum_sentences": self.MIN_EVALUATION_SENTENCES,
            "sentence_policy_version": SENTENCE_POLICY_VERSION,
            "source_paper_count": len(source_ids),
            "eligible_paper_count": len(eligible_ids),
            "excluded_paper_count": len(source_ids) - len(eligible_ids),
            "sampling": "none; retain every eligible paper",
        }
        # Preserve the public examples. All qualifying papers in the previous
        # evaluation pool remain, with no sample-size or domain restriction.
        self.splits: dict[str, list[str]] = {
            "demo": preferred,
            "evaluation": eligible_ids,
        }
        if evaluation_rows is not None and set(evaluation_rows) != set(eligible_ids):
            missing = sorted(set(eligible_ids) - set(evaluation_rows))[:5]
            extra = sorted(set(evaluation_rows) - set(eligible_ids))[:5]
            raise DatasetError(
                f"{self.EVALUATION_LABELS_FILE} must cover exactly the evaluation split: "
                f"missing={missing}, extra={extra}"
            )

    def example_answer_metadata(self) -> dict:
        return {
            "version": self.EXAMPLE_ANSWER_VERSION,
            "authorship": "manually authored format examples covering every example reference",
            "scope": "demo",
            "submission_format": "compact-v1",
            "quality_status": "code-checked",
            "quality_policy_version": "code-basic-v1",
            "semantic_reviewed": False,
        }

    def summary(self) -> dict:
        return {
            "paper_count": len(self.papers),
            "expression_count": sum(map(len, self.labels.values())),
            "domain_count": len({domain_code(paper) for paper in self.papers.values()}),
            "zero_gold_count": sum(not expressions for expressions in self.labels.values()),
            "version": self.version,
            "split_version": self.SPLIT_VERSION,
            "evaluation_filter": deepcopy(self.evaluation_filter),
            "label_sources": dict(self.label_sources),
            "splits": {key: len(ids) for key, ids in self.splits.items()},
            "split_stats": {
                scope: {
                    "paper_count": len(ids),
                    "expression_count": sum(len(self.labels[paper_id]) for paper_id in ids),
                    "domain_count": len({domain_code(self.papers[paper_id]) for paper_id in ids}),
                }
                for scope, ids in self.splits.items()
            },
            "audit": deepcopy(self.audit),
        }

    def search(self, query: str = "", domain: str = "", offset: int = 0, limit: int = 20) -> dict:
        query = query.casefold().strip()
        records = [
            {"paper_id": paper_id, "paper_name": paper.get("paper_name", paper_id),
             "domain": domain_code(paper), "gold_count": len(self.labels[paper_id])}
            for paper_id, paper in self.papers.items()
            if (not domain or domain_code(paper) == domain)
            and (not query or query in " ".join((paper_id, str(paper.get("paper_name", "")), domain_code(paper))).casefold())
        ]
        offset, limit = max(0, offset), min(100, max(1, limit))
        return {"items": records[offset:offset + limit], "total": len(records)}

    def demo_predictions(self, *, compact: bool = False) -> list[dict]:
        """Authored examples covering all demo references, not model output.

        Exact-expression coverage and structural validity are intentional. The
        quality of the answer choices still requires a separate assessment.
        """
        predictions = []
        for index, paper_id in enumerate(self.splits["demo"]):
            labels = self.labels[paper_id]
            questions = []
            if labels:
                preferred = "identical to" if index == 0 else "consistent with"
                expression = next((item for item in labels if item["text"] == preferred), labels[0])
                ordered = [expression, *(item for item in labels if item["text"] != expression["text"])]
                questions = [
                    self._example_question(paper_id, item, "valid" if position == 0 else f"expression-{position + 1}")
                    for position, item in enumerate(ordered)
                ]
            predictions.append({"paper_id": paper_id, "questions": questions})
        if compact:
            return [{"paper_id": paper["paper_id"], "questions": [
                {
                    "expression_text": question["expression_text"],
                    "options": [option["text"] for option in question["options"]],
                    "answer_id": next(index for index, option in enumerate(question["options"]) if option["id"] == question["answer_id"]),
                }
                for question in paper["questions"]
            ]} for paper in predictions]
        return predictions

    def _example_question(self, paper_id: str, expression: dict, suffix: str) -> dict:
        abstract, text = self.papers[paper_id]["abstract"], expression["text"]
        start = abstract.index(text)
        end = start + len(text)
        token_match = list(re.finditer(r"\S+", text))[-1]
        blank_start, blank_end = start + token_match.start(), start + token_match.end()
        answer = abstract[blank_start:blank_end]
        # Sentence boundaries are only used to author the example, never to repair submissions.
        context_start = abstract.rfind(". ", 0, start) + 2
        if context_start == 1:
            context_start = 0
        next_period = abstract.find(". ", end)
        context_end = len(abstract) if next_period < 0 else next_period + 1
        targeted_distractors = {
            "that": ["what", "which", "whom"],
            "than": ["then", "that", "when"],
            "exhibit": ["exhibiting", "exhibited", "exhibits"],
        }
        distractors = targeted_distractors.get(answer)
        if distractors is None:
            distractors = [word for word in ("with", "of", "for", "to", "at", "from") if word != answer][:3]
        return {
            "question_id": f"demo-{paper_id}-{suffix}",
            "expression_text": text,
            "expression_type": expression.get("expression_type", ""),
            "expression_span": {"start": start, "end": end},
            "context_span": {"start": context_start, "end": context_end},
            "blank_span": {"start": blank_start, "end": blank_end},
            "options": [{"id": key, "text": value} for key, value in zip("ABCD", [answer, *distractors])],
            "answer_id": "A",
            "learning_point": "직접 작성한 형식 예시입니다. 보기 품질은 별도의 검토가 필요합니다.",
        }
