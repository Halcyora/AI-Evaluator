from pathlib import Path
from typing import Any, Dict, List

from source_html import parse_output_html_responses
from source_json import load_output_json_responses


def load_output_items(path: Path) -> List[Dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".html":
        return parse_output_html_responses(path)

    return load_output_json_responses(path)
