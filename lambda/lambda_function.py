import urllib.parse

import requests

BACKEND_URL = ""
AUTH_TOKEN = ""


def lambda_handler(event, context):
    """
    AWS Lambda entrypoint.
    Send a post request informing backend of a new email arrives.
    """
    bucket = event["Records"][0]["s3"]["bucket"]["name"]
    key = urllib.parse.unquote_plus(event["Records"][0]["s3"]["object"]["key"])

    payload = {"bucket": bucket, "key": key}

    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            f"{BACKEND_URL}/emails/", headers=headers, json=payload, timeout=10
        )
        return {
            "statusCode": response.status_code,
            "body": "sent new email notification to the backend",
        }
    except Exception:
        return {
            "statusCode": 500,
            "body": "error while email notification to the backend",
        }
