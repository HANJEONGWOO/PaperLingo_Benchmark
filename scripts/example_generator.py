#!/usr/bin/env python3
"""Generate prediction JSONL using only selected source abstracts (never labels).

Usage from the repository root:
    python scripts/example_generator.py --input selected_abstracts.jsonl --output predictions.jsonl

Set GENERATOR_BASE_URL, GENERATOR_MODEL and optionally
GENERATOR_API_KEY in the process environment. GENERATOR_TIMEOUT defaults to 300
seconds for local generation. Provider charges can apply;
this script calls the configured model only when explicitly executed. Default limit
is 5 papers. Pass --limit to intentionally change it. Existing output is preserved.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperlingo.judge import JudgeConfig, _json_format_unsupported, _response_identity, _usage, strict_json_loads  # noqa: E402


EXPRESSION_TYPES = {
    "academic_vocabulary", "collocation", "verb_pattern", "preposition_pattern",
    "discourse_marker", "rhetorical_frame", "grammar_pattern",
}


def source_contexts(paper: dict) -> list[dict]:
    """Provide exact source contexts with stable IDs; the model never rewrites them."""
    abstract = paper["abstract"]
    boundaries = list(re.finditer(r"(?<=[.!?])\s+(?=[A-Z])", abstract))
    starts = [0] + [match.end() for match in boundaries]
    ends = [match.start() for match in boundaries] + [len(abstract)]
    contexts = [
        {"id": f"c{index + 1}", "text": abstract[start:end], "start": start, "end": end}
        for index, (start, end) in enumerate(zip(starts, ends)) if abstract[start:end].strip()
    ]
    contexts.append({"id": "abstract", "text": abstract, "start": 0, "end": len(abstract)})
    return contexts


def locate(text: str, fragment: str, occurrence: int, field: str) -> tuple[int, int]:
    if not isinstance(fragment, str) or not fragment or type(occurrence) is not int or occurrence < 0:
        raise ValueError(f"Invalid {field} substring or occurrence index.")
    position = -1
    for _ in range(occurrence + 1):
        position = text.find(fragment, position + 1)
        if position < 0:
            raise ValueError(f"{field} is not an exact source substring at the requested occurrence.")
    return position, position + len(fragment)


def to_prediction(paper: dict, generated: dict) -> dict:
    """Resolve source offsets by code, without changing generated content."""
    if not isinstance(generated, dict) or set(generated) != {"paper_id", "questions"}:
        raise ValueError("Generator response must contain paper_id and questions only.")
    if generated["paper_id"] != paper["paper_id"]:
        raise ValueError("Generator returned the wrong paper_id.")
    questions = generated["questions"]
    if not isinstance(questions, list) or len(questions) > 5:
        raise ValueError("Generator must return 0–5 questions.")
    required = {
        "question_id", "expression_text", "expression_type", "context_id", "blank_text",
        "expression_occurrence", "blank_occurrence", "options",
        "answer_id", "learning_point",
    }
    converted = []
    question_ids = set()
    contexts = {context["id"]: context for context in source_contexts(paper)}
    for question in questions:
        if not isinstance(question, dict) or set(question) != required:
            raise ValueError("Generated question fields do not match the generator schema.")
        question_id = question["question_id"]
        if not isinstance(question_id, str) or not question_id.strip() or question_id in question_ids:
            raise ValueError("Question IDs must be nonempty and unique.")
        question_ids.add(question_id)
        if not isinstance(question["expression_type"], str) or question["expression_type"] not in EXPRESSION_TYPES:
            raise ValueError("Unknown expression_type.")
        if not isinstance(question["learning_point"], str) or not question["learning_point"].strip():
            raise ValueError("learning_point must be a nonempty string.")
        context_id = question["context_id"]
        if not isinstance(context_id, str) or context_id not in contexts:
            raise ValueError("context_id must refer to a supplied source context.")
        context = contexts[context_id]
        context_start, context_end = context["start"], context["end"]
        expression_start, expression_end = locate(
            context["text"], question["expression_text"], question["expression_occurrence"], "expression_text"
        )
        blank_start, blank_end = locate(
            question["expression_text"], question["blank_text"], question["blank_occurrence"], "blank_text"
        )
        options = question["options"]
        if not isinstance(options, list) or len(options) != 4:
            raise ValueError("Exactly four options are required.")
        by_id = {}
        normalized_options = set()
        for option in options:
            if not isinstance(option, dict) or set(option) != {"id", "text"}:
                raise ValueError("Option fields must be id and text.")
            if not isinstance(option["id"], str) or option["id"] not in {"A", "B", "C", "D"} or option["id"] in by_id:
                raise ValueError("Option IDs must be A/B/C/D exactly once.")
            if not isinstance(option["text"], str) or not option["text"].strip():
                raise ValueError("Option text must be a nonempty string.")
            normalized = " ".join(unicodedata.normalize("NFKC", option["text"]).casefold().split())
            if normalized in normalized_options:
                raise ValueError("Four option texts must be distinct, including after case/spacing/Unicode normalization.")
            normalized_options.add(normalized)
            by_id[option["id"]] = option["text"]
        answer_id = question["answer_id"]
        if not isinstance(answer_id, str) or answer_id not in by_id or by_id[answer_id] != question["blank_text"]:
            raise ValueError("Correct option must exactly restore the original blank.")
        absolute_expression = context_start + expression_start
        converted.append({
            "question_id": question_id,
            "expression_text": question["expression_text"],
            "expression_type": question["expression_type"],
            "expression_span": {"start": absolute_expression, "end": context_start + expression_end},
            "context_span": {"start": context_start, "end": context_end},
            "blank_span": {"start": absolute_expression + blank_start, "end": absolute_expression + blank_end},
            "options": options,
            "answer_id": answer_id,
            "learning_point": question["learning_point"],
        })
    return {"paper_id": paper["paper_id"], "questions": converted}


async def run(args: argparse.Namespace) -> None:
    base_url = os.environ.get("GENERATOR_BASE_URL", "")
    model = os.environ.get("GENERATOR_MODEL", "")
    if not base_url or not model:
        raise ValueError("Set GENERATOR_BASE_URL and GENERATOR_MODEL explicitly.")
    config = JudgeConfig(base_url, os.environ.get("GENERATOR_API_KEY", ""), model, timeout=float(os.environ.get("GENERATOR_TIMEOUT", "300")))
    prompt = (ROOT / "prompts" / "generator_v1.txt").read_text(encoding="utf-8")
    if args.output.exists() or Path(str(args.output) + ".meta.json").exists():
        raise ValueError("Output or metadata file already exists; choose a new output path.")
    papers = []
    paper_ids = set()
    with args.input.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            raw = strict_json_loads(line)
            if not isinstance(raw, dict) or not isinstance(raw.get("paper_id"), str) or not isinstance(raw.get("abstract"), str):
                raise ValueError(f"Input line {line_number} requires string paper_id and abstract.")
            if raw["paper_id"] in paper_ids:
                raise ValueError(f"Duplicate paper_id at input line {line_number}.")
            paper_ids.add(raw["paper_id"])
            # A strict whitelist prevents accidental reference-label leakage even
            # if the caller supplies a combined data file.
            papers.append({key: raw[key] for key in ("paper_id", "paper_name", "domain", "abstract") if key in raw})
            if len(papers) >= args.limit:
                break
    if not papers:
        raise ValueError("Input contains no source papers.")
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = "Bearer " + config.api_key
    predictions = []
    metadata = {
        "kind": "example_generator", "model": config.model,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "paper_count": len(papers), "papers": [],
    }
    async with httpx.AsyncClient(timeout=config.timeout, follow_redirects=False) as client:
        for paper in papers:
            start = time.monotonic()
            model_input = {
                **paper,
                "contexts": [{"id": context["id"], "text": context["text"]} for context in source_contexts(paper)],
            }
            body = {
                "model": config.model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(model_input, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
            }
            response = await client.post(config.endpoint, headers=headers, json=body)
            attempts = 1
            if _json_format_unsupported(response):
                body.pop("response_format")
                response = await client.post(config.endpoint, headers=headers, json=body)
                attempts += 1
            if not response.is_success:
                raise ValueError(f"Generator endpoint rejected the request (HTTP {response.status_code}).")
            result = response.json()
            try:
                if not isinstance(result, dict):
                    raise ValueError("Completion must be an object.")
                choices = result["choices"]
                if len(choices) != 1 or choices[0].get("finish_reason") not in {None, "stop"}:
                    raise ValueError("Incomplete completion.")
                content = choices[0]["message"]["content"]
                if not isinstance(content, str) or len(content) > 100_000:
                    raise ValueError("Invalid completion content.")
                generated = strict_json_loads(content)
            except (KeyError, TypeError, ValueError, IndexError, AttributeError, RecursionError):
                raise ValueError("Generator returned an invalid or incomplete JSON response.") from None
            predictions.append(to_prediction(paper, generated))
            metadata["papers"].append({
                "paper_id": paper["paper_id"], "attempts": attempts,
                "latency_ms": round((time.monotonic() - start) * 1000), "usage": _usage(result),
                **_response_identity(result),
            })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting any existing results.
    with args.output.open("x", encoding="utf-8") as output:
        for prediction in predictions:
            output.write(json.dumps(prediction, ensure_ascii=False) + "\n")
    with Path(str(args.output) + ".meta.json").open("x", encoding="utf-8") as output:
        json.dump(metadata, output, ensure_ascii=False, indent=2)
    print(f"Wrote {len(predictions)} predictions to {args.output}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True, help="JSONL of selected source abstracts")
    parser.add_argument("--output", type=Path, required=True, help="New prediction JSONL output file")
    parser.add_argument("--limit", type=int, default=5, help="Maximum source papers to generate (default: 5)")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    try:
        asyncio.run(run(args))
    except httpx.HTTPError:
        print("Generation failed: endpoint connection error. Check server configuration.", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        # Messages are local validation messages only, never provider bodies or keys.
        print(f"Generation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
