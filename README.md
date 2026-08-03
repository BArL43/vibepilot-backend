# VibePilot API

Budget-aware workflow orchestration for the VibeMarketolog Agent API.

## What this demo proves

- A campaign is planned before any paid operation.
- Required and optional costs are separated.
- Expensive video generation is protected by an approval gate.
- The frontend never receives the VibeMarketolog token.
- The service can run safely without a token in deterministic demo mode.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open `http://localhost:8000/docs`.

## Render

`render.yaml` defines the web service, health check, and non-secret environment
variables. Set `VIBE_API_TOKEN` in the Render dashboard only when switching from
demo mode to live Agent API calls.
