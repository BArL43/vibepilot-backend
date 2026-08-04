# VibePilot API

Reference implementation функции **Budget Contract** для
[VibeMarketolog Agent API](https://lk.vibemarketolog.ru/docs/agent-api): агент
сначала фиксирует денежный конверт и допустимые действия, затем повторно
проверяет цену перед каждым списанием, умеет удешевить план и выпускает
криптографически проверяемую квитанцию исполнения.

Версия `0.3.0` — не макет генератора. Live-ветка вызывает реальный Agent API,
сохраняет `request_id`/`generation_id`, принимает подписанные webhook, сверяет
стоимость шагов с изменением баланса и переживает перезапуск процесса через SQL.

## Идея продукта

Agent API безопасно решает один платный вызов. VibePilot добавляет объект уровня
бизнес-задачи: «получить максимум результата, потратить не больше N ₽, оставить
резерв, спросить подтверждение при дорогом или неожиданно подорожавшем шаге».

`POST /api/v1/budget/compare` показывает контрфактические варианты для нескольких
бюджетов без выдуманного `quality score`. `POST /api/v1/contracts/compile`
фиксирует выбранный сценарий, каталог, estimates, policy и digest. Активация сама
ничего не списывает.

## Инварианты

- `demo` никогда автоматически не становится `live`, даже если токен настроен.
- Имена моделей берутся только из `GET /capabilities`.
- В live цена берётся из бесплатного `POST /generate/estimate`; прямо перед
  списанием проверяется финальное тело с `strict=true`.
- Порог подтверждения — исполняемое правило:
  `estimate > threshold OR price_drift > tolerance`.
- При нехватке конверта сначала пробуется более дешёвая совместимая модель, затем
  разрешённый `skip`; перерасход не используется как fallback.
- `actual_spend_rub` меняется только по `cost` ответа Agent API, refund хранится
  отдельно.
- Async image/video не получают `complete` без polling или webhook от платформы.
- До `POST /generate` сохраняются request fingerprint, состояние `running` и
  детерминированный `X-Idempotency-Key`.
- Live-маршруты дополнительно защищены `X-VibePilot-Live-Key`: посетитель
  публичного Render не может потратить серверный баланс.

## Два честных режима

| Режим | Основа цены | Сетевые генерации | Квитанция |
|---|---|---:|---|
| `demo` | `catalog_indicative` из публичного `/capabilities` | нет | `simulation`, расход `0` |
| `live` | `vibe_estimate` для точного payload | да | generation IDs и фактический cost |

Demo-сценарий нужен для интерфейса и безопасного знакомства. Он не называется
выполненной работой и не выдаёт каталожную цену за фактическое списание.

## Конвейер

1. Первая текстовая модель строит три концепции.
2. Другая доступная модель независимо оценивает их по прозрачной JSON-рубрике.
3. Победивший безопасный prompt передаётся image-модели.
4. Перед каждым шагом выполняется свежий estimate; при скачке цены применяется
   fallback или approval.
5. Баннер передаётся в корректное model-specific поле image-to-video.
6. Async-результат приходит через HMAC-SHA256 webhook; polling остаётся fallback.
7. SQL state machine фиксирует факты и сверяет сумму cost/refund с дельтой
   баланса.
8. Receipt v2 подписывается Ed25519 и проверяется офлайн с закреплённым публичным
   ключом deployment.

Подробности: [архитектура](docs/ARCHITECTURE.md),
[продуктовый RFC](docs/RFC-001-budget-contract.md),
[evals](docs/EVALS.md), [короткий маршрут ревью](docs/REVIEW_GUIDE.md).

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Swagger: `http://localhost:8000/docs`. Переменные `.env` нужно загрузить в
окружение до старта; сам файл игнорируется Git.

## Основные маршруты

- `POST /api/v1/budget/compare` — сравнить результат при разных конвертах.
- `POST /api/v1/contracts/compile` — создать immutable Budget Contract.
- `POST /api/v1/contracts/{id}/activate` — создать workflow без списания.
- `POST /api/v1/workflows/{id}/execute` — выполнить до async-шага или gate.
- `POST /api/v1/workflows/{id}/refresh` — polling fallback и продолжение.
- `POST /api/v1/workflows/{id}/approve` — решение человека.
- `POST /api/v1/workflows/{id}/reconcile` — повторная сверка с балансом.
- `POST /api/v1/webhooks/vibe` — HMAC-проверенный callback.
- `POST /api/v1/integrations/vibe/webhook-test` — бесплатный end-to-end self-test.
- `GET /api/v1/workflows/{id}/receipt` — подписанное доказательство исполнения.
- `POST /api/v1/receipts/verify` — проверка подписи без доступа к workflow.
- `GET /api/v1/receipts/public-key` — публичный ключ deployment.

Для операций live нужен заголовок:

```text
X-VibePilot-Live-Key: <VIBEPILOT_LIVE_KEY>
```

VibeMarketolog API token никогда не передаётся браузеру.

## Persistence и Render

SQLAlchemy использует SQLite по умолчанию и PostgreSQL через `DATABASE_URL` на
Render. Оптимистические ревизии не дают устаревшему worker перезаписать новое
состояние; отдельный индекс связывает `generation_id` с workflow/step.

В Render задаются:

- `VIBE_API_TOKEN` — ключ со scopes `read` и `generate`;
- `VIBEPILOT_LIVE_KEY` — независимый операторский секрет;
- `PUBLIC_BASE_URL` — публичный HTTPS URL backend без завершающего `/`;
- `VIBE_WEBHOOK_SECRET` — секрет подписи, выданный для Agent API key;
- `RECEIPT_SIGNING_KEY` — стабильный Ed25519 private key;
- `DATABASE_URL` — PostgreSQL URL (для production).

Без live-секретов сервис остаётся безопасным demo и сообщает об этом в
`/health`; он не имитирует сетевые вызовы. Пошаговый доказательный прогон:
[LIVE_RUN.md](LIVE_RUN.md).

## Проверка

```bash
ruff check app scripts tests
ruff format --check app scripts tests
pytest -q
```

Тесты проверяют fail-closed live, реальные политики threshold/drift, fallback
дорогой image-модели, scope reduction видео, idempotency, signed webhook и его
self-test, SQL persistence/concurrency, reconciliation, Ed25519 и обнаружение
подмены receipt.

## Честная граница доказательства

Код, контрактные тесты и бесплатный `/capabilities` можно проверить без
пользовательского токена. Единственный намеренно отсутствующий артефакт —
`evidence/live-receipt.json`: он появится только после настоящего дешёвого run.
Инструкция не предлагает подменять его демонстрационными данными.
