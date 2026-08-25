import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional

import boto3
from dotenv import load_dotenv

from audit_engine import evaluate_single_case
from aws_utils import preflight_bedrock_or_raise, resolve_model_id
from case_matching import build_dataset_index, build_response_index, normalize_case_key, resolve_dataset_case, resolve_source_entry
from report_renderer import render_html_report
from source_json import load_json
from source_loader import load_output_items


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def resolve_threshold(cli_value: Optional[float]) -> float:
    if cli_value is not None:
        return float(cli_value)

    while True:
        raw = input("Enter pass threshold (0-10): ").strip()
        try:
            value = float(raw)
        except ValueError:
            print("Invalid number. Please enter a value between 0 and 10.")
            continue
        if 0 <= value <= 10:
            return value
        print("Pass threshold must be between 0 and 10.")


def build_summary(evaluated_results: List[Dict[str, Any]], pass_threshold: float) -> Dict[str, Any]:
    scores = [int(item.get("audit_score", item.get("updated_score", item["score"]))) for item in evaluated_results]
    passed_count = sum(1 for item in evaluated_results if item["passed"])
    total = len(evaluated_results)

    return {
        "total_cases": total,
        "average_score": round(mean(scores), 2) if scores else 0.0,
        "pass_threshold": pass_threshold,
        "passed_cases": passed_count,
        "failed_cases": total - passed_count,
        "pass_rate_percent": round((passed_count / total) * 100.0, 2) if evaluated_results else 0.0,
    }


def evaluate_cases(
    dataset: List[Dict[str, Any]],
    output_items: List[Dict[str, Any]],
    bedrock_client: Optional[Any],
    model_id: str,
    pass_threshold: float,
    skip_llm: bool,
) -> Dict[str, Any]:
    output_index = build_response_index(output_items)
    dataset_index = build_dataset_index(dataset)
    evaluated_results: List[Dict[str, Any]] = []

    for i, source_entry in enumerate(output_items, start=1):
        source_case = source_entry.get("test_case") if isinstance(source_entry.get("test_case"), dict) else {}
        case = resolve_dataset_case(dataset_index, dataset, source_case)
        if case is None:
            scenario = str(source_case.get("scenario", "<unknown scenario>"))
            raise ValueError(
                "Could not find matching dataset case for source evaluation: "
                f"{scenario}. Ensure --responses file corresponds to the same dataset."
            )

        matched_source = resolve_source_entry(output_index, output_items, case, normalize_case_key(case))
        if matched_source is None:
            scenario = str(case.get("scenario", "<unknown scenario>"))
            raise ValueError(
                "Could not find matching source evaluation for dataset case: "
                f"{scenario}. Ensure --responses file corresponds to the same dataset."
            )

        evaluated_results.append(
            evaluate_single_case(
                case_index=i,
                case=case,
                source_entry=matched_source,
                bedrock_client=bedrock_client,
                model_id=model_id,
                pass_threshold=pass_threshold,
                skip_llm=skip_llm,
            )
        )

    summary = build_summary(evaluated_results, pass_threshold)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_id": model_id,
        "summary": summary,
        "results": evaluated_results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prompt evaluation pipeline using AWS Bedrock.")
    parser.add_argument("--dataset", default="dataset/dataset.json", help="Path to dataset JSON.")
    parser.add_argument(
        "--responses",
        default="dataset/output.html",
        help="Path to existing evaluations source (.json or .html).",
    )
    parser.add_argument("--output-json", default="results/output_llm.json", help="Path to write evaluated JSON report.")
    parser.add_argument("--output-html", default="results/output_llm.html", help="Path to write HTML report.")
    parser.add_argument("--pass-threshold", type=float, default=None, help="Pass threshold score in range 0-10.")
    parser.add_argument("--model-id", default=None, help="AWS Bedrock model ID.")
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip Bedrock auditing and keep existing score/reasoning from source.",
    )
    parser.add_argument("--aws-region", default=None, help="AWS region override (falls back to AWS_DEFAULT_REGION).")
    parser.add_argument("--preflight-only", action="store_true", help="Run AWS auth preflight and exit.")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    dataset_path = Path(args.dataset)
    responses_path = Path(args.responses)
    output_json_path = Path(args.output_json)
    output_html_path = Path(args.output_html)

    if not dataset_path.exists():
        raise FileNotFoundError(f"Dataset not found: {dataset_path}")
    if not responses_path.exists():
        raise FileNotFoundError(f"Responses not found: {responses_path}")

    pass_threshold = resolve_threshold(args.pass_threshold)
    if not (0 <= pass_threshold <= 10):
        raise ValueError("Pass threshold must be in range 0-10.")

    model_id = "llm-skipped"
    if not args.skip_llm:
        model_id = resolve_model_id(args.model_id)

    dataset = load_json(dataset_path)
    if not isinstance(dataset, list):
        raise ValueError("Dataset JSON must be a list of test cases.")

    output_items = load_output_items(responses_path)

    region_name = args.aws_region or os.getenv("AWS_DEFAULT_REGION")

    if not args.skip_llm:
        if preflight_bedrock_or_raise(region_name=region_name, preflight_only=args.preflight_only):
            return

    bedrock_client: Optional[Any] = None
    if not args.skip_llm:
        bedrock_client = boto3.client("bedrock-runtime", region_name=region_name)

    evaluated = evaluate_cases(
        dataset=dataset,
        output_items=output_items,
        bedrock_client=bedrock_client,
        model_id=model_id,
        pass_threshold=pass_threshold,
        skip_llm=args.skip_llm,
    )

    save_json(output_json_path, evaluated)
    output_html_path.write_text(render_html_report(evaluated), encoding="utf-8")

    summary = evaluated["summary"]
    print("Evaluation complete.")
    print(f"Cases: {summary['total_cases']}")
    print(f"Average score: {summary['average_score']}")
    print(f"Pass rate: {summary['pass_rate_percent']}%")
    print(f"Output JSON: {output_json_path}")
    print(f"Output HTML: {output_html_path}")


if __name__ == "__main__":
    main()
