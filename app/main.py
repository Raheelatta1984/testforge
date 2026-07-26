# ... (Inside your main.py)

@app.post("/api/runs")
async def start_run(body: dict):
    db = SessionLocal()
    try:
        run = Run(target_type=body['target_type'], target_id=body['target_id'], mode="script")
        db.add(run)
        db.commit()
        db.refresh(run)
        rid = run.id
        
        # Correctly spawn background task with its own event loop reference
        async def run_wrapper():
            async def on_f(d): await emit_run(rid, {"type":"frame", "data":d})
            async def on_e(s): await emit_run(rid, s)
            await execute_run(rid, on_e, on_frame=on_f)
            
        asyncio.create_task(run_wrapper())
        return {"run_id": rid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()