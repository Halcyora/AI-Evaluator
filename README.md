# Task 7 - Prompt Evaluation Application (AWS Bedrock)

This project now includes a repeatable prompt-evaluation pipeline that:

1. Reads test cases from `context/dataset.json`
2. Ingests matching response text from `context/output.json`
3. Validates each response against `solution_criteria` and restrictions
4. Uses AWS Bedrock LLM judging with strict prompt templates
5. Produces scored machine-readable results in `context/output.json`
6. Generates a human-readable report in `context/output.html`

## Architecture (Local + AWS)

- Local CLI application: `app/prompt_evaluator.py`
- AWS service used: Bedrock Runtime (`converse` API)
- Input: `context/dataset.json`, `context/output.json`
- Output: `context/output.json`, `context/output.html`

## Evaluation Strategy

The app uses a hybrid approach:

- Deterministic prechecks for common failures:
  - Missing required sections (breakfast/lunch/dinner/snacks)
  - Wrong meal counts (for exact count criteria)
  - Unmet restrictions (vegetarian, nut-free, dairy-free, halal, etc.)
  - Incomplete nutrition totals / unverifiable macro constraints
  - Truncated output
- LLM judge prompt template with strict JSON output schema:
  - Criteria-by-criteria pass/fail with evidence
  - Integer score (0-10)
  - Brief reasoning
- Final score combines LLM score and deterministic penalties for robustness.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Ensure AWS credentials and region are available (already supported via `.env`):

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_DEFAULT_REGION`

Optionally provide model ID in environment:

- `BEDROCK_MODEL_ID=anthropic.claude-3-5-sonnet-20240620-v1:0`

## Run

From workspace root:

```bash
python app/prompt_evaluator.py
```

Runtime input prompts:

- Pass threshold (0-10)
- Bedrock model ID (if `BEDROCK_MODEL_ID` is not set)

Or run non-interactively:

```bash
python app/prompt_evaluator.py --pass-threshold 7 --model-id anthropic.claude-3-5-sonnet-20240620-v1:0
```

Optional deterministic-only validation run (no Bedrock call):

```bash
python app/prompt_evaluator.py --skip-llm --pass-threshold 7
```

Quick Bedrock connectivity check:

```bash
python app/check_bedrock.py --model-id anthropic.claude-3-5-sonnet-20240620-v1:0 --aws-region us-east-1
```

Run AWS auth preflight only (no evaluation):

```bash
python app/prompt_evaluator.py --preflight-only --model-id anthropic.claude-3-5-sonnet-20240620-v1:0
```

If you get invalid token errors (`UnrecognizedClientException`):

- Verify `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are current and active.
- If using temporary credentials (access key starts with `ASIA`), set `AWS_SESSION_TOKEN`.
- Confirm `AWS_DEFAULT_REGION` or `--aws-region` is correct.
- Re-run preflight before a full evaluation.

## Notes

- Response-to-test-case mapping is primarily by canonical test-case key, with index fallback.
- Existing `context/output.json` is used as response source and then overwritten with evaluated structured output.
- If Bedrock call fails for a case, deterministic checks still run and the error is captured per case in `llm_error`.
