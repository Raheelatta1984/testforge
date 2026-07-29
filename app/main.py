import asyncio
import os
import traceback
import csv
import io
import zipfile
import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# INITIALIZE FASTAPI APP
app = FastAPI(title="TestForge AI Enterprise Edition")

# SAFE IMPORTS FROM LOCAL MODULES
try:
    from app.config import DEMO_MODE, ARTIFACTS
    from app.db import (SessionLocal, init_db, Project, Recording, RecordingStep,
                     Variable, Run, resolve_variables, interpolate)
    from app.recorder import RecorderSession
    from app.executor import execute_run
except Exception as e:
    print("CRITICAL IMPORT FAILURE IN MAIN.PY")
    traceback.print_exc()
    raise e

# Initialize Database Schema
init_db()

# WebSocket Subscription Manager for Live Execution Updates
RUN_SUBS: dict[str, list[asyncio.Queue]] = {}

async def emit_run(run_id: str, evt: dict):
    if run_id in RUN_SUBS:
        for q in list(RUN_SUBS[run_id]):
            try:
                q.put_nowait(evt)
            except Exception:
                pass

# DATA SERIALIZERS
def proj_to_dict(p):
    return {"id": p.id, "name": p.name, "base_url": p.base_url}

def step_to_dict(s):
    return {
        "id": s.id, 
        "order": s.order, 
        "action": s.action,
        "selector": s.selector, 
        "value": s.value, 
        "url": s.url,
        "label": s.label, 
        "screenshot_path": s.screenshot_path
    }

def rec_to_dict(r, with_steps=False):
    data = {
        "id": r.id, 
        "project_id": r.project_id, 
        "parent_id": r.parent_id, 
        "name": r.name,
        "start_url": r.start_url,
        "shared": r.shared, 
        "status": r.status, 
        "has_video": bool(r.video_path),
        "step_count": len(r.steps)
    }
    if with_steps:
        data["steps"] = [step_to_dict(s) for s in r.steps]
    return data

# --- API ROUTES: PROJECTS ---

@app.get("/api/projects")
def get_projects():
    db = SessionLocal()
    try:
        items = db.query(Project).all()
        return [proj_to_dict(p) for p in items]
    finally:
        db.close()

@app.post("/api/projects")
def create_project(body: dict):
    db = SessionLocal()
    try:
        new_project = Project(
            name=body.get('name', 'New Project'), 
            base_url=body.get('base_url', '')
        )
        db.add(new_project)
        db.commit()
        db.refresh(new_project)
        # Create a default base_url variable for convenience
        default_var = Variable(
            scope="project", 
            project_id=new_project.id, 
            name="base_url", 
            value=new_project.base_url
        )
        db.add(default_var)
        db.commit()
        return proj_to_dict(new_project)
    finally:
        db.close()

# --- API ROUTES: VARIABLES ---

@app.get("/api/variables")
def get_variables(project_id: str):
    db = SessionLocal()
    try:
        # Get variables for this project + global ones
        vars = db.query(Variable).filter(
            (Variable.scope == "global") | (Variable.project_id == project_id)
        ).all()
        # Find which recordings use each variable for tagging
        all_recs = db.query(Recording).filter(Recording.project_id == project_id).all()
        
        result_list = []
        for v in vars:
            tags = []
            for r in all_recs:
                if any(v.name in str(s.value) for s in r.steps):
                    tags.append(r.name)
            
            result_list.append({
                "id": v.id,
                "name": v.name,
                "value": v.value,
                "associated_recordings": tags
            })
        return result_list
    finally:
        db.close()

@app.post("/api/variables")
def create_variable(body: dict):
    db = SessionLocal()
    try:
        new_var = Variable(
            scope="project",
            project_id=body.get("project_id"),
            name=body.get("name"),
            value=body.get("value")
        )
        db.add(new_var)
        db.commit()
        return {"id": new_var.id}
    finally:
        db.close()

@app.patch("/api/variables/{var_id}")
def update_variable(var_id: str, body: dict):
    db = SessionLocal()
    try:
        v = db.get(Variable, var_id)
        if v and "value" in body:
            v.value = body["value"]
        db.commit()
        return {"status": "updated"}
    finally:
        db.close()

@app.delete("/api/variables/{var_id}")
def delete_variable(var_id: str):
    db = SessionLocal()
    try:
        v = db.get(Variable, var_id)
        if v:
            db.delete(v)
            db.commit()
        return {"status": "deleted"}
    finally:
        db.close()

# --- API ROUTES: RECORDINGS ---

@app.get("/api/projects/{pid}/recordings")
def get_project_recordings(pid: str):
    db = SessionLocal()
    try:
        # Get project recordings + any shared recordings
        items = db.query(Recording).filter(
            (Recording.project_id == pid) | (Recording.shared == True)
        ).all()
        return [rec_to_dict(r) for r in items]
    finally:
        db.close()

@app.post("/api/recordings")
def create_recording_entry(body: dict):
    db = SessionLocal()
    try:
        new_rec = Recording(
            project_id=body.get('project_id'),
            parent_id=body.get('parent_id'),
            name=body.get('name'),
            start_url=body.get('start_url'),
            status="recording"
        )
        db.add(new_rec)
        db.commit()
        db.refresh(new_rec)
        return {"id": new_rec.id}
    finally:
        db.close()

@app.get("/api/recordings/{rid}/export/jenkins")
def export_jenkins_gherkin(rid: str):
    db = SessionLocal()
    try:
        r = db.get(Recording, rid)
        if not r: raise HTTPException(404)
        
        feature_text = f"Feature: {r.name}\n"
        feature_text += f"  # Automated Test Generated by TestForge AI\n\n"
        feature_text += f"  Scenario: UI Validation of {r.name}\n"
        feature_text += f"    Given I navigate to '{r.start_url}'\n"
        
        for s in r.steps:
            if s.action == "click":
                feature_text += f"    When I click on the element labeled '{s.label}'\n"
            elif s.action == "fill":
                feature_text += f"    And I enter '{s.value}' into the '{s.label}' field\n"
            elif s.action == "press":
                feature_text += f"    And I press the '{s.value}' key\n"
        
        feature_text += f"    Then the sequence completes without errors\n"
        return PlainTextResponse(feature_text)
    finally:
        db.close()

# --- API ROUTES: EXECUTION & RUNS ---

@app.post("/api/runs")
def queue_run(body: dict):
    db = SessionLocal()
    try:
        new_run = Run(
            target_type="recording",
            target_id=body.get('target_id'),
            status="queued"
        )
        db.add(new_run)
        db.commit()
        db.refresh(new_run)
        run_id = new_run.id
        
        # Background Execution Spawning
        async def run_task():
            async def on_frame_callback(data):
                await emit_run(run_id, {"type": "frame", "data": data})
            
            async def on_event_callback(evt):
                await emit_run(run_id, {"type": "step", **evt})
            
            await execute_run(run_id, on_event_callback, on_frame=on_frame_callback)
        
        asyncio.create_task(run_task())
        return {"run_id": run_id}
    finally:
        db.close()

@app.get("/api/runs")
def get_runs_list():
    db = SessionLocal()
    try:
        items = db.query(Run).order_by(Run.created_at.desc()).limit(20).all()
        results = []
        for r in items:
            # Attach baseline recording for the Comparison tab
            baseline_steps = []
            source_rec = db.get(Recording, r.target_id)
            if source_rec:
                baseline_steps = [{"action": s.action, "label": s.label} for s in source_rec.steps]
            
            results.append({
                "id": r.id,
                "status": r.status,
                "created_at": str(r.created_at),
                "log": r.log,
                "baseline": baseline_steps
            })
        return results
    finally:
        db.close()

@app.get("/api/runs/screenshot/{run_id}/{filename}")
def serve_run_screenshot(run_id: str, filename: str):
    file_path = os.path.join(ARTIFACTS, "runs", run_id, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    raise HTTPException(404)