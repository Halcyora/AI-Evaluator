import json
from typing import Any, Dict, Optional


def build_evaluator_prompt(
    case: Dict[str, Any],
    response_text: str,
    existing_score: Optional[float],
    existing_reasoning: Optional[str],
) -> str:
    schema_hint = {
        "audit_score_0_to_10": "integer (how correct the EXISTING score/reasoning is; 10 = fully correct, 0 = fully incorrect)",
        "reasoning_audit": "string (concise explanation of what is wrong/missed in existing reasoning)",
    }

    return (
        "ROLE:\n"
        "You are an expert evaluation auditor. Your job is to audit the correctness of an EXISTING score and EXISTING reasoning. "
        "You are not allowed to re-grade the meal plan directly or rewrite the full evaluation.\n\n"
        "OBJECTIVE:\n"
        "Judge how accurate the existing evaluation is, based on the test case requirements and the candidate response text.\n\n"
        "METHOD (follow in order):\n"
        "1) Extract required criteria and restrictions from TEST_CASE.\n"
        "2) Check whether claims made in EXISTING_EVALUATION.reasoning are supported by CANDIDATE_RESPONSE.\n"
        "3) Check whether EXISTING_EVALUATION.score is calibrated to that reasoning.\n"
        "4) Report only the audit verdict: what is correct, what is wrong/missing/overstated, and the audit score.\n\n"
        "AUDIT SCORE RUBRIC (integer 0-10):\n"
        "- 10: Existing score and reasoning are fully correct and well-supported.\n"
        "- 8-9: Mostly correct; only minor omissions or wording issues.\n"
        "- 6-7: Partly correct; noticeable misses or slight score miscalibration.\n"
        "- 4-5: Mixed/weak; several incorrect or unsupported claims.\n"
        "- 2-3: Largely incorrect; major reasoning or score errors.\n"
        "- 0-1: Fundamentally wrong or contradictory evaluation.\n\n"
        "RULES:\n"
        "- Use only TEST_CASE and CANDIDATE_RESPONSE as evidence.\n"
        "- Score correctness of the existing evaluation, not meal-plan quality by itself.\n"
        "- If response text is truncated/unclear, mention uncertainty briefly in reasoning_audit.\n"
        "- reasoning_audit must be concise (2-5 sentences), specific, and factual.\n"
        "- audit_score_0_to_10 must be an integer in [0, 10].\n"
        "- Do not output markdown, explanations, or extra keys.\n\n"
        "Return STRICT JSON only with exactly this schema:\n"
        f"{json.dumps(schema_hint, ensure_ascii=False)}\n\n"
        "TEST_CASE:\n"
        f"{json.dumps(case, ensure_ascii=False)}\n\n"
        "EXISTING_EVALUATION:\n"
        f"{json.dumps({'score': existing_score, 'reasoning': existing_reasoning}, ensure_ascii=False)}\n\n"
        "CANDIDATE_RESPONSE:\n"
        f"{response_text}\n"
    )
