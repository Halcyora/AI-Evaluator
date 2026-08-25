import json
from typing import Any, Dict, List, Optional


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


def build_response_index(output_items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for item in output_items:
        case = item.get("test_case")
        if isinstance(case, dict):
            index[normalize_case_key(case)] = item
    return index


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


def build_dataset_index(dataset: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for case in dataset:
        index[normalize_case_key(case)] = case
    return index


def resolve_dataset_case(
    dataset_index: Dict[str, Dict[str, Any]],
    dataset: List[Dict[str, Any]],
    source_case: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    strict_key = normalize_case_key(source_case)
    case = dataset_index.get(strict_key)
    if case is not None:
        return case

    relaxed_key = normalize_case_key_relaxed(source_case)
    for candidate in dataset:
        if normalize_case_key_relaxed(candidate) == relaxed_key:
            return candidate

    return None
