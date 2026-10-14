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

from app.db import SessionLocal
from app.errors import redact

from . import registry, sources
from .common import InputError, InputLimitError

router = APIRouter()
MAX_BODY_BYTES = 2_500_000

# Which stored sources each agent accepts, and the builder for each.
SOURCE_RULES = {
    "failure-triage": ("run",),
    "defect-drafting": ("run",),
    "flaky": ("recording",),
    "regression": ("project",),
    "release-summary": ("project",),
}


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
    kind = str(source.get("type") or "")
    allowed = SOURCE_RULES.get(agent_id, ())
    if kind not in allowed:
        raise HTTPException(400, f"{registry.BY_ID[agent_id]['name']} accepts a stored source of type: "
                                 + (", ".join(allowed) or "none (paste its input instead)") + ".")
    with SessionLocal() as db:
        try:
            if kind == "run":
                run_id = str(source.get("run_id") or "").strip()
                if not run_id:
                    raise HTTPException(400, "run_id is required.")
                if agent_id == "defect-drafting":
                    return sources.defect_payload(db, run_id)
                return sources.triage_payload(db, run_id)
            if kind == "recording":
                recording_id = str(source.get("recording_id") or "").strip()
                if not recording_id:
                    raise HTTPException(400, "recording_id is required.")
                return sources.flaky_payload(db, recording_id, source.get("limit") or 100)
            project_id = str(source.get("project_id") or "").strip()
            if not project_id:
                raise HTTPException(400, "project_id is required.")
            if agent_id == "regression":
                return sources.regression_payload(project_id)
            return sources.release_payload(db, project_id, source.get("since"), source.get("until"),
                                           source.get("batch_id"))
        except sources.SourceNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, redact(str(exc))) from None


@router.post("/api/agents/{agent_id}/from-source")
async def run_agent_from_source(agent_id: str, request: Request):
    _known(agent_id)
    body = await _body(request)
    mode = _mode(body)
    source = body.get("source")
    if not isinstance(source, dict):
        raise HTTPException(400, "Provide a \"source\" object, for example {\"type\": \"run\", \"run_id\": \"...\"}.")
    if agent_id not in SOURCE_RULES:
        raise HTTPException(400, f"{registry.BY_ID[agent_id]['name']} takes pasted input; use /run.")
    built = _source_payload(agent_id, source)
    overrides = body.get("input") if isinstance(body.get("input"), dict) else {}
    return _execute(agent_id, {**built, **overrides}, mode)
