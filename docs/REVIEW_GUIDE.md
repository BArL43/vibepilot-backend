# Короткий маршрут ревью

VibePilot 0.3 отвечает не только на вопрос «как вызвать несколько моделей», а на
вопрос «как автономному агенту честно выполнить составную платную задачу в общем
денежном конверте».

## За пять минут

1. Откройте `POST /api/v1/budget/compare` в Swagger: один brief показывает, что
   реально помещается в 15/30/60/100 ₽.
2. Скомпилируйте `POST /api/v1/contracts/compile`: получите выбранный сценарий,
   alternatives, policy, catalog digest и contract digest.
3. Запустите `pytest -q`: контрактные тесты моделируют живые ответы API, скачок
   цены, fallback, webhook, concurrency, reconciliation и подмену receipt.
4. Посмотрите `docs/RFC-001-budget-contract.md`: это предложение функции для
   основной платформы, а код — её reference implementation на существующем API.
5. После live-run проверьте `evidence/live-receipt.json` командой
   `python scripts/verify_receipt.py ...`.

## Что проверяется кодом

| Риск | Механизм | Проверяемый след |
|---|---|---|
| Симуляция названа live | Явный mode + fail-closed secrets | status, `simulated`, generation IDs |
| Выдумана модель/цена | `/capabilities` + `/generate/estimate` | catalog digest, pricing basis, request ID |
| Порог декоративен | Числовой threshold + drift tolerance | approval reason и audit event |
| Старое approval разрешило новую цену | Привязка к estimate + payload fingerprint | invalidation до generate |
| Цена изменилась перед запуском | Estimate финального payload | initial/current estimate и drift |
| Шаг не входит в конверт | Cheaper payload → optional skip | `applied_fallback`, отсутствие generate call |
| Retry списал дважды | Persist-before-call + idempotency key | fingerprint, stable key, SQL revision |
| Async-работа выдумана | Signed webhook либо polling | generation/task ID и upstream status |
| Callback подделан | HMAC-SHA256 raw body | 401 без правильной подписи |
| Учёт разошёлся | Generation sum против balance delta | reconciliation status/difference |
| Квитанцию изменили/переподписали | Ed25519 + pinned deployment key | offline verification failure |

## Что добавлено сверх прямых замечаний

- Budget Contract как предлагаемый объект продукта, а не только локальный fix.
- Counterfactual planner без псевдонаучной оценки «качества».
- SQL state и optimistic concurrency вместо памяти процесса.
- Бесплатный официальный webhook self-test до первого платного вызова.
- Сверка двух независимых источников денежного факта.
- Стабильная deployment identity для receipt через Ed25519.
- JSON-rubric отдельной critic-модели и честные границы AI-eval.

## Честное ограничение

До настройки пользовательского Agent API key репозиторий доказывает логику,
протокол и инварианты, но не содержит придуманного live receipt. Единственный
ручной шаг — безопасно добавить секреты на Render и выполнить дешёвый smoke-run;
после него номера генераций и баланс можно независимо сопоставить с кабинетом.
