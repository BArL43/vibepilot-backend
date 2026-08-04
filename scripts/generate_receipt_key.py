#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.receipts import ReceiptSigner

if __name__ == "__main__":
    print(ReceiptSigner.generate_private_key())
