import json
import re
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from prompt_template import build_evaluator_prompt


def select_output_text(output_entry: Dict[str, Any]) -> str:
    value = output_entry.get("output", "")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


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


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None

    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

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
) -> Tuple[Optional[int], str, Optional[str]]:
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
        return None, "LLM evaluation unavailable due to AWS error.", str(exc)

    contents = response.get("output", {}).get("message", {}).get("content", [])
    text_parts = [part.get("text", "") for part in contents if isinstance(part, dict)]
    model_text = "\n".join(t for t in text_parts if t)

    parsed = extract_json_object(model_text)
    if not parsed:
        return None, "LLM response was not valid JSON.", None

    score = parsed.get("audit_score_0_to_10")
    if score is None:
        score = parsed.get("updated_score_0_to_10")
    score_int: Optional[int]
    if isinstance(score, (int, float)):
        score_int = int(round(float(score)))
        score_int = max(0, min(10, score_int))
    else:
        score_int = None

    reasoning_audit = str(parsed.get("reasoning_audit", "")).strip() or "No reasoning audit provided."

    return score_int, reasoning_audit, None


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
    base_score = int(round(source_score)) if source_score is not None else 0
    base_reasoning = source_reasoning or ""

    if skip_llm:
        audit_score, reasoning_audit, llm_error = (
            base_score,
            "LLM evaluation skipped by CLI option.",
            None,
        )
    else:
        audit_score, reasoning_audit, llm_error = bedrock_judge(
            client=bedrock_client,
            model_id=model_id,
            case=case,
            response_text=response_text,
            existing_score=source_score,
            existing_reasoning=source_reasoning,
        )

    final_audit_score = audit_score if audit_score is not None else base_score
    passed = final_audit_score >= pass_threshold

    return {
        "case_id": f"TC-{case_index:03d}",
        "test_case": case,
        "output": response_text,
        "score": base_score,
        "reasoning": base_reasoning,
        "passed": passed,
        "reasoning_audit": reasoning_audit,
        "audit_score": final_audit_score,
        "llm_error": llm_error,
    }
