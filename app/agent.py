import json, os, re, shlex, time
from datetime import datetime

from app.config import ARTIFACTS, DEMO_MODE
from app.db import SessionLocal, Run, Recording, Scenario, resolve_variables, interpolate

MODEL     = os.environ.get("TF_AGENT_MODEL", "claude-sonnet-4-5")
MAX_ITERS = int(os.environ.get("TF_AGENT_MAX_ITERS", "40"))
MCP_CMD   = os.environ.get("TF_MCP_CMD", "npx")
MCP_ARGS  = shlex.split(os.environ.get(
    "TF_MCP_ARGS", "-y @playwright/mcp@latest --headless --isolated --caps=vision"))

SYSTEM = (
    "You are a QA automation agent driving a real browser via Playwright MCP tools.\n"
    "Rules:\n"
    "1. Execute the steps IN ORDER. After each step, verify its expected result "
    "using browser_snapshot before moving on.\n"
    "2. If an element is not found by its description, inspect the snapshot and pick "
    "the closest matching element (prefer role/label refs).\n"
    "3. For paste steps use browser_type with the captured value.\n"
    "4. Never claim success without verifying it in the snapshot.\n"
    "5. When finished, write a short summary, then a final line containing ONLY JSON:\n"
    '   {"verdict":"pass"|"fail","failed_step":<n|null>,"summary":"..."}'
)

def _load_target(db, run):
    if run.target_type == "recording":
        t = db.get(Recording, run.target_id)
        steps = [{"order": s.order, "action": s.action, "label": s.label,
                  "value": s.value, "url": s.url,
                  "selector": (s.selector or {}).get("primary")} for s in t.steps]
        return t, steps, t.project_id, t.id
    t = db.get(Scenario, run.target_id)
    steps = [{"order": s.order, "action": s.action, "label": s.expected_result,
              "value": s.value, "url": None, "selector": None} for s in t.steps]
    return t, steps, t.project_id, None

def _prompt(target, steps, variables):
    lines = []
    for s in steps:
        parts = [f"{s['order']}. [{s['action']}]"]
        if s.get("url"):      parts.append(f"url={s['url']}")
        if s.get("value"):    parts.append(f'value="{interpolate(s["value"], variables)}"')
        if s.get("selector"): parts.append(f"selector={s['selector']}")
        if s.get("label"):    parts.append(f"— {s['label']}")
        lines.append(" ".join(parts))
    start = next((s["url"] for s in steps if s.get("url")),
                 getattr(target, "start_url", ""))
    return ("Execute this test flow in the browser.\n"
            f"START URL: {start}\n"
            f"VARIABLES: {json.dumps(variables)}\n"
            "STEPS:\n" + "\n".join(lines) +
            "\nBegin by navigating to the start URL. End with the FINAL JSON verdict line.")

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
            raise RuntimeError(
                "Agent mode needs ANTHROPIC_API_KEY. Script mode works without it.")
        from anthropic import AsyncAnthropic
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        target, steps, project_id, rec_id = _load_target(db, run)
        variables = resolve_variables(db, project_id=project_id, recording_id=rec_id)
        prompt = _prompt(target, steps, variables)

        run_dir = f"{ARTIFACTS}/runs/{run_id}"
        os.makedirs(run_dir, exist_ok=True)
        params = StdioServerParameters(command=MCP_CMD,
            args=MCP_ARGS + ["--output-dir", run_dir], env={**os.environ})

        client = AsyncAnthropic()
        verdict = {"verdict": "error", "summary": "no response"}

        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as mcp:
                await mcp.initialize()
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
                        verdict = {"verdict": "error",
                                   "summary": f"stop_reason={resp.stop_reason}"}; break

                    results = []
                    for b in resp.content:
                        if b.type != "tool_use":
                            continue
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
                        if is_error:
                            entry["error"] = json.dumps(content, default=str)[:300]
                        log.append(entry)
                        await on_event({"type": "step", **entry})
                        results.append({"type": "tool_result", "tool_use_id": b.id,
                                        "content": content, "is_error": is_error})
                    messages.append({"role": "assistant", "content": resp.content})
                    messages.append({"role": "user", "content": results})
                else:
                    verdict = {"verdict": "inconclusive", "summary": "max iterations"}

        run.status = {"pass": "passed", "fail": "failed"}.get(
            verdict.get("verdict"), "error")
        if verdict.get("summary"):
            log.append({"order": order + 1, "action": "verdict",
                        "label": verdict["summary"][:300], "status": run.status})
    except Exception as e:
        run.status = "error"; run.error = str(e)[:1000]
    finally:
        run.log = log
        run.agent_transcript = transcript
        run.finished_at = datetime.utcnow()
        db.commit()
        await on_event({"type": "done", "status": run.status,
                        "log": log, "has_video": False})
        db.close()
