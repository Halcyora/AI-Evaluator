# Task - Prompt Evaluation Audit Pipeline (AWS Bedrock)

This project audits an existing evaluation instead of re-evaluating responses from scratch.

The pipeline checks whether existing Score and Reasoning are correct for each case and writes audit results to the results folder.

## Quick Start

1. Copy [.env.example](.env.example) to .env and fill AWS credentials, region, and optional model ID.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the evaluator:

```bash
python app/prompt_evaluator.py --dataset dataset/dataset.json --responses dataset/output.html --output-json results/output_llm.json --output-html results/output_llm.html --pass-threshold 7 --model-id amazon.nova-pro-v1:0
```

## What This App Does

1. Reads test cases from [dataset/dataset.json](dataset/dataset.json).
2. Reads existing evaluated rows from [dataset/output.html](dataset/output.html) or [dataset/output.json](dataset/output.json).
3. Sends each case to AWS Bedrock with a strict auditor prompt.
4. Produces:
   - [results/output_llm.json](results/output_llm.json) (machine-readable)
   - [results/output_llm.html](results/output_llm.html) (human-readable)

Important behavior:

- The app preserves the original 6 columns from source evaluation data.
- It adds only 2 audit columns: Reasoning Audit and Audit Score.
- Audit Score is not a replacement meal-plan score. It is a correctness score for the existing evaluation.

## Final Report Columns

The final HTML report contains exactly 8 columns in this order:

1. Scenario
2. Prompt Inputs
3. Solution Criteria
4. Output
5. Score
6. Reasoning
7. Reasoning Audit
8. Audit Score

Column meaning:

- Score: original score from the input evaluation source.
- Reasoning: original reasoning from the input evaluation source.
- Reasoning Audit: auditor explanation of what is correct/incorrect in existing reasoning.
- Audit Score: integer 0-10 rating of how correct the existing Score + Reasoning are.

## Prompt Input Terms

In the audit prompt:

- TEST_CASE means the structured case metadata from [dataset/dataset.json](dataset/dataset.json).
  - Includes scenario, prompt_inputs, solution_criteria, and task_description.
- CANDIDATE_RESPONSE means the Output text from the source evaluation row.
- EXISTING_EVALUATION means the original Score and Reasoning being audited.

## Current Architecture

- Main CLI: [app/prompt_evaluator.py](app/prompt_evaluator.py)
- AWS helpers and auth preflight: [app/aws_utils.py](app/aws_utils.py)
- Prompt template: [app/prompt_template.py](app/prompt_template.py)
- Audit engine and response parsing: [app/audit_engine.py](app/audit_engine.py)
- Case matching and source/dataset mapping: [app/case_matching.py](app/case_matching.py)
- Source loader:
  - [app/source_loader.py](app/source_loader.py)
  - [app/source_html.py](app/source_html.py)
  - [app/source_json.py](app/source_json.py)
- HTML report renderer: [app/report_renderer.py](app/report_renderer.py)

## Data Flow

1. Load dataset list from [dataset/dataset.json](dataset/dataset.json).
2. Load source rows from [dataset/output.html](dataset/output.html) or [dataset/output.json](dataset/output.json).
3. Match source rows to dataset cases using normalized keys.
4. Evaluate in source-row order, so output row order follows the source report order.
5. For each row:
   - keep source Score and source Reasoning unchanged in report columns 5 and 6
   - generate Reasoning Audit and Audit Score with Bedrock
6. Write JSON + HTML artifacts to the results folder.

## Requirements

Python dependencies in [requirements.txt](requirements.txt):

- boto3
- python-dotenv

Install:

```bash
pip install -r requirements.txt
```

## AWS Configuration

Before running, copy [.env.example](.env.example) to .env and fill your real values.

Set environment variables (or .env values):

- AWS_ACCESS_KEY_ID
- AWS_SECRET_ACCESS_KEY
- AWS_DEFAULT_REGION

If using temporary credentials, also set:

- AWS_SESSION_TOKEN

Optional model variable:

- BEDROCK_MODEL_ID

## Run Commands

Default run (uses HTML source):

```bash
python app/prompt_evaluator.py
```

Non-interactive run:

```bash
python app/prompt_evaluator.py --dataset dataset/dataset.json --responses dataset/output.html --output-json results/output_llm.json --output-html results/output_llm.html --pass-threshold 7 --model-id amazon.nova-pro-v1:0
```

Run with JSON source:

```bash
python app/prompt_evaluator.py --dataset dataset/dataset.json --responses dataset/output.json --output-json results/output_llm.json --output-html results/output_llm.html --pass-threshold 7 --model-id amazon.nova-pro-v1:0
```

Skip LLM (copies source Score/Reasoning and fills audit fields with skip message):

```bash
python app/prompt_evaluator.py --dataset dataset/dataset.json --responses dataset/output.html --output-json results/output_llm.json --output-html results/output_llm.html --pass-threshold 7 --skip-llm
```

AWS preflight only:

```bash
python app/prompt_evaluator.py --preflight-only --model-id amazon.nova-pro-v1:0
```

## CLI Arguments

- --dataset: path to dataset JSON (default dataset/dataset.json)
- --responses: path to source evaluation file (.html or .json)
- --output-json: path for output JSON report
- --output-html: path for output HTML report
- --pass-threshold: numeric pass threshold in range 0-10
- --model-id: Bedrock model ID
- --skip-llm: skip Bedrock and do no audit call
- --aws-region: override AWS region
- --preflight-only: validate AWS auth and exit

## Output JSON Shape

Top-level keys:

- generated_at
- model_id
- summary
- results

Per-result keys:

- case_id
- test_case
- output
- score
- reasoning
- reasoning_audit
- audit_score
- passed
- llm_error

Notes:

- score is the existing source score.
- audit_score is the auditor's 0-10 correctness score for existing evaluation.
- summary.average_score is computed from audit_score.

## Troubleshooting

If AWS auth fails:

- Verify AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.
- Ensure AWS_SESSION_TOKEN is set for temporary ASIA credentials.
- Verify AWS_DEFAULT_REGION or pass --aws-region.
- Run with --preflight-only first.

If source rows do not match dataset cases:

- Ensure [dataset/dataset.json](dataset/dataset.json) and source file come from the same evaluation set.
- Ensure source file has valid test_case metadata.

If you see differences between source and output Score column:

- Compare by Scenario identity, not raw row position from different files.
- The pipeline now evaluates in source-row order and keeps source Score in column 5.
