#!/usr/bin/env python3
"""Operator-controlled proof run against a deployed VibePilot backend."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

TERMINAL = {"complete", "cancelled", "blocked", "error"}


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    live_key: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.request(
        method,
        path,
        json=body,
        headers={"X-VibePilot-Live-Key": live_key},
    )
    if not response.is_success:
        print(response.text, file=sys.stderr)
        response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("Backend returned a non-object response")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a cheap, verifiable VibeMarketolog workflow and print its receipt."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("VIBEPILOT_API_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--budget", type=float, default=20)
    parser.add_argument("--include-video", action="store_true")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Approve a threshold gate. Without this flag the script stops safely.",
    )
    parser.add_argument("--poll-seconds", type=float, default=12)
    parser.add_argument("--max-polls", type=int, default=160)
    parser.add_argument(
        "--skip-webhook-test",
        action="store_true",
        help="Skip the free signed webhook delivery self-test.",
    )
    parser.add_argument(
        "--receipt-out",
        default="evidence/live-receipt.json",
        help="Where to save the verified receipt JSON.",
    )
    parser.add_argument(
        "--public-key-out",
        default="evidence/receipt-public-key.json",
        help="Where to save the deployment public-key document.",
    )
    parser.add_argument(
        "--brief",
        default=(
            "Кофейня в Казани запускает утреннее комбо для офисных сотрудников: "
            "кофе и выпечка по будням с 08:00 до 11:00."
        ),
    )
    args = parser.parse_args()

    live_key = os.getenv("VIBEPILOT_LIVE_KEY", "").strip()
    if not live_key:
        parser.error("Set VIBEPILOT_LIVE_KEY in the operator environment")

    base_url = args.base_url.rstrip("/")
    with httpx.Client(base_url=base_url, timeout=150) as client:
        if not args.skip_webhook_test:
            webhook_test = _request(
                client,
                "POST",
                "/api/v1/integrations/vibe/webhook-test",
                live_key=live_key,
            )
            if webhook_test.get("delivered") is not True:
                raise RuntimeError(f"Signed webhook self-test failed: {webhook_test}")
            print(
                json.dumps(
                    {
                        "event": "webhook_test_passed",
                        "http_status": webhook_test.get("http_status"),
                        "request_id": webhook_test.get("request_id"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        contract = _request(
            client,
            "POST",
            "/api/v1/contracts/compile",
            live_key=live_key,
            body={
                "brief": args.brief,
                "budgets_rub": [args.budget],
                "execution_mode": "live",
                "approval_required_above_rub": 10,
                "include_video": args.include_video,
                "reserve_rub": 5,
                "price_drift_tolerance_rub": 2,
            },
        )
        workflow = _request(
            client,
            "POST",
            f"/api/v1/contracts/{contract['id']}/activate",
            live_key=live_key,
        )
        workflow_id = workflow["id"]
        print(
            json.dumps(
                {
                    "event": "planned",
                    "contract_id": contract["id"],
                    "contract_digest": contract["digest"],
                    "workflow_id": workflow_id,
                    "required_cost_rub": workflow["required_cost_rub"],
                    "maximum_cost_rub": workflow["maximum_cost_rub"],
                    "models": [
                        step["model"]
                        for step in workflow["steps"]
                        if step["kind"] == "generation"
                    ],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        workflow = _request(
            client,
            "POST",
            f"/api/v1/workflows/{workflow_id}/execute",
            live_key=live_key,
        )
        for poll_number in range(1, args.max_polls + 1):
            if workflow["status"] == "awaiting_approval":
                waiting = next(
                    step
                    for step in workflow["steps"]
                    if step["status"] == "awaiting_approval"
                )
                print(
                    json.dumps(
                        {
                            "event": "awaiting_approval",
                            "step": waiting["id"],
                            "fresh_estimate_rub": waiting["estimated_cost_rub"],
                            "reason": waiting["approval_reason"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if not args.approve:
                    print(
                        "Stopped before approval; rerun with --approve after review.",
                        file=sys.stderr,
                    )
                    return 2
                workflow = _request(
                    client,
                    "POST",
                    f"/api/v1/workflows/{workflow_id}/approve",
                    live_key=live_key,
                    body={"approved": True, "note": "Operator-approved smoke run"},
                )
                continue

            if workflow["status"] in TERMINAL:
                break
            running = next(
                (step for step in workflow["steps"] if step["status"] == "running"),
                None,
            )
            print(
                json.dumps(
                    {
                        "event": "poll",
                        "number": poll_number,
                        "step": running["id"] if running else None,
                        "generation_id": running["generation_id"] if running else None,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            time.sleep(max(1, args.poll_seconds))
            workflow = _request(
                client,
                "POST",
                f"/api/v1/workflows/{workflow_id}/refresh",
                live_key=live_key,
            )
        else:
            raise RuntimeError("Polling limit exceeded")

        receipt = _request(
            client,
            "GET",
            f"/api/v1/workflows/{workflow_id}/receipt",
            live_key=live_key,
        )
        verification = _request(
            client,
            "POST",
            "/api/v1/receipts/verify",
            live_key=live_key,
            body=receipt,
        )
        if not verification.get("valid"):
            raise RuntimeError(f"Receipt signature failed: {verification}")
        public_key = _request(
            client,
            "GET",
            "/api/v1/receipts/public-key",
            live_key=live_key,
        )
        receipt_path = Path(args.receipt_out)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        public_key_path = Path(args.public_key_out)
        public_key_path.parent.mkdir(parents=True, exist_ok=True)
        public_key_path.write_text(
            json.dumps(public_key, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Verified receipt saved to {receipt_path}", file=sys.stderr)
        print(f"Pinned public key saved to {public_key_path}", file=sys.stderr)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0 if receipt["verification"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
