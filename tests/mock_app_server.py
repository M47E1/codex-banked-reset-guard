from __future__ import annotations

import json
import sys
import time


credit_id = "RateLimitResetCredit_mock-only"
consumed = False


def send(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        send(
            {
                "id": request_id,
                "result": {
                    "codexHome": "/mock",
                    "platformFamily": "unix",
                    "platformOs": "linux",
                    "userAgent": "mock",
                },
            }
        )
    elif method == "initialized":
        continue
    elif method == "account/rateLimits/read":
        credits = []
        if not consumed:
            credits.append(
                {
                    "id": credit_id,
                    "status": "available",
                    "resetType": "codexRateLimits",
                    "grantedAt": int(time.time()) - 60,
                    "expiresAt": int(time.time()) + 3600,
                    "title": "Mock reset",
                    "description": "Mock only",
                }
            )
        send(
            {
                "id": request_id,
                "result": {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": 50,
                            "windowDurationMins": 300,
                            "resetsAt": int(time.time()) + 3000,
                        },
                        "secondary": None,
                        "rateLimitReachedType": None,
                    },
                    "rateLimitResetCredits": {
                        "availableCount": 0 if consumed else 1,
                        "credits": credits,
                    },
                },
            }
        )
    elif method == "account/rateLimitResetCredit/consume":
        params = request.get("params") or {}
        if params.get("creditId") != credit_id or not params.get("idempotencyKey"):
            send(
                {
                    "id": request_id,
                    "error": {"code": -32602, "message": "invalid params"},
                }
            )
        else:
            consumed = True
            send({"id": request_id, "result": {"outcome": "reset"}})
    else:
        send({"id": request_id, "error": {"code": -32601, "message": "not found"}})
