# Live evidence

Здесь намеренно нет нарисованного примера «боевого» запуска. Файл
`live-receipt.json` появится только после настоящего Agent API run и должен иметь
`verification: verified`, реальные `generation_id`, фактический `cost` и
валидную Ed25519 signature.

Перед платным шагом `scripts/live_smoke.py` также требует успешный официальный
`POST /webhook-test`, поэтому evidence подтверждает не только polling, но и
доступность публичного HMAC listener.

Команда запуска описана в `LIVE_RUN.md`. Проверка после появления файла:

```bash
python scripts/verify_receipt.py evidence/live-receipt.json \
  --public-key-file evidence/receipt-public-key.json
```

Проверка без `--public-key-file` доказывает целостность, но не личность
подписанта; скрипт честно пометит её `identity_pinned: false`.
