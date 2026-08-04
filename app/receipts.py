from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class ReceiptSigner:
    def __init__(self, encoded_private_key: str = "") -> None:
        if encoded_private_key:
            private_bytes = _b64decode(encoded_private_key)
            if len(private_bytes) != 32:
                raise ValueError("RECEIPT_SIGNING_KEY must encode exactly 32 bytes")
            self.private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
            self.ephemeral = False
        else:
            self.private_key = Ed25519PrivateKey.generate()
            self.ephemeral = True
        self.public_bytes = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.key_id = hashlib.sha256(self.public_bytes).hexdigest()[:16]

    def sign(self, body: dict[str, Any]) -> dict[str, Any]:
        signature = self.private_key.sign(_canonical(body))
        return {
            "algorithm": "Ed25519",
            "key_id": self.key_id,
            "public_key_base64url": _b64encode(self.public_bytes),
            "signature_base64url": _b64encode(signature),
            "canonicalization": "RFC8785-inspired sorted UTF-8 JSON",
            "key_persistence": "ephemeral" if self.ephemeral else "configured",
        }

    def public_document(self) -> dict[str, Any]:
        return {
            "algorithm": "Ed25519",
            "key_id": self.key_id,
            "public_key_base64url": _b64encode(self.public_bytes),
            "ephemeral": self.ephemeral,
            "note": (
                "Configure RECEIPT_SIGNING_KEY for a stable deployment identity."
                if self.ephemeral
                else "Stable deployment key loaded from RECEIPT_SIGNING_KEY."
            ),
        }

    @staticmethod
    def generate_private_key() -> str:
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return _b64encode(raw)


def verify_signed_receipt(
    receipt: dict[str, Any],
    *,
    expected_key_id: str | None = None,
    expected_public_key_base64url: str | None = None,
) -> tuple[bool, str]:
    signature = receipt.get("signature")
    if not isinstance(signature, dict):
        return False, "signature field is missing"
    if signature.get("algorithm") != "Ed25519":
        return False, "unsupported signature algorithm"
    embedded_key_id = str(signature.get("key_id", ""))
    embedded_public_key = str(signature.get("public_key_base64url", ""))
    if expected_key_id is not None and embedded_key_id != expected_key_id:
        return False, "receipt was signed by an unexpected key id"
    if (
        expected_public_key_base64url is not None
        and embedded_public_key != expected_public_key_base64url
    ):
        return False, "receipt public key does not match the pinned deployment key"
    try:
        public_key = Ed25519PublicKey.from_public_bytes(_b64decode(embedded_public_key))
        signature_bytes = _b64decode(str(signature["signature_base64url"]))
    except (KeyError, TypeError, ValueError) as exc:
        return False, f"malformed signature: {type(exc).__name__}"
    unsigned = deepcopy(receipt)
    unsigned.pop("signature", None)
    try:
        public_key.verify(signature_bytes, _canonical(unsigned))
    except InvalidSignature:
        return False, "signature does not match receipt content"
    return True, "signature is valid"
