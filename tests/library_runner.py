"""Execute catalog scenarios through a running TestForge server.

This is the same library the dashboard uses: create a recording, drive the
remote browser, then queue a run. No second automation stack.
"""

import json
import time
import urllib.error
import urllib.request

from tests.jpegutil import jpeg_size


class ScenarioError(AssertionError):
    pass


class LibraryClient:
    def __init__(self, base, log, timeout=25):
        self.base = base.rstrip("/")
        self.log = log
        self.timeout = timeout

    def request(self, method, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                ctype = resp.headers.get("content-type", "")
                if "json" in ctype:
                    return resp.status, json.loads(raw.decode() or "null")
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                parsed = json.loads(detail)
                detail = parsed.get("detail", detail)
            except Exception:
                pass
            return exc.code, detail

    def ok(self, method, path, body=None, timeout=None):
        status, payload = self.request(method, path, body, timeout=timeout)
        if status >= 400:
            raise ScenarioError(f"{method} {path} -> {status}: {payload}")
        return status, payload


def resolve(value, ctx):
    if isinstance(value, str) and value.startswith("$") and value[1:] in ctx:
        return ctx[value[1:]]
    if isinstance(value, dict):
        return {key: resolve(item, ctx) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, ctx) for item in value]
    return value


class ScenarioRunner:
    def __init__(self, client, log):
        self.client = client
        self.log = log

    def run(self, scenario):
        ctx = {}
        started = time.monotonic()
        self.log(f"BEGIN {scenario['id']} {scenario['title']}")
        try:
            for index, raw_step in enumerate(scenario["steps"], 1):
                step = resolve(raw_step, ctx)
                self.log(f"  step {index} {step['action']}")
                self._dispatch(step, ctx)
            elapsed = time.monotonic() - started
            self.log(f"PASS {scenario['id']} in {elapsed:.2f}s")
            return {"id": scenario["id"], "title": scenario["title"], "status": "passed", "seconds": round(elapsed, 3), "error": None}
        except Exception as exc:
            elapsed = time.monotonic() - started
            self.log(f"FAIL {scenario['id']} in {elapsed:.2f}s: {exc}")
            if ctx.get("rec"):
                try:
                    self.client.request("POST", f"/api/recordings/{ctx['rec']}/stop", {})
                except Exception:
                    pass
            return {
                "id": scenario["id"],
                "title": scenario["title"],
                "status": "failed",
                "seconds": round(elapsed, 3),
                "error": str(exc),
            }

    def _dispatch(self, step, ctx):
        action = step["action"]
        handler = getattr(self, "do_" + action, None)
        if handler is None:
            raise ScenarioError(f"Unknown step action {action}")
        handler(step, ctx)

    def do_health(self, step, ctx):
        _, payload = self.client.ok("GET", "/api/health")
        if payload.get("status") != "ok" or not payload.get("recorder") or not payload.get("executor"):
            raise ScenarioError(f"health is not ready: {payload}")

    def do_diagnostics(self, step, ctx):
        _, payload = self.client.ok("GET", "/api/diagnostics")
        probe = payload.get("write_probe") or {}
        if not probe.get("ok"):
            raise ScenarioError(f"write probe failed: {payload.get('write_probe')}")
        if payload.get("recorder") != "ok" or payload.get("executor") != "ok":
            raise ScenarioError(f"diagnostics degraded: recorder={payload.get('recorder')} executor={payload.get('executor')}")

    def do_expect_http(self, step, ctx):
        status, payload = self.client.request(step["method"], step["path"], step.get("body"))
        if status != step["status"]:
            raise ScenarioError(f"expected HTTP {step['status']} got {status}: {payload}")
        if step.get("detail_contains") and step["detail_contains"].lower() not in str(payload).lower():
            raise ScenarioError(f"detail missing {step['detail_contains']!r}: {payload}")
        if step.get("json_field"):
            if not isinstance(payload, dict) or payload.get(step["json_field"]) != step.get("json_equals"):
                raise ScenarioError(f"JSON field mismatch: {payload}")

    def do_create_project(self, step, ctx):
        _, payload = self.client.ok("POST", "/api/projects", {"name": step["name"], "base_url": step.get("base_url", "")})
        if payload.get("source") != "repository" or not str(payload.get("repository_path", "")).endswith("project.json"):
            raise ScenarioError(f"project was not stored in the repository: {payload}")
        ctx[step["save"]] = payload["id"]

    def do_list_projects(self, step, ctx):
        _, payload = self.client.ok("GET", "/api/projects")
        names = [item.get("name") for item in payload]
        if step["contains_name"] not in names:
            raise ScenarioError(f"{step['contains_name']!r} not in {names}")

    def do_create_variable(self, step, ctx):
        _, payload = self.client.ok("POST", "/api/variables", {
            "project_id": step["project"],
            "name": step["name"],
            "value": step.get("value", ""),
        })
        if payload.get("source") != "repository" or not str(payload.get("repository_path", "")).endswith("variables.json"):
            raise ScenarioError(f"variable was not stored in the repository: {payload}")
        if step.get("save"):
            ctx[step["save"]] = payload["id"]

    def do_patch_variable(self, step, ctx):
        _, payload = self.client.ok("PATCH", f"/api/variables/{step['variable']}", {"value": step["value"]})
        if payload.get("value") != step["value"]:
            raise ScenarioError(f"variable not updated: {payload}")

    def do_delete_variable(self, step, ctx):
        self.client.ok("DELETE", f"/api/variables/{step['variable']}")

    def do_create_recording(self, step, ctx):
        _, payload = self.client.ok("POST", "/api/recordings", {
            "project_id": step["project"],
            "name": step["name"],
            "start_url": step.get("start_url", "/demo.html"),
        })
        ctx[step["save"]] = payload["id"]
        if payload.get("source") != "repository" or not str(payload.get("repository_path", "")).endswith("recording.json"):
            raise ScenarioError(f"recording was not stored in the repository: {payload}")
        if step.get("expect_start_url") and payload.get("start_url") != step["expect_start_url"]:
            raise ScenarioError(f"start_url {payload.get('start_url')!r} != {step['expect_start_url']!r}")
        if step.get("expect_start_url_contains") and step["expect_start_url_contains"] not in (payload.get("start_url") or ""):
            raise ScenarioError(f"start_url {payload.get('start_url')!r} missing {step['expect_start_url_contains']!r}")

    def do_start_session(self, step, ctx):
        self.client.ok("POST", f"/api/recordings/{step['recording']}/session", {})

    def do_session_status(self, step, ctx):
        _, payload = self.client.ok("GET", f"/api/recordings/{step['recording']}/session")
        if step.get("expect_status") and payload.get("status") != step["expect_status"]:
            raise ScenarioError(f"session status {payload.get('status')!r} != {step['expect_status']!r}")

    def do_wait_frame(self, step, ctx):
        deadline = time.time() + step.get("timeout", 40)
        last = None
        while time.time() < deadline:
            _, session = self.client.ok("GET", f"/api/recordings/{step['recording']}/session")
            if session.get("error"):
                raise ScenarioError("browser failed to start: " + session["error"])
            status, blob = self.client.request("GET", f"/api/recordings/{step['recording']}/frame?t={time.time()}")
            last = status if not isinstance(blob, (bytes, bytearray)) else f"{status} {len(blob)} bytes"
            if status == 200 and isinstance(blob, (bytes, bytearray)) and len(blob) >= step.get("min_bytes", 1):
                size = jpeg_size(blob)
                if step.get("min_width") and (not size or size[0] < step["min_width"]):
                    raise ScenarioError(f"frame width {size} < {step['min_width']}")
                if step.get("min_height") and (not size or size[1] < step["min_height"]):
                    raise ScenarioError(f"frame height {size} < {step['min_height']}")
                self.log(f"    frame {len(blob)} bytes {size}")
                return
            time.sleep(0.3)
        raise ScenarioError(f"no browser frame arrived (last {last})")

    def do_input(self, step, ctx):
        _, payload = self.client.ok("POST", f"/api/recordings/{step['recording']}/input", step["body"])
        recorded = payload.get("step") or {}
        if step.get("expect_action") and recorded.get("action") != step["expect_action"]:
            raise ScenarioError(f"recorded action {recorded.get('action')!r} != {step['expect_action']!r}")
        if step.get("expect_selector"):
            primary = (recorded.get("selector") or {}).get("primary")
            if primary != step["expect_selector"]:
                raise ScenarioError(f"selector {primary!r} != {step['expect_selector']!r}")
        if step.get("expect_value") is not None and recorded.get("value") != step["expect_value"]:
            raise ScenarioError(f"value {recorded.get('value')!r} != {step['expect_value']!r}")

    def do_assert_actions(self, step, ctx):
        _, payload = self.client.ok("GET", f"/api/recordings/{step['recording']}")
        actions = [item.get("action") for item in payload.get("steps") or []]
        if actions != step["actions"]:
            raise ScenarioError(f"actions {actions} != {step['actions']}")

    def do_stop_session(self, step, ctx):
        self.client.ok("POST", f"/api/recordings/{step['recording']}/stop", {})

    def do_queue_run(self, step, ctx):
        _, payload = self.client.ok("POST", "/api/runs", {"target_id": step["recording"]})
        ctx[step["save"]] = payload["run_id"]

    def do_wait_run(self, step, ctx):
        deadline = time.time() + step.get("timeout", 60)
        last = None
        while time.time() < deadline:
            _, payload = self.client.ok("GET", f"/api/runs/{step['run']}")
            last = payload
            if payload.get("status") in {"passed", "failed", "error", "failed_audited"}:
                break
            time.sleep(0.4)
        else:
            raise ScenarioError(f"run did not finish: {last}")
        if last.get("status") != step["expect_status"]:
            raise ScenarioError(last.get("rog_monitor_log") or f"status {last.get('status')}")
        excerpts = " | ".join((item.get("excerpt") or "") for item in (last.get("log") or []))
        blob = (last.get("rog_monitor_log") or "") + " " + excerpts
        if step.get("excerpt_contains") and step["excerpt_contains"] not in excerpts:
            raise ScenarioError(f"excerpt missing {step['excerpt_contains']!r}: {excerpts[:400]}")
        if step.get("error_contains") and step["error_contains"].lower() not in blob.lower():
            raise ScenarioError(f"error missing {step['error_contains']!r}: {blob[:400]}")

    def do_assert_live_frame(self, step, ctx):
        status, blob = self.client.request("GET", f"/api/runs/{step['run']}/live.jpg")
        if status != 200 or not isinstance(blob, (bytes, bytearray)) or len(blob) < step.get("min_bytes", 1):
            raise ScenarioError(f"live frame missing ({status}, {type(blob).__name__})")

    def do_library_contains(self, step, ctx):
        _, payload = self.client.ok("GET", "/api/library")
        if payload.get("source") != "repository":
            raise ScenarioError(f"library source is not the repository: {payload}")
        projects = payload.get("projects") or []
        match = next((item for item in projects if item.get("id") == step["project_id"]), None)
        if match is None:
            raise ScenarioError(f"project {step['project_id']} not in repository library")
        if step.get("variable") and step["variable"] not in (match.get("variables") or []):
            raise ScenarioError(f"variable {step['variable']} missing from {match.get('variables')}")
        if step.get("recording_id"):
            recordings = match.get("recordings") or []
            recording = next((item for item in recordings if item.get("id") == step["recording_id"]), None)
            if recording is None:
                raise ScenarioError(f"recording {step['recording_id']} missing from repository library")
            for resource in step.get("resources") or []:
                if resource not in (recording.get("resources") or []):
                    raise ScenarioError(f"resource {resource} missing from {recording}")

    def do_assert_repository(self, step, ctx):
        _, payload = self.client.ok("GET", f"/api/recordings/{step['recording']}")
        if payload.get("source") != "repository" or not str(payload.get("repository_path", "")).endswith("recording.json"):
            raise ScenarioError(f"recording is not in the repository: {payload}")
        self.do_library_contains({
            "project_id": payload["project_id"],
            "recording_id": payload["id"],
            "resources": step.get("resources") or ["Jenkinsfile"],
        }, ctx)

    def do_jenkins_contains(self, step, ctx):
        status, payload = self.client.request("GET", f"/api/recordings/{step['recording']}/jenkins")
        text = payload if isinstance(payload, str) else payload.decode(errors="replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        if status != 200 or step["text"] not in text:
            raise ScenarioError(f"jenkins script missing {step['text']!r}: {text[:200]}")
