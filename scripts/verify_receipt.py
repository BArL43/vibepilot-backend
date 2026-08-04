#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.receipts import verify_signed_receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a VibePilot receipt and optionally pin signer identity."
    )
    parser.add_argument("receipt", help="Path to receipt JSON")
    parser.add_argument("--public-key", help="Expected Ed25519 public key (base64url)")
    parser.add_argument("--key-id", help="Expected deployment key id")
    parser.add_argument(
        "--public-key-file",
        help="JSON returned by GET /api/v1/receipts/public-key",
    )
    args = parser.parse_args()

    expected_public_key = args.public_key
    expected_key_id = args.key_id
    if args.public_key_file:
        key_document = json.loads(
            Path(args.public_key_file).read_text(encoding="utf-8")
        )
        expected_public_key = str(key_document["public_key_base64url"])
        expected_key_id = str(key_document["key_id"])

    receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    valid, message = verify_signed_receipt(
        receipt,
        expected_key_id=expected_key_id,
        expected_public_key_base64url=expected_public_key,
    )
    identity_pinned = bool(expected_public_key or expected_key_id)
    if valid and not identity_pinned:
        message += "; signer identity was not pinned"
    print(
        json.dumps(
            {
                "valid": valid,
                "identity_pinned": identity_pinned,
                "message": message,
            },
            ensure_ascii=False,
        )
    )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
