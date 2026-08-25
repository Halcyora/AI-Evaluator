import argparse
import json
import os
import sys
from typing import Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv


def validate_aws_auth(region_name: Optional[str]) -> Tuple[bool, str]:
    try:
        session = boto3.Session(region_name=region_name)
        creds = session.get_credentials()
        if creds is None:
            return False, "No AWS credentials found. Configure AWS credentials or .env values first."

        frozen = creds.get_frozen_credentials()
        access_key = frozen.access_key or ""
        if not access_key:
            return False, "AWS_ACCESS_KEY_ID is missing."

        if access_key.startswith("ASIA") and not (frozen.token or ""):
            return False, "Temporary credentials detected (ASIA...) but AWS_SESSION_TOKEN is missing."

        sts_client = session.client("sts")
        identity = sts_client.get_caller_identity()
        arn = identity.get("Arn", "unknown")
        return True, f"AWS auth verified for principal: {arn}"
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        return False, f"AWS auth check failed with {error_code}: {exc}"
    except BotoCoreError as exc:
        return False, f"AWS SDK error during auth preflight: {exc}"


def resolve_model_id(cli_model_id: Optional[str]) -> str:
    if cli_model_id:
        return cli_model_id

    env_model_id = os.getenv("BEDROCK_MODEL_ID", "").strip()
    if env_model_id:
        return env_model_id

    raise ValueError("Bedrock model ID is required. Pass --model-id or set BEDROCK_MODEL_ID.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether AWS Bedrock is accessible from this environment.")
    parser.add_argument("--model-id", default=None, help="Bedrock model ID, e.g. anthropic.claude-3-5-sonnet-20240620-v1:0")
    parser.add_argument("--aws-region", default=None, help="AWS region override. Falls back to AWS_DEFAULT_REGION.")
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: BEDROCK_OK",
        help="Small test prompt used for the connectivity check.",
    )
    parser.add_argument("--max-tokens", type=int, default=64, help="Max tokens for test call.")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    region_name = args.aws_region or os.getenv("AWS_DEFAULT_REGION")
    model_id = resolve_model_id(args.model_id)

    ok, message = validate_aws_auth(region_name=region_name)
    print(message)
    if not ok:
        return 1

    try:
        client = boto3.client("bedrock-runtime", region_name=region_name)
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": args.prompt}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": max(1, args.max_tokens)},
        )
    except (ClientError, BotoCoreError) as exc:
        print(f"Bedrock request failed: {exc}")
        return 1

    content = response.get("output", {}).get("message", {}).get("content", [])
    text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
    model_text = "\n".join([t for t in text_parts if t]).strip()

    print("Bedrock request succeeded.")
    print(f"Model: {model_id}")
    print(f"Region: {region_name or 'default session region'}")
    print("Model response preview:")
    print(json.dumps({"text": model_text[:500]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1)