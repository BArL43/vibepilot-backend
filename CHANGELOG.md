# Changelog

## 0.6.2

Current product layer on top of the v0.5 execution engine:

- production entrypoint moved to `app.server:app`;
- added the operator frontend and canonical root route;
- added persistent campaign history for an authorized live operator;
- added text-safe media prompts and deterministic Russian copy overlays;
- added Budget Booster for an extra A/B banner that can use only the safe free part of a completed workflow budget;
- kept v0.5 budget, estimate, approval, idempotency, reconciliation and signed-receipt guarantees intact;
- added regression coverage for the v0.6 planner and campaign history.

## 0.3.1

- Добавлена адаптивная реакция на лимит prompt, который возвращает живой
  `/generate/estimate`: VibePilot извлекает допустимую длину из ответа API,
  сохраняет бриф и выбранную концепцию, сокращает служебную рубрику и бесплатно
  перепроверяет запрос до списания.
- Обрезанный или невалидный JSON модели-критика больше не прокидывается целиком
  в генератор изображения.
- Ожидаемый отказ runtime-валидации возвращается как структурированный `422`,
  а не маскируется под внутренний `500`; ответ явно фиксирует нулевую стоимость
  отклонённого действия.
- Добавлены регрессионные тесты на оба поведения; полный набор — 24 теста.

## 0.3.0

Продуктовый слой сверх исправлений 0.2:

- добавлен `Budget Contract`: неизменяемый конверт, policy, digest каталога и
  контракта, выбранный сценарий и проверяемые альтернативы;
- counterfactual planner сравнивает несколько бюджетов по deliverables/tier/цене
  без выдуманного `quality score`;
- state перенесён из памяти в SQLite/PostgreSQL, добавлены optimistic revisions
  и индекс `generation_id → workflow + step`;
- async-результаты принимаются через официальный HMAC-SHA256 webhook по raw body;
  повторная доставка идемпотентна, polling сохранён как fallback;
- добавлен бесплатный end-to-end `/webhook-test` до платного smoke-run;
- runtime price drift стал самостоятельным approval-условием;
- approval привязан к конкретным смете и payload fingerprint и аннулируется при
  существенном изменении до `generate`;
- дорогая image-модель автоматически заменяется дешёвой совместимой до списания,
  а необязательное видео действительно исключается из плана, а не только
  описывается как fallback;
- фактические generation costs/refunds сверяются с изменением баланса; mismatch
  фиксируется, но не скрывается;
- receipt v2 подписывается Ed25519 и проверяется офлайн;
- critic возвращает объяснимую JSON-рубрику, его structured result становится
  входом баннера и частью receipt;
- добавлены RFC, архитектура, eval-документ и доказательный live-run workflow.

## 0.2.0

Исправления по обратной связи:

- удалено автоматическое и ложное определение live по факту наличия токена;
- live-план и live-execution теперь действительно вызывают Agent API;
- async-шаг получает `complete` только после подтверждения generation status;
- модели выбираются из `/capabilities`, стоимость live-запросов — из
  `/generate/estimate`;
- повторный estimate выполняется непосредственно перед списанием;
- approval gate вычисляется из числового threshold, а не из флага конкретного
  video-шага;
- бюджет меняется только по фактическому `cost`; ошибка до принятия запроса
  фиксируется с `charged_rub: 0`;
- добавлены strict mode, idempotency keys, request IDs и refund accounting.

Добавлено сверх замечаний:

- криптографически хешируемый Live Run Receipt для воспроизводимой проверки;
- отдельный `VIBEPILOT_LIVE_KEY`, чтобы публичный Render не позволял посетителям
  тратить баланс;
- операторский smoke-run с безопасной остановкой перед approval;
- динамическая передача результата banner в image-to-video payload;
- fail-closed catalog/estimate validation и автоматическое снижение приоритета
  до economy до первого списания.
