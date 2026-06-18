# Alibaba Cloud (Qwen Cloud) usage & deployment

## Service call (required artifact)

The project's inference runs on **Alibaba Cloud DashScope / Model Studio** (Qwen
Cloud) via its OpenAI-compatible endpoint. The minimal proof is
[`aliyun_proof.py`](aliyun_proof.py); the production seam is
[`charter/adapters/qwen.py`](../../charter/adapters/qwen.py).

```bash
export DASHSCOPE_API_KEY=...     # Alibaba Cloud Model Studio (Bailian) API key
python submission/aliyun_deploy/aliyun_proof.py
```

- Endpoint: `https://dashscope.aliyuncs.com/compatible-mode/v1` (use the `-intl`
  host outside mainland China).
- Model: `qwen-max` (configurable via `CHARTER_QWEN_MODEL`).
- The same key + env powers the live demo and the A/B/C experiment:
  `CHARTER_LLM_PROVIDER=qwen DASHSCOPE_API_KEY=... python -m examples.cfo_office.experiment`.

> During development we routed Qwen through a third-party OpenAI-compatible
> gateway for convenience; for the submission, set `DASHSCOPE_*` to the
> **Alibaba Cloud** endpoint above so every model call is on Qwen Cloud.

## Optional: deploy the server on Alibaba Cloud

The Charter MCP/HTTP server is a standard FastAPI app (`charter/server.py`) with
a `Dockerfile`. To run it on Alibaba Cloud:

- **ECS**: pull the image, `docker run -p 8000:8000 ... charter-server`, expose
  via the security group; the public URL serves signed charters at
  `/{principal}/{agent}` and the JWKS at `/.well-known/jwks.json`.
- **Function Compute / Serverless App Engine**: deploy the same container image;
  set `DASHSCOPE_API_KEY` and `CHARTER_URL_BASE` as environment variables.

Deployment is optional for the submission (the service-call artifact above is the
required item); a public server URL is a plus.
