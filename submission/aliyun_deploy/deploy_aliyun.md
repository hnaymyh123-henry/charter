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

## Live deployment (done) — Function Compute

A stdlib-only Charter-gate service is **deployed and running** on Alibaba Cloud
Function Compute, publicly reachable, judged by Qwen on DashScope:

```
https://charter-server-iolbockadg.cn-hangzhou.fcapp.run
  /gate?task=Send the client tax summary to an outside address   -> incompatible
  /gate?task=Send an email to the external auditor                -> needs_approval
  /gate?task=Send an internal reminder email                      -> allow
```

Source + deployment notes: [`submission/fc_app/`](../fc_app/). It is an FC 3.0
Web Function (custom runtime, `python3 app.py`, port 9000, scale-to-zero, anonymous
HTTP trigger), with `DASHSCOPE_API_KEY` set as a function env var. No Docker / image
registry needed — the service is stdlib + `urllib` against the DashScope endpoint.

## Other deployment options (container)

The Charter MCP/HTTP server is a standard FastAPI app (`charter/server.py`) with
a `Dockerfile`. To run it on Alibaba Cloud:

- **ECS**: pull the image, `docker run -p 8000:8000 ... charter-server`, expose
  via the security group; the public URL serves signed charters at
  `/{principal}/{agent}` and the JWKS at `/.well-known/jwks.json`.
- **Function Compute / Serverless App Engine**: deploy the same container image;
  set `DASHSCOPE_API_KEY` and `CHARTER_URL_BASE` as environment variables.

Deployment is optional for the submission (the service-call artifact above is the
required item); a public server URL is a plus.
