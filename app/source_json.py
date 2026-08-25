import json
from pathlib import Path
from typing import Any, Dict, List


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_output_json_responses(responses: Any) -> List[Dict[str, Any]]:
    if isinstance(responses, dict) and "results" in responses:
        output_items = responses["results"]
    elif isinstance(responses, list):
        output_items = responses
    else:
        raise ValueError("Responses JSON must be a list, or an object containing 'results'.")

    if not isinstance(output_items, list):
        raise ValueError("Response entries must be a list.")

    return output_items


def load_output_json_responses(path: Path) -> List[Dict[str, Any]]:
    return parse_output_json_responses(load_json(path))
