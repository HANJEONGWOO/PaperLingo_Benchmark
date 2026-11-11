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
import sys
import time
import unicodedata

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperlingo.judge import JudgeConfig, _json_format_unsupported, _response_identity, _usage, strict_json_loads  # noqa: E402
from paperlingo.dataset import occurrences  # noqa: E402


def to_prediction(paper: dict, generated: dict) -> dict:
    """Validate compact prediction fields without repairing generated content.

    The scoring server derives the first matching source span and sentence.
    This client only confirms that both the expression and correct option occur
    verbatim at whole-word boundaries; it never reads reference labels.
    """
    if not isinstance(generated, dict) or set(generated) != {"paper_id", "questions"}:
        raise ValueError("Generator response must contain paper_id and questions only.")
    if generated["paper_id"] != paper["paper_id"]:
        raise ValueError("Generator returned the wrong paper_id.")
    questions = generated["questions"]
    if not isinstance(questions, list) or len(questions) > 10:
        raise ValueError("Generator must return 0–10 questions.")
    required = {"expression_text", "options", "answer_id"}
    converted = []
    for question in questions:
        if not isinstance(question, dict) or set(question) != required:
            raise ValueError("Generated question fields do not match the generator schema.")
        expression = question["expression_text"]
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 1000:
            raise ValueError("expression_text must be a nonempty string of at most 1000 characters.")
        if not occurrences(paper["abstract"], expression, whole_words=True):
            raise ValueError("expression_text must occur exactly in the abstract at whole-word boundaries.")
        options = question["options"]
        if not isinstance(options, list) or len(options) != 4:
            raise ValueError("Exactly four options are required.")
        normalized_options = set()
        for option in options:
            if not isinstance(option, str) or not option.strip() or len(option) > 1000:
                raise ValueError("Each option must be a nonempty string of at most 1000 characters.")
            normalized = " ".join(unicodedata.normalize("NFKC", option).casefold().split())
            if normalized in normalized_options:
                raise ValueError("Four option texts must be distinct, including after case/spacing/Unicode normalization.")
            normalized_options.add(normalized)
        answer_id = question["answer_id"]
        if type(answer_id) is not int or not 0 <= answer_id <= 3:
            raise ValueError("answer_id must be an integer index from 0 to 3, not a boolean.")
        if not occurrences(expression, options[answer_id], whole_words=True):
            raise ValueError("Correct option must occur exactly within expression_text at whole-word boundaries.")
        converted.append({
            "expression_text": expression,
            "options": list(options),
            "answer_id": answer_id,
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
            body = {
                "model": config.model,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(paper, ensure_ascii=False)},
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
