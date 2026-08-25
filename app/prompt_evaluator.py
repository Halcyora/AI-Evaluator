import argparse
import json
import os
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


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


def extract_number(text: str) -> Optional[float]:
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return float(match.group(1))


def parse_weight_kg(case: Dict[str, Any]) -> Optional[float]:
    weight_text = str(case.get("prompt_inputs", {}).get("weight", ""))
    value = extract_number(weight_text)
    return value


def extract_meal_markers(output_text: str) -> Dict[str, bool]:
    text = output_text.lower()
    markers = {
        "breakfast": bool(re.search(r"\bbreakfast\b", text)),
        "lunch": bool(re.search(r"\blunch\b", text)),
        "dinner": bool(re.search(r"\bdinner\b", text)),
        "snack": bool(re.search(r"\bsnack|snacks\b", text)),
    }
    return markers


def estimate_meal_count(output_text: str) -> int:
    lines = [line.strip().lower() for line in output_text.splitlines()]
    heading_meal_hits = 0
    for line in lines:
        if re.search(r"\b(breakfast|lunch|dinner|snack|meal|pre-training|post-workout|recovery)\b", line):
            if line.startswith("##") or line.startswith("**") or line.endswith(":"):
                heading_meal_hits += 1

    if heading_meal_hits > 0:
        # Deduplicate multiple formatting references by capping to plausible day count.
        return min(heading_meal_hits, 10)

    # Fallback: count obvious meal label occurrences.
    fallback = len(re.findall(r"\b(breakfast|lunch|dinner|snack|snacks)\b", output_text.lower()))
    return min(fallback, 10)


def detect_truncation(output_text: str) -> bool:
    text = output_text.strip().lower()
    if "[truncated]" in text:
        return True
    if text.endswith("[truncated]"):
        return True
    # Common signs of cut output
    if text.endswith(("|", "-", "(")):
        return True
    return False


def extract_macro_grams(output_text: str, macro_name: str) -> Optional[float]:
    patterns = [
        rf"{macro_name}\*\*\s*:?\s*(\d+(?:\.\d+)?)\s*g",
        rf"{macro_name}\s*:?\s*(\d+(?:\.\d+)?)\s*g",
    ]
    found: List[float] = []
    for pattern in patterns:
        for m in re.finditer(pattern, output_text, flags=re.IGNORECASE):
            found.append(float(m.group(1)))
    if not found:
        return None
    # In many plans the largest occurrence is the daily total.
    return max(found)


def extract_total_calories(output_text: str) -> Optional[float]:
    patterns = [
        r"\*\*Calories\*\*\s*\|\s*(\d+(?:\.\d+)?)",
        r"Calories\s*:?\s*(\d+(?:\.\d+)?)\s*kcal",
        r"daily\s+total\D{0,10}(\d+(?:\.\d+)?)\s*kcal",
    ]
    found: List[float] = []
    for pattern in patterns:
        for m in re.finditer(pattern, output_text, flags=re.IGNORECASE):
            found.append(float(m.group(1)))
    if not found:
        return None
    return max(found)


def contains_any(text: str, keywords: List[str]) -> Optional[str]:
    lower = text.lower()
    for keyword in keywords:
        if re.search(rf"\b{re.escape(keyword.lower())}\b", lower):
            return keyword
    return None


def make_issue(issue_type: str, severity: str, detail: str) -> Dict[str, str]:
    return {"type": issue_type, "severity": severity, "detail": detail}


def restriction_enabled(restrictions_text: str, terms: List[str]) -> bool:
    return any(term in restrictions_text for term in terms)


def add_keyword_restriction_issue(
    issues: List[Dict[str, str]],
    restrictions_text: str,
    trigger_terms: List[str],
    output_text: str,
    blocked_keywords: List[str],
    detail_prefix: str,
) -> None:
    if not restriction_enabled(restrictions_text, trigger_terms):
        return

    hit = contains_any(output_text, blocked_keywords)
    if hit:
        issues.append(make_issue("unmet_restriction", "major", f"{detail_prefix} '{hit}'."))


def token_numbers_before_phrase(text: str, phrase_tokens: List[str]) -> List[float]:
    tokens = re.split(r"\s+", text.lower().replace("\n", " ").strip())
    out: List[float] = []
    window = len(phrase_tokens)
    if not tokens or window == 0:
        return out

    for i in range(len(tokens) - window):
        if tokens[i + 1 : i + 1 + window] == phrase_tokens:
            value_text = tokens[i].strip(",;:|()[]{}")
            try:
                out.append(float(value_text))
            except ValueError:
                continue
    return out


def extract_sodium_mentions(output_text: str) -> List[float]:
    return token_numbers_before_phrase(output_text, ["mg", "sodium"])


def check_low_sodium_rule(restrictions_text: str, output_text: str) -> List[Dict[str, str]]:
    if not restriction_enabled(restrictions_text, ["low-sodium", "sodium"]):
        return []

    sodium_match = re.search(r"max(?:imum)?\s*(\d+)\s*mg", restrictions_text)
    limit = float(sodium_match.group(1)) if sodium_match else 1500.0
    sodium_mentions = extract_sodium_mentions(output_text)

    if not sodium_mentions:
        return [make_issue("incomplete_totals", "minor", "No explicit sodium total found for low-sodium constraint.")]

    if max(sodium_mentions) > limit:
        return [make_issue("unmet_restriction", "major", f"Sodium appears above limit ({limit} mg).")]

    return []


def check_restriction_compliance(restrictions: str, output_text: str) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    r = restrictions.lower()

    non_veg = [
        "chicken",
        "beef",
        "pork",
        "fish",
        "salmon",
        "tuna",
        "shrimp",
        "turkey",
        "ham",
        "bacon",
        "lamb",
        "meat",
        "seafood",
    ]
    nuts = [
        "almond",
        "walnut",
        "cashew",
        "pistachio",
        "hazelnut",
        "pecan",
        "macadamia",
        "peanut",
        "nut butter",
        "sesame",
        "tahini",
    ]
    dairy = ["milk", "cheese", "yogurt", "butter", "whey", "casein", "cream", "cottage cheese"]
    refined_sugar = ["white sugar", "brown sugar", "corn syrup", "honey", "maple syrup", "syrup"]
    processed_food = ["protein powder", "energy bar", "granola bar", "processed"]
    pork_alcohol = ["pork", "bacon", "ham", "wine", "beer", "alcohol", "rum", "vodka"]

    add_keyword_restriction_issue(
        issues,
        r,
        ["vegetarian"],
        output_text,
        non_veg,
        "Vegetarian restriction violated by",
    )
    add_keyword_restriction_issue(
        issues,
        r,
        ["no nuts", "nut", "allerg"],
        output_text,
        nuts,
        "Nut-free restriction violated by",
    )
    add_keyword_restriction_issue(
        issues,
        r,
        ["no dairy", "lactose"],
        output_text,
        dairy,
        "Dairy restriction violated by",
    )
    add_keyword_restriction_issue(
        issues,
        r,
        ["halal"],
        output_text,
        pork_alcohol,
        "Halal restriction violated by",
    )
    add_keyword_restriction_issue(
        issues,
        r,
        ["no refined sugars"],
        output_text,
        refined_sugar,
        "No-refined-sugar restriction potentially violated by",
    )
    add_keyword_restriction_issue(
        issues,
        r,
        ["no processed foods"],
        output_text,
        processed_food,
        "No-processed-foods restriction potentially violated by",
    )

    if "organic" in r:
        organic_mentions = len(re.findall(r"\borganic\b", output_text.lower()))
        if organic_mentions < 3:
            issues.append(make_issue("unmet_restriction", "major", "Organic-only restriction may not be consistently satisfied."))

    issues.extend(check_low_sodium_rule(r, output_text))

    return issues


def check_exact_meal_count(criterion: str, meal_count: int) -> List[Dict[str, str]]:
    if "exactly 4 meals" in criterion and meal_count != 4:
        return [make_issue("wrong_meal_count", "major", f"Expected exactly 4 meals, detected approximately {meal_count}.")]
    return []


def check_required_main_meals(criterion: str, meal_markers: Dict[str, bool]) -> List[Dict[str, str]]:
    needs_main_meals = "all 3 meals" in criterion or "all three meals" in criterion or "covers all 3 meals" in criterion
    if not needs_main_meals:
        return []

    missing = [k for k in ["breakfast", "lunch", "dinner"] if not meal_markers[k]]
    if missing:
        return [make_issue("missing_sections", "major", "Missing required meals: " + ", ".join(missing))]
    return []


def check_required_snacks(criterion: str, meal_markers: Dict[str, bool]) -> List[Dict[str, str]]:
    if ("plus snacks" in criterion or "and snacks" in criterion) and not meal_markers["snack"]:
        return [make_issue("missing_sections", "major", "Snacks required by criterion but not detected.")]
    return []


def check_meal_range(criterion: str, meal_count: int) -> List[Dict[str, str]]:
    if "3-4 meals/snacks" in criterion and not (3 <= meal_count <= 4):
        return [
            make_issue(
                "wrong_meal_count",
                "major",
                f"Expected 3-4 meals/snacks, detected approximately {meal_count}.",
            )
        ]
    return []


def check_protein_per_kg(criterion: str, case: Dict[str, Any], output_text: str) -> List[Dict[str, str]]:
    req_values = token_numbers_before_phrase(criterion, ["g", "per", "kg"])
    if not req_values:
        return []

    req_per_kg = float(req_values[0])
    weight_kg = parse_weight_kg(case)
    protein_total = extract_macro_grams(output_text, "protein")
    if weight_kg and protein_total:
        required_total = req_per_kg * weight_kg
        if protein_total + 1e-6 < required_total:
            return [
                make_issue(
                    "incomplete_totals",
                    "major",
                    f"Protein appears below required threshold ({required_total:.1f} g).",
                )
            ]
        return []

    return [make_issue("incomplete_totals", "minor", "Could not fully verify protein-per-kg criterion from output totals.")]


def check_protein_percent(criterion: str, output_text: str) -> List[Dict[str, str]]:
    match = re.search(r"protein\s+comprises\s+at\s+least\s+(\d+(?:\.\d+)?)%", criterion)
    if not match:
        return []

    min_pct = float(match.group(1))
    protein_total = extract_macro_grams(output_text, "protein")
    calories = extract_total_calories(output_text)
    if protein_total and calories:
        pct = (protein_total * 4.0 / calories) * 100.0
        if pct + 1e-6 < min_pct:
            return [
                make_issue(
                    "incomplete_totals",
                    "major",
                    f"Protein calories percentage appears low ({pct:.1f}% < {min_pct:.1f}%).",
                )
            ]
        return []

    return [make_issue("incomplete_totals", "minor", "Could not verify protein calorie percentage criterion from totals.")]


def check_carbohydrate_percent(criterion: str, output_text: str) -> List[Dict[str, str]]:
    match = re.search(r"carbohydrates\s+comprise\s+(\d+)-(\d+)%", criterion)
    if not match:
        return []

    low = float(match.group(1))
    high = float(match.group(2))
    carbs_total = extract_macro_grams(output_text, "carbohydrates")
    calories = extract_total_calories(output_text)
    if carbs_total and calories:
        pct = (carbs_total * 4.0 / calories) * 100.0
        if pct < low or pct > high:
            return [
                make_issue(
                    "incomplete_totals",
                    "major",
                    f"Carbohydrate percentage appears outside target ({pct:.1f}% not in {low:.1f}-{high:.1f}%).",
                )
            ]
        return []

    return [make_issue("incomplete_totals", "minor", "Could not verify carbohydrate percentage criterion from totals.")]


def criteria_issues_for_single_rule(
    criterion: str,
    case: Dict[str, Any],
    output_text: str,
    meal_markers: Dict[str, bool],
    meal_count: int,
) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    issues.extend(check_exact_meal_count(criterion, meal_count))
    issues.extend(check_required_main_meals(criterion, meal_markers))
    issues.extend(check_required_snacks(criterion, meal_markers))
    issues.extend(check_meal_range(criterion, meal_count))
    issues.extend(check_protein_per_kg(criterion, case, output_text))
    issues.extend(check_protein_percent(criterion, output_text))
    issues.extend(check_carbohydrate_percent(criterion, output_text))
    return issues


def deduplicate_issues(issues: List[Dict[str, str]]) -> List[Dict[str, str]]:
    unique: Dict[str, Dict[str, str]] = {}
    for issue in issues:
        unique_key = f"{issue['type']}|{issue['detail']}"
        unique[unique_key] = issue
    return list(unique.values())


def criteria_rule_checks(case: Dict[str, Any], output_text: str) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    criteria = [str(c).lower() for c in case.get("solution_criteria", [])]
    meal_markers = extract_meal_markers(output_text)
    meal_count = estimate_meal_count(output_text)

    for criterion in criteria:
        issues.extend(criteria_issues_for_single_rule(criterion, case, output_text, meal_markers, meal_count))

    restrictions = str(case.get("prompt_inputs", {}).get("restrictions", ""))
    if restrictions and restrictions.lower() != "none":
        issues.extend(check_restriction_compliance(restrictions, output_text))

    if detect_truncation(output_text):
        issues.append(make_issue("truncated_output", "major", "Output appears truncated or incomplete."))

    if not re.search(r"\b(calories|protein|carbohydrates|fat|daily total|daily totals)\b", output_text.lower()):
        issues.append(make_issue("incomplete_totals", "minor", "No clear daily nutrition totals were detected."))

    return deduplicate_issues(issues)


def build_evaluator_prompt(case: Dict[str, Any], response_text: str, deterministic_issues: List[Dict[str, str]]) -> str:
    schema_hint = {
        "score_0_to_10": "integer",
        "reasoning": "string (max 120 words)",
        "criteria_results": [
            {
                "criterion": "string",
                "met": "boolean",
                "evidence": "string",
                "issue_type": "one of: missing_sections|wrong_meal_count|unmet_restriction|incomplete_totals|truncated_output|other",
            }
        ],
        "critical_failures": ["string"],
    }

    return (
        "You are a strict prompt-evaluation judge for athlete meal-plan responses. "
        "Evaluate only against the provided solution criteria and restrictions. "
        "Be conservative: if evidence is missing, mark unmet.\n\n"
        "Scoring policy:\n"
        "- Start from 10.\n"
        "- Major criterion failure: -3 each.\n"
        "- Minor criterion failure: -1 each.\n"
        "- Truncated/incomplete response: at least one major failure.\n"
        "- Keep score in [0,10].\n"
        "- Return integer score.\n\n"
        "Special checks to enforce:\n"
        "- If criterion says exactly 4 meals, more or fewer is a failure.\n"
        "- Respect explicit diet restrictions (vegetarian, nut-free, dairy-free, halal, gluten-free, organic-only, low-sodium, no refined sugar, no processed foods).\n"
        "- Flag missing sections and missing totals where required by criteria.\n\n"
        "Return STRICT JSON only, no markdown, matching this schema:\n"
        f"{json.dumps(schema_hint, ensure_ascii=False)}\n\n"
        "TEST_CASE:\n"
        f"{json.dumps(case, ensure_ascii=False)}\n\n"
        "DETERMINISTIC_PRECHECK_ISSUES:\n"
        f"{json.dumps(deterministic_issues, ensure_ascii=False)}\n\n"
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
    deterministic_issues: List[Dict[str, str]],
) -> Tuple[Optional[int], str, List[Dict[str, Any]], List[str], Optional[str]]:
    prompt = build_evaluator_prompt(case, response_text, deterministic_issues)

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
        return None, "LLM evaluation unavailable due to AWS error.", [], [], str(exc)

    contents = response.get("output", {}).get("message", {}).get("content", [])
    text_parts = [part.get("text", "") for part in contents if isinstance(part, dict)]
    model_text = "\n".join(t for t in text_parts if t)

    parsed = extract_json_object(model_text)
    if not parsed:
        return None, "LLM response was not valid JSON.", [], [], None

    score = parsed.get("score_0_to_10")
    score_int: Optional[int]
    if isinstance(score, (int, float)):
        score_int = int(round(float(score)))
        score_int = max(0, min(10, score_int))
    else:
        score_int = None

    reasoning = str(parsed.get("reasoning", "")).strip() or "No reasoning provided by evaluator."
    criteria_results = parsed.get("criteria_results", [])
    if not isinstance(criteria_results, list):
        criteria_results = []
    critical_failures = parsed.get("critical_failures", [])
    if not isinstance(critical_failures, list):
        critical_failures = []

    return score_int, reasoning, criteria_results, [str(x) for x in critical_failures], None


def severity_penalty(severity: str) -> int:
    if severity == "major":
        return 2
    if severity == "minor":
        return 1
    return 1


def combine_scores(llm_score: Optional[int], deterministic_issues: List[Dict[str, str]]) -> int:
    baseline = llm_score if llm_score is not None else 8
    penalty = sum(severity_penalty(issue.get("severity", "minor")) for issue in deterministic_issues)
    final = baseline - penalty
    return max(0, min(10, final))


def short_reasoning(llm_reasoning: str, deterministic_issues: List[Dict[str, str]]) -> str:
    if not deterministic_issues:
        return llm_reasoning or "All required criteria appear satisfied."

    top_issues = "; ".join(issue.get("detail", "") for issue in deterministic_issues[:3])
    if llm_reasoning:
        return f"{llm_reasoning} Key detected issues: {top_issues}"
    return f"Detected issues: {top_issues}"


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

        issues_html = "<br>".join(
            f"[{escape(issue.get('severity', 'minor'))}] {escape(issue.get('detail', ''))}"
            for issue in item.get("issues_detected", [])
        )
        if not issues_html:
            issues_html = "None"

        rows.append(
            "\n".join(
                [
                    "<tr>",
                    f"<td>{escape(str(item['case_id']))}</td>",
                    f"<td>{escape(str(item['test_case'].get('scenario', '')))}</td>",
                    f"<td>{prompt_inputs_html}</td>",
                    f"<td>{criteria_html}</td>",
                    f"<td><pre>{escape(str(item.get('output', '')))}</pre></td>",
                    f"<td><span class=\"score score-{score_class(int(item['score']))}\">{item['score']}</span></td>",
                    f"<td>{escape(str(item.get('reasoning', '')))}</td>",
                    f"<td>{issues_html}</td>",
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
            <th>Score</th>
            <th>Reasoning</th>
            <th>Issues</th>
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
        case_key = normalize_case_key(case)
        source_entry = output_index.get(case_key)

        if not source_entry and i - 1 < len(output_items):
            # Fallback by position if output file does not embed a full test case object.
            source_entry = output_items[i - 1]

        response_text = select_output_text(source_entry or {}) if source_entry else ""

        deterministic_issues = criteria_rule_checks(case, response_text)

        if skip_llm:
            llm_score, llm_reasoning, criteria_results, critical_failures, llm_error = (
                None,
                "LLM evaluation skipped by CLI option.",
                [],
                [],
                None,
            )
        else:
            llm_score, llm_reasoning, criteria_results, critical_failures, llm_error = bedrock_judge(
                client=bedrock_client,
                model_id=model_id,
                case=case,
                response_text=response_text,
                deterministic_issues=deterministic_issues,
            )

        if critical_failures:
            for failure in critical_failures:
                deterministic_issues.append(
                    {
                        "type": "other",
                        "severity": "major",
                        "detail": f"LLM critical failure: {failure}",
                    }
                )

        final_score = combine_scores(llm_score, deterministic_issues)
        reasoning = short_reasoning(llm_reasoning, deterministic_issues)
        passed = final_score >= pass_threshold

        evaluated_results.append(
            {
                "case_id": f"TC-{i:03d}",
                "test_case": case,
                "output": response_text,
                "score": final_score,
                "reasoning": reasoning,
                "passed": passed,
                "issues_detected": deterministic_issues,
                "criteria_results": criteria_results,
                "llm_raw_score": llm_score,
                "llm_error": llm_error,
            }
        )

    scores = [int(item["score"]) for item in evaluated_results]
    passed_count = sum(1 for item in evaluated_results if item["passed"])

    summary = {
        "total_cases": len(evaluated_results),
        "average_score": round(mean(scores), 2) if scores else 0.0,
        "pass_threshold": pass_threshold,
        "passed_cases": passed_count,
        "failed_cases": len(evaluated_results) - passed_count,
        "pass_rate_percent": round((passed_count / len(evaluated_results)) * 100.0, 2) if evaluated_results else 0.0,
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_id": model_id,
        "summary": summary,
        "results": evaluated_results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prompt evaluation pipeline using AWS Bedrock.")
    parser.add_argument("--dataset", default="context/dataset.json", help="Path to dataset JSON.")
    parser.add_argument("--responses", default="context/output.json", help="Path to existing responses JSON.")
    parser.add_argument("--output-json", default="context/output.json", help="Path to write evaluated JSON report.")
    parser.add_argument("--output-html", default="context/output.html", help="Path to write HTML report.")
    parser.add_argument("--pass-threshold", type=float, default=None, help="Pass threshold score in range 0-10.")
    parser.add_argument("--model-id", default=None, help="AWS Bedrock model ID.")
    parser.add_argument("--skip-llm", action="store_true", help="Run deterministic checks only and skip Bedrock calls.")
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

    model_id = "deterministic-only"
    if not args.skip_llm:
        model_id = resolve_model_id(args.model_id)

    dataset = load_json(dataset_path)
    if not isinstance(dataset, list):
        raise ValueError("Dataset JSON must be a list of test cases.")

    responses = load_json(responses_path)
    if isinstance(responses, dict) and "results" in responses:
        output_items = responses["results"]
    elif isinstance(responses, list):
        output_items = responses
    else:
        raise ValueError("Responses JSON must be a list, or an object containing 'results'.")

    if not isinstance(output_items, list):
        raise ValueError("Response entries must be a list.")

    region_name = args.aws_region or os.getenv("AWS_DEFAULT_REGION")

    if not args.skip_llm:
        ok, preflight_message = validate_aws_auth_for_bedrock(region_name=region_name)
        print(preflight_message)
        if not ok:
            raise RuntimeError(
                "AWS preflight failed. Fix credentials first, then rerun. "
                "Tip: if using temporary credentials, set AWS_SESSION_TOKEN as well."
            )
        if args.preflight_only:
            print("Preflight complete. Exiting without evaluation.")
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
