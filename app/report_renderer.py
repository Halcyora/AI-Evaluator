from html import escape
from typing import Any, Dict


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

        score_value = item.get("score")
        score_text = "-" if score_value is None else str(score_value)
        reasoning_text = escape(str(item.get("reasoning", "")))
        reasoning_audit_html = escape(str(item.get("reasoning_audit") or "None"))
        audit_score = item.get("audit_score")
        audit_score_text = "-" if audit_score is None else str(audit_score)

        rows.append(
            "\n".join(
                [
                    "<tr>",
                    f"<td>{escape(str(item['test_case'].get('scenario', '')))}</td>",
                    f"<td>{prompt_inputs_html}</td>",
                    f"<td>{criteria_html}</td>",
                    f"<td><pre>{escape(str(item.get('output', '')))}</pre></td>",
              f"<td><span class=\"score score-{score_class(int(float(score_value) if score_value is not None else 0))}\">{score_text}</span></td>",
              f"<td>{reasoning_text}</td>",
                    f"<td>{reasoning_audit_html}</td>",
              f"<td><span class=\"score score-{score_class(int(float(audit_score) if audit_score is not None else 0))}\">{audit_score_text}</span></td>",
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
            <th>Scenario</th>
            <th>Prompt Inputs</th>
            <th>Solution Criteria</th>
            <th>Output</th>
            <th>Score</th>
            <th>Reasoning</th>
            <th>Reasoning Audit</th>
            <th>Audit Score</th>
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
