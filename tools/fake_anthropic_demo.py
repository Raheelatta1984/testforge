"""A tiny fake Anthropic messages API for the live end-to-end demo.

Returns a working conversion for any convert prompt, and a corrected step for
any single-step fix prompt. Not part of the app; demo tooling only.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

CONVERSION = {
    "name": "Converted login flow",
    "start_url": "{{base_url}}/login",
    "steps": [
        {"action": "navigate", "selector": {}, "value": "{{base_url}}/login", "label": "Open login"},
        {"action": "fill", "selector": {"kind": "label", "primary": "Name"}, "value": "{{username}}", "label": "Enter name"},
        {"action": "click", "selector": {"kind": "role", "primary": "button", "name": "Continue"}, "value": "", "label": "Continue"},
        {"action": "assert_text", "selector": {"kind": "css", "primary": "#out"}, "value": "Hello, {{username}}", "label": "Verify greeting"},
    ],
    "notes": ["The for-loop over users became {{username}}; set it per run."],
}

RUNNER_NOTE = {
    "name": "TestForge replay runner",
    "start_url": "",
    "steps": [],
    "notes": ["This script replays steps.json and contains no literal browser steps; upload steps.json together with the script as a two-file bundle."],
}

FIXED_STEP = {"action": "fill", "selector": {"kind": "label", "primary": "Name"},
              "value": "{{username}}", "label": "Enter name"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        prompt = json.dumps(body.get("messages", []))
        if "corrected step JSON" in prompt:
            answer = FIXED_STEP
        elif "steps.json" in prompt and "runtime.perform" in prompt:
            answer = RUNNER_NOTE
        else:
            answer = CONVERSION
        payload = json.dumps({"content": [{"type": "text", "text": json.dumps(answer)}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", 9999), Handler).serve_forever()
