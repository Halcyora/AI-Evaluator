import os
from typing import Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError


def resolve_model_id(cli_value: Optional[str]) -> str:
    if cli_value:
        return cli_value

    env_model = os.getenv("BEDROCK_MODEL_ID", "").strip()
    if env_model:
        return env_model

    raw = input("Enter AWS Bedrock model ID (for example, anthropic.claude-3-5-sonnet-20240620-v1:0): ").strip()
    if raw:
        return raw
    raise ValueError("Bedrock model ID is required.")


def validate_aws_auth_for_bedrock(region_name: Optional[str]) -> Tuple[bool, str]:
    try:
        session = boto3.Session(region_name=region_name)
        creds = session.get_credentials()
        if creds is None:
            return False, "No AWS credentials were found. Configure credentials via .env, environment variables, or AWS profile."

        frozen = creds.get_frozen_credentials()
        access_key = frozen.access_key or ""
        if not access_key:
            return False, "AWS access key is missing."

        if access_key.startswith("ASIA") and not (frozen.token or ""):
            return False, "Temporary AWS credentials detected (ASIA...) but AWS session token is missing. Set AWS_SESSION_TOKEN."

        sts_client = session.client("sts")
        identity = sts_client.get_caller_identity()
        arn = identity.get("Arn", "unknown")
        return True, f"AWS auth verified for principal: {arn}"
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "UnrecognizedClientException":
            return (
                False,
                "AWS credentials are invalid for this request (UnrecognizedClientException). "
                "Rotate/recreate credentials and ensure region is correct.",
            )
        if error_code == "ExpiredToken":
            return False, "AWS token is expired. Refresh credentials and set AWS_SESSION_TOKEN if using temporary credentials."
        if error_code == "InvalidClientTokenId":
            return False, "AWS access key appears invalid. Verify AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
        return False, f"AWS auth check failed with {error_code}: {exc}"
    except BotoCoreError as exc:
        return False, f"AWS SDK error during auth preflight: {exc}"


def preflight_bedrock_or_raise(region_name: Optional[str], preflight_only: bool) -> bool:
    ok, preflight_message = validate_aws_auth_for_bedrock(region_name=region_name)
    print(preflight_message)
    if not ok:
        raise RuntimeError(
            "AWS preflight failed. Fix credentials first, then rerun. "
            "Tip: if using temporary credentials, set AWS_SESSION_TOKEN as well."
        )
    if preflight_only:
        print("Preflight complete. Exiting without evaluation.")
        return True
    return False
