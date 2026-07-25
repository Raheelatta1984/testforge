import json, os, re, time
from datetime import datetime

from app.config import ARTIFACTS, DEMO_MODE
from app.db import SessionLocal, Run, Recording, Scenario, resolve_variables, interpolate

MODEL     = os.environ.get("TF_AGENT_MODEL", "claude-sonnet-4-5")
MAX_ITERS = int(os.environ.get("TF_AGENT_MAX_ITERS", "40"))

# Transport configuration
MCP_TRANSPORT = os.environ.get("TF_MCP_TRANSPORT", "stdio")  # "stdio" or "sse"
MCP_URL       = os.environ.get("TF_MCP_URL", "https://mcp.render.com/mcp")
MCP_TOKEN     = os.environ.get("TF_MCP_TOKEN", "rnd_fdtToZxFf0d7YotgduQDMnwQjUxH")

SYSTEM = (
    "You are an AI assistant with access to Render and cloud infrastructure tools via MCP.\n"
    "Execute the user's instructions, inspect tool outputs, and complete the task.\n"
    "When finished, write a short summary, then a final line containing ONLY JSON:\n"
    '   {"verdict":"pass"|"fail","summary":"..."}'
)

async def _connect_mcp(run_dir):
    """Supports both local stdio (Playwright MCP) and remote HTTP/SSE MCP (Render MCP)."""
    if MCP_TRANSPORT == "sse":
        from mcp.client.sse import sse_client
        from mcp import ClientSession
        headers = {"Authorization": f"Bearer {MCP_TOKEN}"}
        # sse_client returns an async context manager yielding (read_stream, write_stream)
        async with sse_client(MCP_URL, headers=headers) as (r, w):
            async with ClientSession(r, w) as mcp:
                await mcp.initialize()
                yield mcp
    else:
        import shlex
        from mcp.client.stdio import stdio_client
        from mcp import StdioServerParameters, ClientSession
        args = shlex.split(os.environ.get(
            "TF_MCP_ARGS", "-y @playwright/mcp@latest --headless --isolated --caps=vision"))
        params = StdioServerParameters(command="npx", args=args + ["--output-dir", run_dir], env={**os.environ})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as mcp:
                await mcp.initialize()
                yield mcp

def _convert(res):
    blocks = []
    for c in res.content:
        if c.type == "text":
            blocks.append({"type": "text", "text": c.text[:12000]})
        elif c.type == "image":
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": c.mimeType, "data": c.data}})
    return (blocks or "ok"), bool(res.isError)

def _verdict(resp):
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r'\{[^{}]*"verdict"[^{}]*\}', text, re.DOTALL)
    if m:
        try: return json.loads(m.group())
        except Exception: pass
    return {"verdict": "unknown", "summary": text[-400:]}

async def run_agent(run_id, on_event):
    db = SessionLocal()
    run = db.get(Run, run_id)
    run.status = "running"; db.commit()
    log, transcript, order = [], [], 0
    try:
        if DEMO_MODE:
            raise RuntimeError("Agent mode needs ANTHROPIC_API_KEY for the LLM driver.")
        from anthropic import AsyncAnthropic

        target = db.get(Scenario, run.target_id) if run.target_type == "scenario" else db.get(Recording, run.target_id)
        prompt = f"Task target: {getattr(target, 'title', getattr(target, 'name', 'Run Task'))}"

        run_dir = f"{ARTIFACTS}/runs/{run_id}"
        os.makedirs(run_dir, exist_ok=True)

        client = AsyncAnthropic()
        verdict = {"verdict": "error", "summary": "no response"}

        async for mcp in _connect_mcp(run_dir):
            tools = [{"name": t.name, "description": t.description or "",
                      "input_schema": t.inputSchema}
                     for t in (await mcp.list_tools()).tools]
            messages = [{"role": "user", "content": prompt}]

            for it in range(MAX_ITERS):
                resp = await client.messages.create(
                    model=MODEL, max_tokens=4096, system=SYSTEM,
                    tools=tools, messages=messages)
                transcript.append({"iter": it, "stop": resp.stop_reason,
                    "content": [{"type": b.type,
                        **({"text": b.text} if b.type == "text" else
                           {"name": b.name, "input": b.input}
                           if b.type == "tool_use" else {})}
                        for b in resp.content]})

                if resp.stop_reason == "end_turn":
                    verdict = _verdict(resp); break
                if resp.stop_reason != "tool_use":
                    verdict = {"verdict": "error", "summary": f"stop_reason={resp.stop_reason}"}; break

                results = []
                for b in resp.content:
                    if b.type != "tool_use": continue
                    order += 1
                    started = time.monotonic()
                    try:
                        res = await mcp.call_tool(b.name, b.input)
                        content, is_error = _convert(res)
                    except Exception as exc:
                        content, is_error = str(exc), True
                    entry = {"order": order, "action": b.name,
                             "label": json.dumps(b.input, default=str)[:140],
                             "status": "failed" if is_error else "passed",
                             "duration_ms": int((time.monotonic() - started) * 1000)}
                    if is_error: entry["error"] = json.dumps(content, default=str)[:300]
                    log.append(entry)
                    await on_event({"type": "step", **entry})
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": content, "is_error": is_error})
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": results})
            else:
                verdict = {"verdict": "inconclusive", "summary": "max iterations"}

        run.status = {"pass": "passed", "fail": "failed"}.get(verdict.get("verdict"), "error")
        if verdict.get("summary"):
            log.append({"order": order + 1, "action": "verdict", "label": verdict["summary"][:300], "status": run.status})
    except Exception as e:
        run.status = "error"; run.error = str(e)[:1000]
    finally:
        run.log = log
        run.agent_transcript = transcript
        run.finished_at = datetime.utcnow()
        db.commit()
        await on_event({"type": "done", "status": run.status, "log": log, "has_video": False})
        db.close()
