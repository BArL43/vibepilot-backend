from app.models import CampaignRequest
from app.v060 import TEXT_SAFE_RULE, build_steps_v060

CATALOG = {
    "text_models": {
        "models": {
            "claude-opus-5": {"sample_cost_rub": 3.75},
            "gpt-5.6-sol": {"sample_cost_rub": 4.2},
        }
    },
    "models": {
        "text": {},
        "image": {
            "z-image": {"price": 1.2, "required": ["prompt"]},
            "nano-banana-2-lite": {"price": 9, "required": ["prompt"]},
            "seedream-5-pro": {"price": 40, "required": ["prompt"]},
        },
        "video": {
            "pixverse-v6": {"price": 40, "required": ["prompt"]},
        },
    },
}


def request(priority: str = "quality") -> CampaignRequest:
    return CampaignRequest(
        brief="Кофейня: утренняя рекламная кампания для молодых специалистов.",
        budget_rub=200,
        priority=priority,
        include_video=True,
    )


def test_v060_preserves_balanced_model_contract() -> None:
    steps = build_steps_v060(CATALOG, request("balanced"))
    models = [item.model for item in steps if item.kind == "generation"]
    assert models == [
        "claude-opus-5",
        "gpt-5.6-sol",
        "nano-banana-2-lite",
        "pixverse-v6",
    ]


def test_v060_requires_exact_copy_and_text_safe_visuals() -> None:
    steps = build_steps_v060(CATALOG, request())
    selection = next(item for item in steps if item.id == "selection")
    banner = next(item for item in steps if item.id == "banner")
    video = next(item for item in steps if item.id == "video")

    assert "headline, subheadline, offer, cta" in selection.request_payload["system"]
    assert TEXT_SAFE_RULE in banner.request_payload["prompt"]
    assert "Не добавляй в кадр читаемый текст" in video.request_payload["prompt"]
