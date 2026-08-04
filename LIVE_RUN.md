# Контрольный live-прогон

Цель — получить недостающий проверяемый артефакт: настоящий Budget Contract,
подтверждённую доставку webhook, реальные `generation_id`, фактическое списание,
сверку баланса и Ed25519-signed receipt.

## 1. Ограничить риск в VibeMarketolog

Создайте отдельный Agent API key со scopes `read` и `generate` и небольшим
дневным лимитом, например 60 ₽. Сохраните показанный один раз `webhook_secret`.
Не публикуйте оба значения и не добавляйте их в Git.

## 2. Настроить Render

Создайте стабильный ключ подписи локально:

```bash
python scripts/generate_receipt_key.py
```

В Render Environment добавьте:

```text
VIBE_API_TOKEN=<ключ oc_...>
VIBEPILOT_LIVE_KEY=<отдельная случайная строка>
PUBLIC_BASE_URL=https://<render-service>.onrender.com
VIBE_WEBHOOK_SECRET=<webhook_secret этого API key>
RECEIPT_SIGNING_KEY=<вывод generate_receipt_key.py>
DATABASE_URL=<PostgreSQL Internal Database URL>
```

После redeploy проверьте:

```bash
curl -s https://<render-service>.onrender.com/health
```

Ожидаются `token_configured: true`, `live_control_configured: true`,
`webhook_configured: true`, `receipt_signing: "configured"` и
`state_store: "postgresql"`.

## 3. Выполнить минимальный доказательный run

На своём компьютере:

```bash
export VIBEPILOT_LIVE_KEY='<операторский секрет Render>'
export VIBEPILOT_API_URL='https://<render-service>.onrender.com'
python scripts/live_smoke.py --budget 20
```

Скрипт последовательно:

1. вызывает бесплатный `/webhook-test` VibeMarketolog и требует успешную
   HMAC-доставку обратно на Render;
2. компилирует live Budget Contract на точных `/generate/estimate`;
3. активирует его без списания;
4. запускает две текстовые операции и дешёвый image-шаг;
5. ждёт webhook либо использует polling fallback;
6. получает receipt и проверяет Ed25519-подпись;
7. сохраняет файл в `evidence/live-receipt.json`.

Видео по умолчанию выключено. Полный конвейер запускается только осознанно:

```bash
python scripts/live_smoke.py --budget 100 --include-video --approve
```

Без `--approve` скрипт остановится перед gate с кодом 2. Если временно не готов
публичный callback, допустим диагностический `--skip-webhook-test`, но для
финальной досылки лучше показать полноценный signed webhook.

## 4. Независимая проверка

```bash
python scripts/verify_receipt.py evidence/live-receipt.json \
  --public-key-file evidence/receipt-public-key.json
```

В receipt должны быть:

- `verification: "verified"`;
- `contract_id`, `contract_digest` и `catalog_digest`;
- реальные `generation_id` выполненных шагов;
- initial/runtime estimates, price drift и applied fallback;
- `actual_charged_rub`, refund и net spend;
- `reconciliation.status: "matched"` либо честно объяснимый `mismatch`, если в
  том же аккаунте параллельно были другие списания;
- upstream request IDs;
- валидная Ed25519 signature с `key_persistence: "configured"`.
- `identity_pinned: true` при офлайн-проверке отдельным public-key document.

Receipt не содержит VIBE-токен, webhook secret или операторский ключ. Перед
публикацией можно удалить временные result URL, но после этого исходная подпись
станет невалидной. Поэтому лучше публиковать файл целиком: подписанные ссылки
VibeMarketolog ограничены по времени, а секретов в них нет.
