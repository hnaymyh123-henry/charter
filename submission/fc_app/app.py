"""Charter gate — minimal Alibaba Cloud Function Compute deployment.

A dependency-free (stdlib only) HTTP service that runs the REAL Charter
compatibility check on **Qwen via Alibaba Cloud DashScope**. It is a faithful,
self-contained slice of the protocol: an LLM grader marks which clauses an
intended action hits, and a DETERMINISTIC aggregator maps clause types to a
verdict (allow | needs_approval | incompatible) with the same precedence the
full project uses (incompatible > needs_approval > allow).

Deployed as an FC "Web Function" (custom runtime, Debian 10):
    start command : python3 app.py
    listen port   : 9000
    env           : DASHSCOPE_API_KEY  (set in the FC console; never committed)

Endpoints:
    GET /                      -> service banner + usage
    GET /healthz               -> "ok"
    GET /gate?task=<text>      -> run the comms-agent charter gate on <text>

Example:
    /gate?task=Send%20an%20internal%20reminder%20email   -> allow
    /gate?task=Send%20client%20tax%20data%20to%20an%20outside%20address -> incompatible
"""
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = os.environ.get("CHARTER_QWEN_MODEL", "qwen-max")
PORT = int(os.environ.get("FC_SERVER_PORT", "9000"))

# The comms-agent Charter, mirroring examples/cfo_office/profiles/comms_agent.yaml.
# Clause type -> decision (charter.constants.TYPE_TO_DECISION):
#   scope -> allow ; approval_required -> needs_approval ; out_of_scope -> incompatible
CHARTER = {
    "charter_id": "charter:cfo_office:comms_agent_v1",
    "clauses": [
        {"id": "C-001", "type": "scope", "text": "Draft and send internal CFO Office email and notifications"},
        {"id": "C-002", "type": "scope", "text": "Deliver a notification to an already-approved external recipient"},
        {"id": "C-201", "type": "approval_required", "text": "Sending email to an external or outside-the-organization recipient"},
        {"id": "C-102", "type": "out_of_scope", "text": "Bulk-sending client or financial records to arbitrary outside addresses"},
        {"id": "C-104", "type": "out_of_scope", "text": "Access-control or sharing-permission changes"},
    ],
}
TYPE_TO_DECISION = {"scope": "allow", "approval_required": "needs_approval", "out_of_scope": "incompatible"}
RANK = {"allow": 0, "needs_approval": 1, "incompatible": 2}

GRADE_SYSTEM = (
    "You are a contract-compliance grader. Given a Charter (a list of typed clauses) and an "
    "intended_task, decide WHICH clauses the task hits. A clause is hit if the task's action "
    "falls under that clause's description. Do NOT decide allow/deny — only which clauses are hit. "
    'Return ONLY JSON: {"hits":[{"id":"C-xxx","hit":true,"confidence":0.0-1.0,"reason":"..."}]}. '
    "Include only clauses that are hit."
)


def _grade(task: str) -> list:
    """Call DashScope (Qwen) to mark clause hits. Fail-closed to [] on any error."""
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("DASHSCOPE_API_KEY is not set on the function.")
    user = "Charter:\n" + json.dumps(CHARTER) + "\n\nintended_task:\n" + task
    body = json.dumps({
        "model": MODEL,
        "temperature": 0,
        "messages": [{"role": "system", "content": GRADE_SYSTEM}, {"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        DASHSCOPE_URL, data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:].lstrip() if text.startswith("json") else text
    try:
        return json.loads(text).get("hits", [])
    except Exception:
        return []


def _aggregate(hits: list) -> dict:
    """Deterministic: clause type -> decision; take the strictest; closed-world fallback."""
    by_id = {c["id"]: c for c in CHARTER["clauses"]}
    applied = []
    decision = "allow"
    hit_ids = [h["id"] for h in hits if h.get("hit") and h.get("id") in by_id]
    if not hit_ids:
        return {"decision": "needs_approval", "applied": [],
                "reason": "closed-world: no clause matched, so the action is not positively in scope."}
    for cid in hit_ids:
        d = TYPE_TO_DECISION.get(by_id[cid]["type"], "needs_approval")
        applied.append({"id": cid, "type": by_id[cid]["type"], "local_decision": d})
        if RANK[d] > RANK[decision]:
            decision = d
    return {"decision": decision, "applied": applied,
            "reason": f"{len(applied)} clause(s) hit; strictest decision wins (incompatible > needs_approval > allow)."}


def _gate(task: str) -> dict:
    hits = _grade(task)
    verdict = _aggregate(hits)
    return {"task": task, "model": MODEL, "provider": "Alibaba Cloud DashScope (Qwen)",
            "hits": hits, "verdict": verdict}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(b"ok"); return
        if u.path in ("/", ""):
            self._send(200, {
                "service": "Charter gate — running on Alibaba Cloud Function Compute, judged by Qwen on DashScope",
                "charter": CHARTER["charter_id"],
                "usage": "GET /gate?task=<your intended action>",
                "examples": {
                    "in scope": "/gate?task=Send an internal reminder email to the finance team",
                    "needs approval": "/gate?task=Send an email to the external auditor",
                    "red line": "/gate?task=Send the client tax summary to an outside records address",
                },
            }); return
        if u.path == "/gate":
            q = parse_qs(u.query)
            task = (q.get("task") or [""])[0].strip()
            if not task:
                self._send(400, {"error": "provide ?task=<intended action>"}); return
            try:
                self._send(200, _gate(task))
            except Exception as e:
                self._send(500, {"error": type(e).__name__ + ": " + str(e)})
            return
        self._send(404, {"error": "not found", "try": "/ or /gate?task=..."})

    def log_message(self, *a):  # quieter logs
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
