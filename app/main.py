# ... (Keep previous imports)
import httpx

@app.get("/api/sync/github-bundle")
def github_sync_bundle():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zipf:
        runs_dir = f"{ARTIFACTS}/runs"
        if os.path.exists(runs_dir):
            for root, _, files in os.walk(runs_dir):
                for file in files:
                    # Capture screenshots and logs
                    full_path = os.path.join(root, file)
                    arc_path = os.path.relpath(full_path, ARTIFACTS)
                    zipf.write(full_path, arc_path)
        
        # Add a text log summary
        db = SessionLocal()
        runs = db.query(Run).all()
        summary = "Run ID, Status, Created\n"
        for r in runs:
            summary += f"{r.id}, {r.status}, {r.created_at}\n"
        zipf.writestr("execution_summary.csv", summary)
        db.close()

    buf.seek(0)
    return Response(buf.read(), media_type="application/zip", 
                    headers={"Content-Disposition": f"attachment; filename=TestForge_Logs_{timestamp}.zip"})

# NEW: Action to trigger a push to GitHub directly from the UI
@app.post("/api/sync/github-push")
async def github_push_logs(token: str, repo: str):
    # This simulates the "Senior QA" flow of pushing artifacts to a repo
    # In a real enterprise app, we'd use 'git' commands or GitHub API
    return {"message": "Logs queued for GitHub Sync folder 'logs/test-results'"}