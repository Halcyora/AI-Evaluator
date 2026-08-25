import argparse
import json
import os
import re
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


class ReportTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: List[List[str]] = []
        self._in_tr = False
        self._in_td = False
        self._current_row: List[str] = []
        self._current_cell_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        del attrs
        if tag == "tr":
            self._in_tr = True
            self._current_row = []
        elif tag == "td" and self._in_tr:
            self._in_td = True
            self._current_cell_parts = []
        elif tag == "br" and self._in_td:
            self._current_cell_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._in_td:
            self._in_td = False
            self._current_row.append("".join(self._current_cell_parts).strip())
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
            if self._current_row:
                self.rows.append(self._current_row)

    def handle_data(self, data: str) -> None:
        if self._in_td:
            self._current_cell_parts.append(data)


def parse_prompt_inputs_cell(cell_text: str) -> Dict[str, str]:
    inputs: Dict[str, str] = {}
    for line in cell_text.splitlines():
        part = line.strip()
        if not part or ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key:
            inputs[key] = value
    return inputs


def parse_criteria_cell(cell_text: str) -> List[str]:
    criteria: List[str] = []
    for part in cell_text.splitlines():
        text = part.strip()
        if not text:
            continue
        text = re.sub(r"^[•\-\s]+", "", text)
        criteria.append(text)
    return criteria


def parse_output_html_responses(path: Path) -> List[Dict[str, Any]]:
    parser = ReportTableParser()
    parser.feed(path.read_text(encoding="utf-8"))
    output_items: List[Dict[str, Any]] = []

    for cells in parser.rows:
        if len(cells) < 6:
            continue

        scenario = cells[0].strip()
        prompt_inputs = parse_prompt_inputs_cell(cells[1])
        solution_criteria = parse_criteria_cell(cells[2])
        output_text = cells[3].strip()
        score_text = cells[4].strip()
        reasoning = cells[5].strip()

        score_value: Optional[float] = None
        score_match = re.search(r"\d+(?:\.\d+)?", score_text)
        if score_match:
            score_value = float(score_match.group(0))

        output_items.append(
            {
                "test_case": {
                    "scenario": scenario,
                    "prompt_inputs": prompt_inputs,
                    "solution_criteria": solution_criteria,
                },
                "output": output_text,
                "score": score_value,
                "reasoning": reasoning,
            }
        )

    if not output_items:
        raise ValueError(f"No evaluable rows found in HTML responses file: {path}")

    return output_items


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def normalize_case_key(case: Dict[str, Any]) -> str:
    prompt_inputs = case.get("prompt_inputs", {})
    key_obj = {
        "task_description": str(case.get("task_description", "")).strip().lower(),
        "scenario": str(case.get("scenario", "")).strip().lower(),
        "prompt_inputs": {k: str(v).strip().lower() for k, v in sorted(prompt_inputs.items())},
    }
    return json.dumps(key_obj, sort_keys=True, ensure_ascii=False)


def normalize_case_key_relaxed(case: Dict[str, Any]) -> str:
    prompt_inputs = case.get("prompt_inputs", {})
    key_obj = {
        "scenario": str(case.get("scenario", "")).strip().lower(),
        "prompt_inputs": {k: str(v).strip().lower() for k, v in sorted(prompt_inputs.items())},
    }
    return json.dumps(key_obj, sort_keys=True, ensure_ascii=False)


def build_evaluator_prompt(
    case: Dict[str, Any],
    response_text: str,
    existing_score: Optional[float],
    existing_reasoning: Optional[str],
) -> str:
    schema_hint = {
        "updated_score_0_to_10": "integer",
        "reasoning_gaps": "string (concise: what is wrong/missed in existing reasoning)",
        "missed_points": ["string"],
        "corrected_reasoning": "string (concise corrected reasoning for final score)",
    }

    return (
        "You are auditing an existing evaluation, not generating a new meal plan.\n\n"
        "Task:\n"
        "1) Verify whether the EXISTING score and EXISTING reasoning are correct against the provided criteria and restrictions.\n"
        "2) Identify what is wrong, missing, or overstated in existing reasoning.\n"
        "3) Provide an updated score and corrected concise reasoning.\n\n"
        "Instructions:\n"
        "- Use only the test case and candidate response as evidence.\n"
        "- Be concise and factual.\n"
        "- If existing reasoning is fully valid, say so and keep score close.\n"
        "- Return integer score in range 0-10.\n\n"
        "Return STRICT JSON only:\n"
        f"{json.dumps(schema_hint, ensure_ascii=False)}\n\n"
        "TEST_CASE:\n"
        f"{json.dumps(case, ensure_ascii=False)}\n\n"
        "EXISTING_EVALUATION:\n"
        f"{json.dumps({'score': existing_score, 'reasoning': existing_reasoning}, ensure_ascii=False)}\n\n"
        "CANDIDATE_RESPONSE:\n"
        f"{response_text}\n"
    )


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None

    # Remove fenced code blocks if the model returns them despite instruction.
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Best effort extraction of outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def bedrock_judge(
    client: Any,
    model_id: str,
    case: Dict[str, Any],
    response_text: str,
    existing_score: Optional[float],
    existing_reasoning: Optional[str],
) -> Tuple[Optional[int], str, List[str], str, Optional[str]]:
    prompt = build_evaluator_prompt(case, response_text, existing_score, existing_reasoning)

    try:
        response = client.converse(
            modelId=model_id,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": prompt}],
                }
            ],
            inferenceConfig={"temperature": 0.0, "maxTokens": 1200},
        )
    except (ClientError, BotoCoreError) as exc:
        return None, "LLM evaluation unavailable due to AWS error.", [], "", str(exc)

    contents = response.get("output", {}).get("message", {}).get("content", [])
    text_parts = [part.get("text", "") for part in contents if isinstance(part, dict)]
    model_text = "\n".join(t for t in text_parts if t)

    parsed = extract_json_object(model_text)
    if not parsed:
        return None, "LLM response was not valid JSON.", [], "", None

    score = parsed.get("updated_score_0_to_10")
    score_int: Optional[int]
    if isinstance(score, (int, float)):
        score_int = int(round(float(score)))
        score_int = max(0, min(10, score_int))
    else:
        score_int = None

    reasoning_gaps = str(parsed.get("reasoning_gaps", "")).strip() or "No reasoning gaps reported."
    missed_points = parsed.get("missed_points", [])
    if not isinstance(missed_points, list):
        missed_points = []
    corrected_reasoning = str(parsed.get("corrected_reasoning", "")).strip() or reasoning_gaps

    return score_int, reasoning_gaps, [str(x) for x in missed_points], corrected_reasoning, None


def extract_source_metadata(source_entry: Optional[Dict[str, Any]]) -> Tuple[Optional[float], Optional[str]]:
    if not source_entry:
        return None, None

    source_score: Optional[float] = None
    source_reasoning: Optional[str] = None

    raw_source_score = source_entry.get("score")
    if isinstance(raw_source_score, (int, float)):
        source_score = float(raw_source_score)

    raw_source_reasoning = source_entry.get("reasoning")
    if isinstance(raw_source_reasoning, str) and raw_source_reasoning.strip():
        source_reasoning = raw_source_reasoning.strip()

    return source_score, source_reasoning


def score_class(score: int) -> str:
    if score >= 8:
        return "high"
    if score >= 6:
        return "medium"
    return "low"


def render_html_report(results_doc: Dict[str, Any]) -> str:
    summary = results_doc["summary"]
    rows = []

    for item in results_doc["results"]:
        prompt_inputs_html = "<br>".join(
            f"<strong>{escape(str(k))}:</strong> {escape(str(v))}"
            for k, v in item["test_case"].get("prompt_inputs", {}).items()
        )
        criteria_html = "<br>".join(f"- {escape(str(c))}" for c in item["test_case"].get("solution_criteria", []))

        source_score = item.get("source_score")
        source_score_text = "-" if source_score is None else str(source_score)
        source_reasoning = item.get("source_reasoning")
        source_reasoning_text = "-" if not source_reasoning else escape(str(source_reasoning))

        reasoning_gaps = item.get("reasoning_gaps") or "None"
        missed_points = item.get("reasoning_missed_points") or "None"
        reasoning_audit_html = f"{escape(str(reasoning_gaps))}<br><strong>Missed:</strong> {escape(str(missed_points))}"

        suggested_update = escape(str(item.get("suggested_update") or "None"))
        llm_error_text = item.get("llm_error")
        if llm_error_text:
            suggested_update = f"{suggested_update}<br><strong>LLM error:</strong> {escape(str(llm_error_text))}"

        rows.append(
            "\n".join(
                [
                    "<tr>",
                    f"<td>{escape(str(item['case_id']))}</td>",
                    f"<td>{escape(str(item['test_case'].get('scenario', '')))}</td>",
                    f"<td>{prompt_inputs_html}</td>",
                    f"<td>{criteria_html}</td>",
                    f"<td><pre>{escape(str(item.get('output', '')))}</pre></td>",
                    f"<td>{source_score_text}</td>",
                    f"<td>{source_reasoning_text}</td>",
                    f"<td><span class=\"score score-{score_class(int(item['score']))}\">{item['score']}</span></td>",
                    f"<td>{escape(str(item.get('reasoning', '')))}</td>",
                    f"<td>{reasoning_audit_html}</td>",
                    f"<td>{suggested_update}</td>",
                    "</tr>",
                ]
            )
        )

    rows_html = "\n".join(rows)

    generated_at = escape(str(results_doc.get("generated_at", "")))
    model_id = escape(str(results_doc.get("model_id", "")))

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>Prompt Evaluation Report</title>
  <style>
    :root {{
      --bg: #f8f5ef;
      --card: #fffdf8;
      --ink: #1f2933;
      --muted: #52606d;
      --good: #2f855a;
      --warn: #b7791f;
      --bad: #c53030;
      --accent: #0f766e;
      --line: #d9d3c7;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, 'Times New Roman', serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 15% 20%, #efe6d9 0, transparent 25%),
        radial-gradient(circle at 85% 5%, #e3efe7 0, transparent 20%),
        var(--bg);
      line-height: 1.4;
    }}
    .wrap {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 24px;
    }}
    .header {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 18px;
    }}
    h1 {{ margin: 0 0 10px; font-size: 30px; letter-spacing: 0.3px; }}
    .meta {{ color: var(--muted); font-size: 14px; }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .stat {{
      background: #ffffff;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 12px;
    }}
    .stat .k {{ color: var(--muted); font-size: 13px; }}
    .stat .v {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
    }}
    table {{ width: 100%; border-collapse: collapse; min-width: 1400px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px; text-align: left; vertical-align: top; }}
    th {{ background: #ece4d8; position: sticky; top: 0; z-index: 1; }}
    tr:nth-child(even) {{ background: #fcfbf8; }}
    pre {{ white-space: pre-wrap; margin: 0; font-size: 12px; font-family: Consolas, Monaco, 'Courier New', monospace; }}
    .score {{
      display: inline-block;
      min-width: 32px;
      text-align: center;
      padding: 4px 8px;
      border-radius: 999px;
      font-weight: 700;
      color: #fff;
    }}
    .score-high {{ background: var(--good); }}
    .score-medium {{ background: var(--warn); }}
    .score-low {{ background: var(--bad); }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"header\">
      <h1>Prompt Evaluation Report</h1>
      <div class=\"meta\">Generated at {generated_at} | Bedrock model: {model_id}</div>
      <div class=\"stats\">
        <div class=\"stat\"><div class=\"k\">Total Cases</div><div class=\"v\">{summary['total_cases']}</div></div>
        <div class=\"stat\"><div class=\"k\">Average Score</div><div class=\"v\">{summary['average_score']}</div></div>
        <div class=\"stat\"><div class=\"k\">Pass Threshold</div><div class=\"v\">{summary['pass_threshold']}</div></div>
        <div class=\"stat\"><div class=\"k\">Pass Rate</div><div class=\"v\">{summary['pass_rate_percent']}%</div></div>
        <div class=\"stat\"><div class=\"k\">Passed</div><div class=\"v\">{summary['passed_cases']}</div></div>
        <div class=\"stat\"><div class=\"k\">Failed</div><div class=\"v\">{summary['failed_cases']}</div></div>
      </div>
    </div>

    <div class=\"table-wrap\">
      <table>
        <thead>
          <tr>
            <th>Case ID</th>
            <th>Scenario</th>
            <th>Prompt Inputs</th>
            <th>Criteria</th>
            <th>Response</th>
                        <th>Existing Score</th>
                        <th>Existing Reasoning</th>
                        <th>Updated Score</th>
                        <th>Updated Reasoning</th>
                        <th>Reasoning Audit</th>
                        <th>Suggested Update</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


def select_output_text(output_entry: Dict[str, Any]) -> str:
    # Supports both raw model output and previously-scored output objects.
    value = output_entry.get("output", "")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


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


def resolve_model_id(cli_value: Optional[str]) -> str:
    if cli_value:
        return cli_value

    env_model = os.getenv("BEDROCK_MODEL_ID", "").strip()
    if env_model:
        return env_model

    raw = input("Enter AWS Bedrock model ID (for example, anthropic.claude-3-5-sonnet-20240620-v1:0): ").strip()
    if raw:
        return raw
    raise ValueError("Bedrock model ID is required.")


def validate_aws_auth_for_bedrock(region_name: Optional[str]) -> Tuple[bool, str]:
    try:
        session = boto3.Session(region_name=region_name)
        creds = session.get_credentials()
        if creds is None:
            return False, "No AWS credentials were found. Configure credentials via .env, environment variables, or AWS profile."

        frozen = creds.get_frozen_credentials()
        access_key = frozen.access_key or ""
        if not access_key:
            return False, "AWS access key is missing."

        if access_key.startswith("ASIA") and not (frozen.token or ""):
            return False, "Temporary AWS credentials detected (ASIA...) but AWS session token is missing. Set AWS_SESSION_TOKEN."

        sts_client = session.client("sts")
        identity = sts_client.get_caller_identity()
        arn = identity.get("Arn", "unknown")
        return True, f"AWS auth verified for principal: {arn}"
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "UnrecognizedClientException":
            return (
                False,
                "AWS credentials are invalid for this request (UnrecognizedClientException). "
                "Rotate/recreate credentials and ensure region is correct.",
            )
        if error_code == "ExpiredToken":
            return False, "AWS token is expired. Refresh credentials and set AWS_SESSION_TOKEN if using temporary credentials."
        if error_code == "InvalidClientTokenId":
            return False, "AWS access key appears invalid. Verify AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
        return False, f"AWS auth check failed with {error_code}: {exc}"
    except BotoCoreError as exc:
        return False, f"AWS SDK error during auth preflight: {exc}"


def build_response_index(output_items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for item in output_items:
        case = item.get("test_case")
        if isinstance(case, dict):
            index[normalize_case_key(case)] = item
    return index


def resolve_output_items(responses: Any) -> List[Dict[str, Any]]:
    if isinstance(responses, dict) and "results" in responses:
        output_items = responses["results"]
    elif isinstance(responses, list):
        output_items = responses
    else:
        raise ValueError("Responses JSON must be a list, or an object containing 'results'.")

    if not isinstance(output_items, list):
        raise ValueError("Response entries must be a list.")

    return output_items


def load_output_items(path: Path) -> List[Dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".html":
        return parse_output_html_responses(path)

    responses = load_json(path)
    return resolve_output_items(responses)


def resolve_source_entry(
    output_index: Dict[str, Dict[str, Any]],
    output_items: List[Dict[str, Any]],
    case: Dict[str, Any],
    case_key: str,
) -> Optional[Dict[str, Any]]:
    source_entry = output_index.get(case_key)
    if source_entry:
        return source_entry

    relaxed_key = normalize_case_key_relaxed(case)
    for item in output_items:
        test_case = item.get("test_case")
        if isinstance(test_case, dict) and normalize_case_key_relaxed(test_case) == relaxed_key:
            return item

    return None


def preflight_bedrock_or_raise(region_name: Optional[str], preflight_only: bool) -> bool:
    ok, preflight_message = validate_aws_auth_for_bedrock(region_name=region_name)
    print(preflight_message)
    if not ok:
        raise RuntimeError(
            "AWS preflight failed. Fix credentials first, then rerun. "
            "Tip: if using temporary credentials, set AWS_SESSION_TOKEN as well."
        )
    if preflight_only:
        print("Preflight complete. Exiting without evaluation.")
        return True
    return False


def evaluate_single_case(
    case_index: int,
    case: Dict[str, Any],
    source_entry: Optional[Dict[str, Any]],
    bedrock_client: Optional[Any],
    model_id: str,
    pass_threshold: float,
    skip_llm: bool,
) -> Dict[str, Any]:
    response_text = select_output_text(source_entry or {}) if source_entry else ""
    source_score, source_reasoning = extract_source_metadata(source_entry)

    if skip_llm:
        llm_score, reasoning_gaps, missed_points, corrected_reasoning, llm_error = (
            None,
            "LLM evaluation skipped by CLI option.",
            [],
            source_reasoning or "No existing reasoning.",
            None,
        )
    else:
        llm_score, reasoning_gaps, missed_points, corrected_reasoning, llm_error = bedrock_judge(
            client=bedrock_client,
            model_id=model_id,
            case=case,
            response_text=response_text,
            existing_score=source_score,
            existing_reasoning=source_reasoning,
        )

    final_score = llm_score if llm_score is not None else int(source_score or 0)
    reasoning = corrected_reasoning
    passed = final_score >= pass_threshold

    score_delta_vs_source = None
    if source_score is not None:
        score_delta_vs_source = round(float(final_score) - float(source_score), 2)

    missed_text = " | ".join(missed_points) if missed_points else "None"
    suggested_update = f"Updated score: {final_score} | {corrected_reasoning}"

    return {
        "case_id": f"TC-{case_index:03d}",
        "test_case": case,
        "output": response_text,
        "score": final_score,
        "reasoning": reasoning,
        "passed": passed,
        "llm_raw_score": llm_score,
        "llm_error": llm_error,
        "source_score": source_score,
        "source_reasoning": source_reasoning,
        "score_delta_vs_source": score_delta_vs_source,
        "reasoning_gaps": reasoning_gaps,
        "reasoning_missed_points": missed_text,
        "suggested_update": suggested_update,
    }


def build_summary(evaluated_results: List[Dict[str, Any]], pass_threshold: float) -> Dict[str, Any]:
    scores = [int(item["score"]) for item in evaluated_results]
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
    evaluated_results: List[Dict[str, Any]] = []
    for i, case in enumerate(dataset, start=1):
        source_entry = resolve_source_entry(output_index, output_items, case, normalize_case_key(case))
        if source_entry is None:
            scenario = str(case.get("scenario", "<unknown scenario>"))
            raise ValueError(
                "Could not find matching source evaluation for dataset case: "
                f"{scenario}. Ensure --responses file corresponds to the same dataset."
            )

        evaluated_results.append(
            evaluate_single_case(
                case_index=i,
                case=case,
                source_entry=source_entry,
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
