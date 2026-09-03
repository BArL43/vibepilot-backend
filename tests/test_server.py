from __future__ import annotations

import subprocess
import sys
from textwrap import dedent


def test_production_server_composes_v060_product_layer() -> None:
    code = dedent(
        """
        from fastapi.testclient import TestClient

        from app import main as core
        from app.server import VERSION, app
        from app.v060 import build_steps_v060

        assert VERSION == "0.6.2"
        assert app.version == VERSION
        assert core.API_VERSION == VERSION
        assert core.build_steps is build_steps_v060

        client = TestClient(app)
        response = client.get("/api/v1/product")
        assert response.status_code == 200
        payload = response.json()
        assert payload["name"] == "VibePilot"
        assert payload["version"] == VERSION
        assert payload["ui"] == "/"
        assert "budget_contract" in payload["features"]
        assert "campaign_history" in payload["features"]
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
