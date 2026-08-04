# RFC-001: Budget Contract для платных действий ИИ-агента

- Статус: reference implementation
- Автор прототипа: Артём / VibePilot
- Версия: 2026-08-04

## Резюме

Предлагается добавить над поштучными платными вызовами Agent API объект
`Budget Contract`: неизменяемый план задачи с общим денежным конвертом,
предсписательными условиями, approval-policy, резервом и fallback-графом.

Контракт не заменяет `/generate`, `/generate/estimate` и `/capabilities`. Он
компилируется в существующие вызовы и даёт агенту транзакционную семантику:
сначала доказать допустимость плана, затем тратить, после каждого результата
сверять факт и в конце выпустить квитанцию.

## Проблема

Сейчас интегратор может безопасно выполнить один вызов:

1. получить каталог;
2. вызвать estimate;
3. вызвать generate;
4. дождаться результата.

Но бизнес-задача обычно состоит из нескольких зависимых шагов. Каждый агент
заново реализует:

- общий лимит, а не лимит одного вызова;
- обязательную и максимальную смету;
- резерв для токенов и дрейфа цены;
- подтверждение дорогих или неожиданно подорожавших операций;
- снижение качества/объёма вместо аварийной остановки;
- reconciliation факта, возвратов и незавершённых генераций;
- доказательство того, какие платные действия действительно выполнялись.

Ошибка в этом слое опаснее обычной ошибки генерации: агент может честно вернуть
картинку, но нарушить общий бюджет, либо сообщить о результате, которого не было.

## Цели

1. Никогда не превысить `max_total_rub` по локально подтверждённым фактам.
2. Не запускать тело запроса, отличающееся от прошедшего estimate, без новой
   проверки.
3. Остановиться перед списанием при недопустимом price drift.
4. Уметь деградировать план до более дешёвого совместимого варианта.
5. Переживать retry, webhook redelivery и перезапуск исполнителя.
6. Выпускать проверяемый receipt с request/generation IDs и реальными ценами.

## Не-цели

- Не вводится выдуманный универсальный `quality_score` моделей.
- Контракт не обещает маркетинговый результат или конверсию.
- Контракт не заменяет дневные лимиты и scopes самого Agent API.
- Первая версия не делает распределённый escrow денежных средств.

## Предлагаемый интерфейс

### Компиляция

```http
POST /api/v1/contracts/compile
```

```json
{
  "brief": "Кофейня запускает утреннее комбо",
  "budgets_rub": [60],
  "execution_mode": "live",
  "approval_required_above_rub": 10,
  "reserve_rub": 5,
  "price_drift_tolerance_rub": 2,
  "include_video": true
}
```

Ответ содержит:

- выбранный feasible scenario;
- отвергнутые альтернативы и причину;
- точные модели и estimate каждого шага;
- digest каталога;
- digest всего контракта;
- fallback-policy.

### Активация

```http
POST /api/v1/contracts/{id}/activate
```

Активация создаёт workflow, но сама по себе не списывает деньги. Если digest
текущего каталога отличается, все approval становятся условными, а runtime
обязан повторить estimate.

### Исполнение

Исполнитель применяет цикл:

```text
prepare dependent payload
  → strict estimate
  → compare with contract + remaining envelope
  → fallback or approval when required
  → persist idempotency key and RUNNING state
  → generate
  → signed webhook / polling reconciliation
  → persist actual cost and result
```

## Политика принятия решений

Approval требуется, когда выполняется хотя бы одно условие:

```text
fresh_estimate > approval_above_rub
price_drift > price_drift_tolerance_rub
```

Approval привязан к fingerprint финального payload и одобренной смете. Если до
`generate` тело изменилось либо цена выросла сверх tolerance, старое разрешение
аннулируется и требуется новое; одного когда-либо заполненного `approved_at`
недостаточно.

Допустимый расход шага:

```text
fresh_estimate <= budget - net_actual_spend - reserve
```

При нарушении сначала проверяются более дешёвые совместимые payloads. Пропуск
разрешён только для шага, помеченного fallback `skip`.

## Counterfactual Planner

`POST /api/v1/budget/compare` отвечает на вопрос, что изменится при разных
конвертах. Выбор выполняется лексикографически:

1. максимальное число deliverables;
2. максимальный запрошенный tier `economy/balanced/quality`;
3. минимальный расход при одинаковом составе.

Такой порядок проверяем и объясним. Он не выдаёт цену модели за объективную
оценку качества.

Каждый сценарий явно указывает `pricing_basis`:

- `catalog_indicative` — безопасная симуляция без токена и без списаний;
- `vibe_estimate` — live-смета конкретного payload через бесплатную расчётную
  ручку.

Таким образом, каталожный ориентир не маскируется под фактическую цену.

## Состояния и идемпотентность

До сетевого вызова сохраняются:

- финальный request fingerprint;
- детерминированный idempotency key;
- состояние `running`;
- ревизия workflow.

Повторный процесс видит `running` и не запускает новый запрос. Если два процесса
всё же конкурируют, SQL compare-and-swap отклоняет устаревшую ревизию, а
idempotency Agent API защищает от повторного списания.

## Webhook и reconciliation

Webhook проверяется по raw body через HMAC-SHA256 и отдельный
`VIBE_WEBHOOK_SECRET`. `generation_id` индексируется в БД, поэтому callback не
доверяет переданному workflow ID. Повторное событие `complete` идемпотентно.

Polling остаётся fallback-механизмом для потерянного webhook или старого ключа.

После завершения исполнитель дополнительно получает текущий `/balance` и
сравнивает:

```text
balance_before - balance_after
    versus
sum(generation.cost) - sum(refund)
```

Результат `matched | mismatch | unavailable` попадает в workflow, audit и
receipt. `mismatch` не переписывается «правильной» цифрой: он может означать баг
учёта либо параллельное списание с того же аккаунта и требует разбора.

## Receipt

Receipt v2 содержит:

- contract/catalog digests;
- initial и runtime estimates, drift;
- фактические списания и возвраты;
- request IDs, generation IDs, task IDs;
- approval и применённые fallbacks;
- результат структурированного critic-eval;
- SHA-256 checksum;
- Ed25519 signature и публичный ключ.

SHA-256 помогает быстро проверить файл, но сам по себе не доказывает
неизменность: его можно пересчитать. Поэтому v2 подписывает всё тело Ed25519.
Проверка выполняется локально. Для доказательства личности подписанта verifier
закрепляет отдельный public-key document deployment; одного ключа, вложенного в
тот же receipt, достаточно только для tamper detection.

## Хранилище

Reference implementation использует SQLAlchemy:

- SQLite — zero-config разработка;
- PostgreSQL через `DATABASE_URL` — Render/production;
- optimistic revision — обнаружение stale update;
- отдельный index `generation_id → workflow + step`.

## Безопасность

- VIBE API token остаётся только на сервере.
- Публичное demo никогда автоматически не становится live.
- Live-маршруты требуют отдельный `VIBEPILOT_LIVE_KEY`.
- Webhook secret не является API-токеном и используется только для HMAC.
- Баланс redacted для запроса без операторского ключа.
- Receipt signing key отделён от обоих ключей.

## Совместимость и путь в продукт

Контракт реализован поверх существующего API, поэтому его можно внедрять
поэтапно:

1. SDK/middleware для внешних агентов;
2. объект `task` в кабинете;
3. нативные `/tasks/plan`, `/tasks/{id}/commit`, `/tasks/{id}/receipt`;
4. опциональный серверный reserve/escrow для строгого общего лимита.

На первом этапе платформе не нужно менять биллинг: reference implementation уже
показывает семантику на текущих endpoint.

## Метрики успеха

- доля задач, завершённых без превышения конверта;
- сумма предотвращённых списаний после повторного estimate;
- доля задач, спасённых fallback вместо остановки;
- число ручных approval на задачу;
- estimate-to-actual variance;
- webhook completion latency;
- число idempotent replays и revision conflicts.
