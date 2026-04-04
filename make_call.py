#!/usr/bin/env python3
"""
Outbound call via Exotel -> connects candidate to ElevenLabs Kavitha agent.

Usage:
  python make_call.py                          # calls TEST_CANDIDATE_NUMBER from .env
  python make_call.py +919876543210            # calls specific number
  python make_call.py +919876543210 https://.. # calls with custom webhook URL
"""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

EXOTEL_API_KEY     = os.getenv("EXOTEL_API_KEY")
EXOTEL_API_TOKEN   = os.getenv("EXOTEL_API_TOKEN")
EXOTEL_ACCOUNT_SID = os.getenv("EXOTEL_ACCOUNT_SID", "supernan1")
EXOTEL_PHONE_NUMBER = os.getenv("EXOTEL_PHONE_NUMBER")
SERVER_BASE_URL    = "https://web-production-966f.up.railway.app"
TEST_CANDIDATE     = os.getenv("TEST_CANDIDATE_NUMBER")

EXOTEL_API_URL = f"https://api.exotel.com/v1/Accounts/{EXOTEL_ACCOUNT_SID}/Calls/connect"


def validate():
    missing = []
    for var, val in [
        ("EXOTEL_API_KEY", EXOTEL_API_KEY),
        ("EXOTEL_API_TOKEN", EXOTEL_API_TOKEN),
        ("EXOTEL_ACCOUNT_SID", EXOTEL_ACCOUNT_SID),
        ("EXOTEL_PHONE_NUMBER", EXOTEL_PHONE_NUMBER),
    ]:
        if not val:
            missing.append(var)
    if missing:
        print(f"[ERROR] Missing .env variables: {', '.join(missing)}")
        sys.exit(1)


def make_call(to_number: str, webhook_base_url: str) -> dict:
    answer_url = f"{webhook_base_url.rstrip('/')}/answer"
    status_url = f"{webhook_base_url.rstrip('/')}/status"

    from_number = EXOTEL_PHONE_NUMBER.replace("-", "")

    # Exotel India requires 0XXXXXXXXXX format (11 digits with leading 0)
    if to_number.startswith("+91"):
        to_number = "0" + to_number[3:]   # +919345473240 -> 09345473240
    elif to_number.startswith("91") and len(to_number) == 12:
        to_number = "0" + to_number[2:]   # 919345473240  -> 09345473240

    payload = {
        "From":           to_number,    # candidate's number — Exotel dials this first
        "To":             from_number,  # ExoPhone — triggers the Kavitha flow
        "CallerId":       from_number,  # what candidate sees as caller ID
        "Url":            "http://my.exotel.com/supernan1/exoml/start_voice/1218626",
        "StatusCallback": status_url,
        "Record":         "false",
    }

    print(f"\n[INFO] Calling {to_number}")
    print(f"[INFO] From:        {from_number}")
    print(f"[INFO] Answer URL:  {answer_url}")
    print(f"[INFO] Status URL:  {status_url}\n")

    response = requests.post(
        EXOTEL_API_URL,
        data=payload,
        auth=(EXOTEL_API_KEY, EXOTEL_API_TOKEN),
        timeout=30,
    )

    if not response.ok:
        print(f"[ERROR] Exotel {response.status_code}: {response.text}")
        response.raise_for_status()

    raw = response.text.strip()
    print(f"[SUCCESS] Call initiated")
    print(f"  HTTP Status: {response.status_code}")
    print(f"  Full response: {response.text[:1000]}")

    # Exotel may return XML or JSON depending on endpoint version
    if raw.startswith("{"):
        import json as _json
        result = _json.loads(raw)
        call = result.get("Call", result)
        print(f"  SID:    {call.get('Sid', 'n/a')}")
        print(f"  Status: {call.get('Status', 'n/a')}")
    elif raw.startswith("<"):
        import re
        sid = re.search(r"<Sid>(.*?)</Sid>", raw)
        status = re.search(r"<Status>(.*?)</Status>", raw)
        print(f"  SID:    {sid.group(1) if sid else 'n/a'}")
        print(f"  Status: {status.group(1) if status else 'n/a'}")
    else:
        print(f"  Response: {raw[:200]}")

    return raw


def main():
    # Resolve phone number
    if len(sys.argv) >= 2:
        to_number = sys.argv[1]
    elif TEST_CANDIDATE:
        to_number = TEST_CANDIDATE
        print(f"[INFO] Using TEST_CANDIDATE_NUMBER: {to_number}")
    else:
        print("[ERROR] Provide a phone number or set TEST_CANDIDATE_NUMBER in .env")
        print("Usage: python make_call.py +919876543210")
        sys.exit(1)

    # Resolve webhook URL
    if len(sys.argv) >= 3:
        webhook_url = sys.argv[2]
    elif SERVER_BASE_URL and "your-ngrok" not in SERVER_BASE_URL:
        webhook_url = SERVER_BASE_URL
    else:
        print("[ERROR] Set SERVER_BASE_URL in .env (your ngrok https:// URL)")
        print("  Example: SERVER_BASE_URL=https://abc123.ngrok.io")
        sys.exit(1)

    validate()
    make_call(to_number, webhook_url)


if __name__ == "__main__":
    main()
