#!/usr/bin/env python3
"""Mock app-server that injects an invalid UTF-8 byte into a credit ID."""

from __future__ import annotations

import json
import sys
from pathlib import Path


MARKER = Path(sys.argv[1])


def read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


def send_message(message):
    sys.stdout.buffer.write(
        json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
    )
    sys.stdout.buffer.flush()


initialize = read_message()
if initialize is None:
    raise SystemExit(0)
send_message({"id": initialize["id"], "result": {}})
read_message()  # initialized notification

rate_request = read_message()
if rate_request is None:
    raise SystemExit(0)
invalid_response = (
    b'{"id":'
    + str(rate_request["id"]).encode("ascii")
    + b',"result":{"rateLimits":{},"rateLimitResetCredits":'
      b'{"availableCount":1,"credits":[{"id":"bad\xffid","status":"available",'
      b'"resetType":"codexRateLimits","grantedAt":1799999000,'
      b'"expiresAt":1800001800,"title":"Full reset"}]}}}\n'
)
sys.stdout.buffer.write(invalid_response)
sys.stdout.buffer.flush()

consume_request = read_message()
if consume_request is None:
    raise SystemExit(0)
if consume_request.get("method") == "account/rateLimitResetCredit/consume":
    MARKER.write_text("consume-sent", encoding="utf-8")
    send_message({"id": consume_request["id"], "result": {"outcome": "reset"}})

verification_request = read_message()
if verification_request is not None:
    send_message(
        {
            "id": verification_request["id"],
            "result": {
                "rateLimits": {},
                "rateLimitResetCredits": {"availableCount": 0, "credits": []},
            },
        }
    )