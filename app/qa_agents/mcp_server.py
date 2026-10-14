"""MCP server that exposes the QA agents to AI clients such as Claude Code CLI.

Transport: stdio, one JSON-RPC 2.0 message per line (the MCP stdio transport).
Nothing but protocol messages is written to stdout; logs go to stderr.

Run from the repository root with the app's Python environment:

    python -m app.qa_agents.mcp_server

Register it with Claude Code CLI:

    claude mcp add testforge -- /path/to/venv/bin/python -m app.qa_agents.mcp_server

Tools (all read-only; they never start a browser or change stored data):

    qa_list_agents      the nine agents and the AI provider status
    qa_get_sample       an agent's example input
    qa_run_agent        run an agent on input you supply
    qa_run_from_source  run an agent on a stored run, recording or project

No MCP SDK is required: the protocol surface used here (initialize, ping, tools/list,
tools/call) is small and is implemented directly, so no new dependency is added.
"""
from __future__ import annotations

import json
import sys

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "testforge-qa-agents", "version": "1.0.0"}
INSTRUCTIONS = (
    "QA agents for TestForge: requirement review, test case and Gherkin generation, API contract review, "
    "automation code review, failure triage, flaky test analysis, defect drafting, regression selection and "
    "release decisions. Results are advisory. Use mode 'rules' to keep everything on this machine; 'auto' "
    "adds AI insights through the provider the server is configured for."
)

ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

_state: dict = {}


def _load() -> dict:
    """Import the application and prepare its database once. The app prints a boot banner and
    progress lines to stdout, which would corrupt the protocol stream, so stdout is pointed at
    stderr while it loads."""
    if _state:
        return _state
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        from app.db import init_db
        from app.errors import redact
        from app.qa_agents import registry, sources
        from app.qa_agents.common import InputError
        init_db()  # the web app does this at import; stored-data sources need the schema too
    finally:
        sys.stdout = real_stdout
    _state.update({"registry": registry, "sources": sources, "redact": redact,
                   "InputError": InputError})
    return _state


def _tools(agent_ids: list[str]) -> list[dict]:
    agent = {"type": "string", "enum": agent_ids, "description": "Agent id, e.g. 'failure-triage'."}
    mode = {"type": "string", "enum": ["auto", "rules"], "default": "auto",
            "description": "'rules' runs fully offline; 'auto' adds AI insights if the server has a provider."}
    read_only = {"readOnlyHint": True, "destructiveHint": False}
    return [
        {"name": "qa_list_agents", "description": "List the QA agents, their inputs and whether AI insights are on.",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}, "annotations": read_only},
        {"name": "qa_get_sample", "description": "Return an agent's example input, to show the expected fields.",
         "inputSchema": {"type": "object", "properties": {"agent": agent}, "required": ["agent"],
                         "additionalProperties": False}, "annotations": read_only},
        {"name": "qa_run_agent",
         "description": ("Run a QA agent on pasted input. Text fields are plain strings; list fields are JSON arrays; "
                         "use qa_get_sample for the exact fields."),
         "inputSchema": {"type": "object", "properties": {"agent": agent,
                                                         "input": {"type": "object"}, "mode": mode},
                         "required": ["agent", "input"], "additionalProperties": False},
         "annotations": read_only},
        {"name": "qa_run_from_source",
         "description": ("Run a QA agent on data already stored in TestForge: a run (failure-triage, "
                         "defect-drafting), a recording (flaky) or a project (regression, release-summary)."),
         "inputSchema": {"type": "object", "properties": {
             "agent": agent,
             "source": {"type": "object", "properties": {
                 "type": {"type": "string", "enum": ["run", "recording", "project"]},
                 "run_id": {"type": "string"}, "recording_id": {"type": "string"},
                 "project_id": {"type": "string"}, "limit": {"type": "integer"},
                 "since": {"type": "string"}, "until": {"type": "string"}, "batch_id": {"type": "string"}},
                 "required": ["type"]},
             "input": {"type": "object", "description": "Optional overrides for the built input."},
             "mode": mode}, "required": ["agent", "source"], "additionalProperties": False},
         "annotations": read_only},
    ]


def _text_result(payload, is_error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=True)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(name: str, args: dict) -> dict:
    state = _load()
    registry, sources = state["registry"], state["sources"]
    redact, InputError = state["redact"], state["InputError"]
    agent_id = str(args.get("agent") or "")
    try:
        if name == "qa_list_agents":
            catalogue = registry.catalogue()
            return _text_result({"agents": [{"id": a["id"], "name": a["name"], "input": a["input"],
                                             "from_source": a["sources"]} for a in catalogue["agents"]],
                                 "ai": catalogue["ai"]})
        if agent_id not in registry.BY_ID:
            return _text_result(f"Unknown agent '{agent_id}'. Call qa_list_agents for the list.", True)
        if name == "qa_get_sample":
            return _text_result(registry.sample(agent_id))
        mode = str(args.get("mode") or "auto")
        if mode not in ("auto", "rules"):
            return _text_result("mode must be 'auto' or 'rules'.", True)
        if name == "qa_run_agent":
            payload = args.get("input")
            if not isinstance(payload, dict):
                return _text_result("input must be a JSON object; call qa_get_sample for the fields.", True)
            return _text_result(registry.run(agent_id, payload, mode))
        if name == "qa_run_from_source":
            source = args.get("source")
            if not isinstance(source, dict):
                return _text_result("source must be an object, e.g. {\"type\": \"run\", \"run_id\": \"...\"}.", True)
            if agent_id not in sources.SOURCE_RULES:
                return _text_result(f"{agent_id} takes pasted input; use qa_run_agent.", True)
            built = sources.build_payload(agent_id, source, registry.BY_ID[agent_id]["name"])
            overrides = args.get("input") if isinstance(args.get("input"), dict) else {}
            return _text_result(registry.run(agent_id, {**built, **overrides}, mode))
        return _text_result(f"Unknown tool '{name}'.", True)
    except sources.SourceNotFound as exc:
        return _text_result(redact(str(exc)), True)
    except (InputError, ValueError) as exc:
        return _text_result(redact(str(exc)), True)
    except Exception as exc:  # an agent failure must not end the session; keep details out of the reply
        print(f"testforge-qa-agents: {name} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return _text_result(f"Internal error while running the tool ({type(exc).__name__}). See the server log.", True)


def _error(message_id, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


def handle(message):
    """Answer one JSON-RPC message. Returns the response dict, or None for notifications and replies."""
    if isinstance(message, list):
        responses = [r for r in (handle(item) for item in message) if r is not None]
        return responses or None
    if not isinstance(message, dict):
        return _error(None, ERR_INVALID_REQUEST, "Request must be a JSON object.")
    if "method" not in message:
        return None  # a reply from the client; this server sends no requests
    method = message.get("method")
    if "id" not in message:
        return None  # notification such as notifications/initialized
    message_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        requested = str(params.get("protocolVersion") or "")
        version = requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": message_id, "result": {
            "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        state = _load()
        return {"jsonrpc": "2.0", "id": message_id,
                "result": {"tools": _tools(list(state["registry"].BY_ID))}}
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error(message_id, ERR_INVALID_PARAMS, "tools/call needs a tool name.")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        return {"jsonrpc": "2.0", "id": message_id, "result": _call_tool(name, args)}
    return _error(message_id, ERR_METHOD_NOT_FOUND, f"Method not found: {method}")


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    print("testforge-qa-agents: ready on stdio", file=sys.stderr)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = _error(None, ERR_PARSE, "Parse error: the line is not JSON.")
        else:
            try:
                response = handle(message)
            except Exception as exc:  # keep serving: one bad message must not end the session
                print(f"testforge-qa-agents: handler failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                response = _error(message.get("id") if isinstance(message, dict) else None,
                                  ERR_INTERNAL, "Internal error.")
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
            stdout.flush()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
