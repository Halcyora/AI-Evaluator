import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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
