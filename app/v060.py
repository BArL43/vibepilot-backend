from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.models import CampaignRequest, Step, Workflow
from app.planner import build_steps as build_steps_v050

TEXT_SAFE_RULE = (
    "ВАЖНО: генерируй только чистый визуал. НЕ РИСУЙ текст, буквы, цифры, "
    "логотипы, водяные знаки, вывески и псевдотипографику внутри изображения. "
    "Оставь спокойную свободную зону под детерминированный текстовый overlay."
)

VIDEO_TEXT_SAFE_RULE = (
    "Не добавляй в кадр читаемый текст, буквы, цифры, титры, логотипы, "
    "вывески или псевдотипографику: точный русский copy будет наложен "
    "VibePilot отдельным детерминированным слоем."
)


def build_steps_v060(catalog: dict[str, Any], request: CampaignRequest) -> list[Step]:
    """Keep v0.5 budget guarantees and make generated media text-safe."""
    steps = build_steps_v050(catalog, request)

    selection = next((item for item in steps if item.id == "selection"), None)
    if selection is not None:
        selection.request_payload["system"] = (
            str(selection.request_payload.get("system", ""))
            + " Верни также поле copy: {headline, subheadline, offer, cta}. "
            + "Все четыре значения должны быть написаны по-русски без markdown. "
            + "headline — короткий рекламный заголовок; subheadline — пояснение; "
            + "offer — конкретный оффер без выдуманных фактов; cta — действие. "
            + "banner_prompt должен описывать ТОЛЬКО визуальную сцену без текста, "
            + "букв, цифр, логотипов и вывесок."
        )
        selection.request_payload["prompt"] = (
            str(selection.request_payload.get("prompt", ""))
            + "\n\nОтдельно подготовь точный русский copy для overlay. Сам visual "
            + "должен оставаться без текста: типографику VibePilot добавит после "
            + "генерации."
        )

    banner = next((item for item in steps if item.id == "banner"), None)
    if banner is not None:
        banner.request_payload["prompt"] = (
            "Создай премиальный рекламный visual 1:1 с сильным hero-shot, "
            "естественным светом, аккуратной композицией и заметной свободной "
            "зоной слева или сверху под будущий текст. "
            f"{TEXT_SAFE_RULE} Бизнес-бриф: {request.brief}"
        )
        safe_fallbacks: list[dict[str, Any]] = []
        for payload in banner.fallback_payloads:
            candidate = deepcopy(payload)
            if candidate.get("_fallback_action") != "skip":
                candidate["prompt"] = (
                    "Создай альтернативный рекламный visual 1:1 с чистой "
                    "композицией и свободной зоной под overlay. "
                    f"{TEXT_SAFE_RULE} Бизнес-бриф: {request.brief}"
                )
            safe_fallbacks.append(candidate)
        banner.fallback_payloads = safe_fallbacks
        banner.reason = (
            "Модель создаёт только visual; точный русский текст рендерится "
            "VibePilot отдельным детерминированным слоем."
        )

    video = next((item for item in steps if item.id == "video"), None)
    if video is not None:
        video.request_payload["prompt"] = (
            "Создай законченный короткий рекламный ролик по визуальной концепции: "
            "плавное движение камеры, понятное действие и чистый финальный кадр, "
            "без визуального шума. "
            f"{VIDEO_TEXT_SAFE_RULE} Бизнес-бриф: {request.brief}"
        )
        video.reason = (
            "Видео сохраняет визуальную концепцию без встроенной типографики; "
            "русский copy показывается точным overlay-слоем VibePilot."
        )

    return steps


def creative_package(workflow: Workflow) -> dict[str, Any]:
    selection = next((item for item in workflow.steps if item.id == "selection"), None)
    banner = next((item for item in workflow.steps if item.id == "banner"), None)
    video = next((item for item in workflow.steps if item.id == "video"), None)
    bonus = next((item for item in workflow.steps if item.id == "bonus_banner"), None)

    structured = selection.structured_result if selection else None
    structured = structured if isinstance(structured, dict) else {}
    raw_copy = structured.get("copy")
    raw_copy = raw_copy if isinstance(raw_copy, dict) else {}

    copy = {
        "headline": _clean_copy(raw_copy.get("headline"), "Главная идея кампании"),
        "subheadline": _clean_copy(raw_copy.get("subheadline"), workflow.brief[:120]),
        "offer": _clean_copy(raw_copy.get("offer"), "Предложение по брифу"),
        "cta": _clean_copy(raw_copy.get("cta"), "Узнать больше"),
    }

    return {
        "workflow_id": workflow.id,
        "text_safe": True,
        "rendering": "deterministic_browser_overlay",
        "copy": copy,
        "banner_url": banner.result_url if banner else None,
        "video_url": video.result_url if video else None,
        "bonus_banner_url": bonus.result_url if bonus else None,
        "note": (
            "Генеративная модель отвечает за visual. Русский copy выводится "
            "отдельным HTML/CSS overlay без риска псевдокириллицы."
        ),
    }


def booster_state(workflow: Workflow) -> dict[str, Any]:
    banner = next((item for item in workflow.steps if item.id == "banner"), None)
    existing = next(
        (item for item in workflow.steps if item.id == "bonus_banner"),
        None,
    )
    available = round(max(0, workflow.remaining_budget_rub - workflow.reserve_rub), 2)
    estimated = round(float(banner.estimated_cost_rub if banner else 0), 2)
    eligible = bool(
        workflow.status == "complete"
        and banner
        and banner.result_url
        and existing is None
        and estimated > 0
        and estimated <= available
    )
    reason = (
        f"Можно сделать A/B-вариант примерно за {estimated:.2f} ₽ и оставить резерв."
        if eligible
        else "Дополнительный креатив сейчас не помещается в безопасный остаток."
    )
    if existing is not None:
        reason = "Дополнительный A/B-вариант уже добавлен в workflow."
    elif workflow.status != "complete":
        reason = "Budget Booster доступен после завершения основной кампании."

    return {
        "eligible": eligible,
        "available_rub": available,
        "estimated_extra_rub": estimated,
        "reserve_rub": workflow.reserve_rub,
        "action": "generate_ab_banner" if eligible else None,
        "reason": reason,
    }


def bonus_banner_from(workflow: Workflow) -> Step:
    source = next(item for item in workflow.steps if item.id == "banner")
    bonus = deepcopy(source)
    bonus.id = "bonus_banner"
    bonus.title = "A/B-вариант баннера"
    bonus.purpose = "Дополнительная визуальная гипотеза из свободного бюджета"
    bonus.status = "planned"
    bonus.reason = (
        "Budget Booster использует только свободную часть конверта после "
        "основного результата и сохраняет safety reserve."
    )
    bonus.actual_cost_rub = 0
    bonus.refunded_rub = 0
    bonus.generation_id = None
    bonus.task_id = None
    bonus.upstream_request_id = None
    bonus.result_url = None
    bonus.result_urls = []
    bonus.text_result = None
    bonus.structured_result = None
    bonus.error_code = None
    bonus.error_message = None
    bonus.simulated = False
    bonus.requires_approval = False
    bonus.approval_reason = None
    bonus.approved_at = None
    bonus.approval_note = None
    bonus.approved_estimate_rub = None
    bonus.approved_request_fingerprint = None
    bonus.request_fingerprint = None
    bonus.idempotency_key = None
    bonus.estimate_snapshot = None
    bonus.estimate_drift_rub = 0
    bonus.applied_fallback = None
    bonus.request_payload = deepcopy(source.request_payload)
    bonus.request_payload.pop("callback_url", None)
    bonus.request_payload["prompt"] = (
        "Создай заметно отличающийся вариант B той же рекламной кампании: "
        "другая композиция, ракурс или визуальная метафора, но тот же уровень "
        "бренда и тот же продуктовый смысл. "
        f"{TEXT_SAFE_RULE} Бизнес-бриф: {workflow.brief}"
    )
    return bonus


def _clean_copy(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback.strip()
    cleaned = " ".join(value.split()).strip()
    return cleaned[:180] or fallback.strip()
