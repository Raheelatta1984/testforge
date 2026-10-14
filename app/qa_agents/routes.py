"""HTTP surface for the QA agent hub.

    GET  /api/agents                          catalogue, AI status, modes
    GET  /api/agents/{agent_id}/sample        example input for the dashboard's LOAD SAMPLE button
    POST /api/agents/{agent_id}/run           run on pasted input: {"input": {...}, "mode": "auto"|"rules"}
    POST /api/agents/{agent_id}/from-source   run on stored data:  {"source": {...}, "input": {...}, "mode": ...}

Routes are plain `def` so the optional AI call (bounded by a timeout) runs in
the thread pool rather than blocking the event loop. Every error body is a
short message; nothing echoes a credential back.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from app.errors import redact

from . import registry, sources
from .common import InputError, InputLimitError

router = APIRouter()
MAX_BODY_BYTES = 2_500_000

async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, f"Request is larger than {MAX_BODY_BYTES // 1_000_000} MB; split the input.")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Request body must be JSON.") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "Request body must be a JSON object.")
    return data


def _mode(body: dict) -> str:
    mode = str(body.get("mode") or "auto")
    if mode not in ("auto", "rules"):
        raise HTTPException(400, "mode must be 'auto' or 'rules'.")
    return mode


def _known(agent_id: str) -> None:
    if agent_id not in registry.BY_ID:
        raise HTTPException(404, "Unknown agent. See GET /api/agents for the list.")


def _execute(agent_id: str, payload: dict, mode: str) -> dict:
    try:
        return registry.run(agent_id, payload, mode)
    except InputLimitError as exc:
        raise HTTPException(413, redact(str(exc))) from None
    except InputError as exc:
        raise HTTPException(400, redact(str(exc))) from None
    except ValueError as exc:
        raise HTTPException(400, redact(str(exc))) from None


@router.get("/api/agents")
def list_agents():
    return registry.catalogue()


@router.get("/api/agents/{agent_id}/sample")
def agent_sample(agent_id: str):
    _known(agent_id)
    return {"agent": agent_id, "input": registry.sample(agent_id)}


@router.post("/api/agents/{agent_id}/run")
async def run_agent(agent_id: str, request: Request):
    _known(agent_id)
    body = await _body(request)
    mode = _mode(body)
    payload = body.get("input")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Provide the agent's input as a JSON object under \"input\".")
    return _execute(agent_id, payload, mode)


def _source_payload(agent_id: str, source: dict) -> dict:
    try:
        return sources.build_payload(agent_id, source, registry.BY_ID[agent_id]["name"])
    except sources.SourceNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:  # InputError is a ValueError: a bad or unsupported source
        raise HTTPException(400, redact(str(exc))) from None


@router.post("/api/agents/{agent_id}/from-source")
async def run_agent_from_source(agent_id: str, request: Request):
    _known(agent_id)
    body = await _body(request)
    mode = _mode(body)
    source = body.get("source")
    if not isinstance(source, dict):
        raise HTTPException(400, "Provide a \"source\" object, for example {\"type\": \"run\", \"run_id\": \"...\"}.")
    if agent_id not in sources.SOURCE_RULES:
        raise HTTPException(400, f"{registry.BY_ID[agent_id]['name']} takes pasted input; use /run.")
    built = _source_payload(agent_id, source)
    overrides = body.get("input") if isinstance(body.get("input"), dict) else {}
    return _execute(agent_id, {**built, **overrides}, mode)
