# Charter gate on Alibaba Cloud Function Compute (live deployment)

A dependency-free (stdlib only) slice of the project, **deployed and running on
Alibaba Cloud Function Compute (FC 3.0)**, that exercises the real Charter
compatibility check on **Qwen via Alibaba Cloud DashScope**.

## Live URL

```
https://charter-server-iolbockadg.cn-hangzhou.fcapp.run
```

| Endpoint | Result |
|---|---|
| `GET /` | service banner + usage |
| `GET /healthz` | `ok` |
| `GET /gate?task=<action>` | runs the comms-agent charter gate on `<action>` |

Verified live on FC (judged by `qwen-max` on DashScope):

| Task | Verdict | Clause |
|---|---|---|
| `Send an internal reminder email to the finance team` | **allow** | C-001 (scope) |
| `Send an email to the external auditor` | **needs_approval** | C-201 (approval_required) |
| `Send the client tax summary to an outside records address` | **incompatible** | C-102 (out_of_scope — red line) |

Try it:
```
.../gate?task=Send%20an%20internal%20reminder%20email          -> allow
.../gate?task=Send%20an%20email%20to%20the%20external%20auditor -> needs_approval
.../gate?task=Send%20the%20client%20tax%20summary%20to%20an%20outside%20address -> incompatible
```

## How it was deployed (FC 3.0 Web Function)

- **Function type:** Web Function, custom runtime (Debian 10 / Python 3.10).
- **Code:** `app.py` (this folder) — pulled into the function's `/code` via the
  built-in WebIDE terminal (`curl` from this public repo), then **部署代码**.
- **Start command:** `python3 app.py` · **Listen port:** `9000`.
- **Scaling:** min instances `0` (scale-to-zero — free when idle).
- **Trigger:** HTTP trigger, **anonymous** auth (public URL).
- **Env vars:** `DASHSCOPE_API_KEY` (Alibaba Cloud Model Studio key — set in the
  console, never committed), `CHARTER_QWEN_MODEL=qwen-max`.

No Docker, no image registry, no external pip dependencies — the service is
stdlib + `urllib` calling the DashScope OpenAI-compatible endpoint, so it deploys
as plain code.

> Note: the endpoint is anonymous for demo/judging convenience and calls Qwen on
> the owner's DashScope quota. After judging, disable the trigger or delete the
> `charter-server` function to stop any usage.
