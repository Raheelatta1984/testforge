"""Record a browser session over HTTP, then replay it.

Requires Chromium. Set TF_CHROMIUM_PATH if Playwright's own browser is not
installed (the download CDN is not always reachable). Exits 0 on success.
"""

import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("TF_SMOKE_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"


def request(method, path, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                return resp.status, json.loads(raw.decode() or "null")
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            pass
        raise AssertionError(f"{method} {path} -> {exc.code}: {detail}") from exc


def jpeg_size(blob: bytes):
    """Read width/height from a JPEG SOF marker. Returns (width, height) or None."""
    if not blob.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index + 8 < len(blob):
        if blob[index] != 0xFF:
            index += 1
            continue
        marker = blob[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", blob[index + 5:index + 9])
            return width, height
        if marker == 0xD9:
            break
        if index + 4 > len(blob):
            break
        length = struct.unpack(">H", blob[index + 2:index + 4])[0]
        index += 2 + length
    return None


def wait_health(proc, log_path):
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit("server exited early:\n" + open(log_path, errors="replace").read()[-4000:])
        try:
            status, payload = request("GET", "/api/health", timeout=2)
            if status == 200 and payload.get("recorder") and payload.get("executor"):
                return payload
        except Exception:
            time.sleep(0.3)
    raise SystemExit("server did not become healthy:\n" + open(log_path, errors="replace").read()[-4000:])


def wait_frame(recording_id):
    deadline = time.time() + 40
    last_error = None
    while time.time() < deadline:
        status, session = request("GET", f"/api/recordings/{recording_id}/session")
        if session.get("error"):
            raise AssertionError("browser failed to start: " + session["error"])
        try:
            code, blob = request("GET", f"/api/recordings/{recording_id}/frame?t={time.time()}", timeout=10)
        except AssertionError as exc:
            last_error = str(exc)
            if "409" in last_error:
                raise
            time.sleep(0.3)
            continue
        if code == 200 and isinstance(blob, (bytes, bytearray)) and len(blob) > 4000:
            return blob
        time.sleep(0.3)
    raise AssertionError("no browser frame arrived. last=" + str(last_error))


def main():
    env = os.environ.copy()
    env["PORT"] = str(PORT)
    env.setdefault("TF_ARTIFACTS", "/tmp/tf-smoke-artifacts")
    env.setdefault("DATABASE_URL", "sqlite:////tmp/tf-smoke.db")
    for path in (env["DATABASE_URL"].replace("sqlite:///", ""), env["TF_ARTIFACTS"]):
        if path and os.path.exists(path) and path.endswith(".db"):
            os.remove(path)
    os.makedirs(env["TF_ARTIFACTS"], exist_ok=True)
    log_path = "/tmp/tf-smoke-server.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        health = wait_health(proc, log_path)
        print("health", health)
        _, project = request("POST", "/api/projects", {"name": "Smoke", "base_url": "/demo.html"})
        assert project["id"], project
        _, recording = request("POST", "/api/recordings", {
            "project_id": project["id"],
            "name": "Sample flow",
            "start_url": "/demo.html",
        })
        assert recording["start_url"].startswith("http://127.0.0.1:"), recording
        request("POST", f"/api/recordings/{recording['id']}/session", {})
        frame = wait_frame(recording["id"])
        size = jpeg_size(frame)
        print("frame", len(frame), "px", size)
        assert size and size[0] >= 800 and size[1] >= 500, size

        # Input sits at (40,120) 280x44. Button sits at (40,180) 160x44.
        request("POST", f"/api/recordings/{recording['id']}/input", {"type": "tap", "x": 80, "y": 140})
        request("POST", f"/api/recordings/{recording['id']}/input", {"type": "text", "text": "{{user}}"})
        request("POST", f"/api/recordings/{recording['id']}/input", {"type": "tap", "x": 80, "y": 200})
        _, saved = request("GET", f"/api/recordings/{recording['id']}")
        actions = [step["action"] for step in saved["steps"]]
        print("steps", actions, [step.get("label") for step in saved["steps"]])
        assert actions[0] == "navigate", actions
        assert "click" in actions and "type" in actions, actions
        request("POST", f"/api/recordings/{recording['id']}/stop", {})

        request("POST", "/api/variables", {"project_id": project["id"], "name": "user", "value": "Ada"})
        _, queued = request("POST", "/api/runs", {"target_id": recording["id"]})
        run_id = queued["run_id"]
        print("run", run_id)
        deadline = time.time() + 60
        final = None
        while time.time() < deadline:
            _, final = request("GET", f"/api/runs/{run_id}")
            if final["status"] in {"passed", "failed", "error", "failed_audited"}:
                break
            time.sleep(0.4)
        else:
            raise AssertionError("run did not finish: " + json.dumps(final)[:500])
        print("status", final["status"])
        excerpts = " | ".join(step.get("excerpt") or "" for step in final.get("log") or [])
        errors = [step.get("error") for step in final.get("log") or [] if step.get("error")]
        print("excerpts", excerpts[:400])
        if errors:
            print("errors", errors)
        if final["status"] != "passed":
            raise AssertionError(final.get("rog_monitor_log") or final["status"])
        assert "Hello, Ada" in excerpts, excerpts
        live_code, live = request("GET", f"/api/runs/{run_id}/live.jpg")
        assert live_code == 200 and len(live) > 4000, live_code
        print("PASS recording + execution")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
