#!/usr/bin/env python3
"""Export a fixed evaluation scope for generators without disclosing labels."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paperlingo.dataset import Dataset
from paperlingo.config import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=["demo", "evaluation"], default="demo")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dataset = Dataset(ROOT / "data")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as stream:
        for paper_id in dataset.splits[args.scope]:
            paper = dataset.papers[paper_id]
            record = {key: paper[key] for key in ("paper_id", "paper_name", "domain", "abstract")}
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    manifest = {"scope": args.scope, "paper_ids": dataset.splits[args.scope], "dataset_version": dataset.version,
                "split_version": dataset.SPLIT_VERSION, "evaluation_filter": dataset.evaluation_filter,
                "sentence_counts": {pid: dataset.sentence_counts[pid] for pid in dataset.splits[args.scope]}}
    args.output.with_suffix(args.output.suffix + ".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Exported {len(manifest['paper_ids'])} abstracts (no labels) to {args.output}")


if __name__ == "__main__":
    main()
