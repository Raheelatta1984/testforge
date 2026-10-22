#!/usr/bin/env python3
"""The TestForge CI runner: one command from a pipeline to a verdict.

    tf run --plan nightly.json --wait --junit results.xml --json results.json
    tf status <run_id|batch_id>
    tf junit  <run_id|batch_id> -o results.xml
    tf report <run_id> -o run.json

Everything here is a thin client over the HTTP API that already existed: it
queues a run or a batch, waits, then collects the JUnit XML and the JSON report
the server renders. No second engine, no browser, no model, and nothing outside
the standard library, so it works on any CI image that has Python 3.9+.

Exit codes, because a gate is only a gate if it can fail:

* ``0`` everything passed
* ``1`` a test failed, or a run ended in error/cancelled
* ``2`` the command, the connection or the wait was wrong (usage, transport,
  timeout) - a pipeline mistake, not a product failure

Configuration: ``--base`` or ``TF_BASE_URL`` (default http://127.0.0.1:8000),
``--token`` or ``TF_ACCESS_TOKEN`` (sent as a bearer token; only needed when the
deployment sets one).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = os.environ.get("TF_BASE_URL", "http://127.0.0.1:8000")
DEFAULT_TIMEOUT = 30.0
DEFAULT_WAIT_SECONDS = 1800.0
DEFAULT_POLL_SECONDS = 3.0
TERMINAL = {"passed", "healed", "failed", "error", "cancelled", "completed"}
FAILED = {"failed", "healed", "error", "cancelled"}
PROG = "tf"


class TestForgeError(Exception):
    """A pipeline mistake or a transport problem: exit code 2."""


class TestForgeClient:
    """Minimal JSON client for the TestForge API."""

    def __init__(self, base: str | None = None, token: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, opener=None):
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.token = token if token is not None else os.environ.get("TF_ACCESS_TOKEN", "")
        self.timeout = float(timeout)
        self._opener = opener
        self.requests: list[tuple[str, str, dict | None]] = []

    # --- transport ---------------------------------------------------------
    def request(self, method: str, path: str, payload: dict | None = None,
                accept: str = "application/json") -> tuple[int, bytes]:
        self.requests.append((method.upper(), path, payload))
        url = self.base + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": accept}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self._opener is not None:
            return self._opener(method.upper(), url, data, headers, self.timeout)
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise TestForgeError(f"cannot reach {url}: {exc}") from exc

    def json(self, method: str, path: str, payload: dict | None = None) -> dict:
        status, body = self.request(method, path, payload)
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise TestForgeError(f"{path} did not answer with JSON: {exc}") from exc
        if status >= 400:
            detail = parsed.get("detail") if isinstance(parsed, dict) else None
            raise TestForgeError(f"{path} answered {status}: {detail or body[:200]!r}")
        return parsed

    def text(self, method: str, path: str) -> str:
        status, body = self.request(method, path, None, accept="application/xml")
        if status >= 400:
            raise TestForgeError(f"{path} answered {status}: {body[:200]!r}")
        return body.decode("utf-8", errors="replace")

    # --- commands ----------------------------------------------------------
    def run(self, plan: dict) -> dict:
        """Queue what a plan file describes: one run, or a batch."""
        ids = plan.get("recording_ids") or ([plan["recording_id"]] if plan.get("recording_id") else [])
        if not ids:
            raise TestForgeError("the plan needs recording_ids (a list) or recording_id")
        options = {key: plan[key] for key in ("name", "screenshots", "share_session",
                                              "retry_transient", "display_window",
                                              "execution_speed", "variables")
                   if key in plan}
        if len(ids) == 1 and not plan.get("force_batch"):
            payload = {"recording_id": ids[0], **options}
            if plan.get("force"):
                payload["force"] = True
            return {"kind": "run", **self.json("POST", "/api/runs", payload)}
        payload = {"recording_ids": list(ids), **options}
        return {"kind": "batch", **self.json("POST", "/api/runs/batch", payload)}

    def status(self, queued: dict) -> dict:
        kind = queued.get("kind")
        if kind == "batch":
            path = f"/api/runs/batch/{queued['batch_id']}"
        else:
            path = f"/api/runs/{queued['run_id']}"
        return self.json("GET", path)

    def wait(self, queued: dict, timeout: float = DEFAULT_WAIT_SECONDS,
             interval: float = DEFAULT_POLL_SECONDS, sleep=time.sleep) -> dict:
        """Poll until the run or batch reaches a terminal state."""
        deadline = time.monotonic() + max(1.0, float(timeout))
        while True:
            status = self.status(queued)
            if str(status.get("status") or "").lower() in TERMINAL:
                return status
            if time.monotonic() >= deadline:
                raise TestForgeError(f"timed out after {timeout:g}s waiting for "
                                     f"{queued.get('run_id') or queued.get('batch_id')}")
            sleep(min(max(0.5, float(interval)), 30.0))

    def junit(self, queued: dict) -> str:
        if queued.get("kind") == "batch":
            return self.text("GET", f"/api/runs/batch/{queued['batch_id']}/junit.xml")
        return self.text("GET", f"/api/runs/{queued['run_id']}/junit.xml")

    def report(self, queued: dict) -> dict:
        if queued.get("kind") == "batch":
            return self.json("GET", f"/api/runs/batch/{queued['batch_id']}")
        return self.json("GET", f"/api/runs/{queued['run_id']}/report.json")


# --- verdicts --------------------------------------------------------------

def summarise(status: dict) -> dict:
    """One shape for a run row and for a batch report."""
    counters = status.get("counters") if isinstance(status.get("counters"), dict) else status
    numbers = {key: int(counters.get(key) or 0)
               for key in ("passed", "failed", "skipped", "cancelled", "error", "total")}
    if not any(numbers.values()):
        raw = str(status.get("status") or "")
        numbers[raw] = numbers.get(raw, 0) + 1
        numbers["total"] = numbers.get("total", 0) + 1
    state = str(status.get("status") or "").lower()
    verdict = "failed" if state in FAILED or numbers["failed"] or numbers["error"] or numbers["cancelled"] else "passed"
    return {"status": state or "unknown", "verdict": verdict, "counts": numbers}


def load_plan(path: str) -> dict:
    target = Path(path)
    if not target.is_file():
        raise TestForgeError(f"plan file not found: {path}")
    try:
        plan = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TestForgeError(f"plan file is not readable JSON: {exc}") from exc
    if not isinstance(plan, dict):
        raise TestForgeError("plan file must contain a JSON object")
    return plan


def _write(path: str, content: str) -> None:
    target = Path(path)
    if target.parent and str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _print(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message)


# --- CLI -------------------------------------------------------------------

def _client(args) -> TestForgeClient:
    return TestForgeClient(base=getattr(args, "base", None), token=getattr(args, "token", None),
                           timeout=getattr(args, "timeout", DEFAULT_TIMEOUT))


def _quiet(args) -> bool:
    return bool(getattr(args, "quiet", False))


def cmd_run(args) -> int:
    client = _client(args)
    plan = load_plan(args.plan)
    queued = client.run(plan)
    target = queued.get("run_id") or queued.get("batch_id")
    _print(f"queued {queued['kind']} {target}", _quiet(args))
    if not args.wait:
        _print(json.dumps(queued, indent=2, sort_keys=True), _quiet(args))
        return 0
    status = client.wait(queued, timeout=args.wait_seconds, interval=args.poll_seconds)
    summary = summarise(status)
    counts = summary["counts"]
    _print(f"{summary['status']}: passed={counts['passed']} failed={counts['failed']} "
           f"skipped={counts['skipped']} cancelled={counts['cancelled']} error={counts['error']}",
           _quiet(args))
    if args.junit:
        _write(args.junit, client.junit(queued))
        _print(f"junit: {args.junit}", args.quiet)
    if args.json:
        _write(args.json, json.dumps(client.report(queued), indent=2, sort_keys=True))
        _print(f"report: {args.json}", args.quiet)
    return 0 if summary["verdict"] == "passed" else 1


def cmd_status(args) -> int:
    client = _client(args)
    payload = client.json("GET", f"/api/runs/{args.target}")
    _print(json.dumps(summarise(payload), indent=2, sort_keys=True), args.quiet)
    return 0 if summarise(payload)["verdict"] == "passed" else 1


def cmd_junit(args) -> int:
    client = _client(args)
    body = client.text("GET", f"/api/runs/{args.target}/junit.xml")
    if args.out:
        _write(args.out, body)
        _print(f"junit: {args.out}", args.quiet)
    else:
        print(body)
    return 0


def cmd_report(args) -> int:
    client = _client(args)
    payload = client.json("GET", f"/api/runs/{args.target}/report.json")
    body = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        _write(args.out, body)
        _print(f"report: {args.out}", args.quiet)
    else:
        print(body)
    return 0


def _add_shared(parser: argparse.ArgumentParser, suppress: bool = False) -> None:
    """Connection flags, accepted before or after the subcommand.

    On a subparser every default is SUPPRESS so a flag typed after the
    subcommand overrides one typed before it, and an absent flag cannot reset
    what the outer parser already parsed.
    """
    skip = argparse.SUPPRESS if suppress else None
    parser.add_argument("--base", dest="base", default=skip,
                        help="TestForge base URL (or TF_BASE_URL)")
    parser.add_argument("--token", dest="token", default=skip,
                        help="access token, sent as a bearer token (or TF_ACCESS_TOKEN)")
    parser.add_argument("--timeout", dest="timeout", type=float,
                        default=skip if suppress else DEFAULT_TIMEOUT,
                        help="HTTP timeout in seconds (default 30)")
    parser.add_argument("--quiet", dest="quiet", action="store_true", default=skip,
                        help="only print what was written")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    _add_shared(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="queue a plan, wait, and collect the evidence")
    _add_shared(run, suppress=True)
    run.add_argument("--plan", required=True, help="JSON plan: recording_ids plus options")
    run.add_argument("--wait", action="store_true", help="poll until the run finishes")
    run.add_argument("--wait-seconds", type=float, default=DEFAULT_WAIT_SECONDS,
                     help="give up waiting after this long (default 1800)")
    run.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS,
                     help="how often to poll (default 3)")
    run.add_argument("--junit", metavar="PATH", help="write JUnit XML here")
    run.add_argument("--json", metavar="PATH", dest="json_out", help="write the JSON report here")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="the verdict for one run or batch")
    _add_shared(status, suppress=True)
    status.add_argument("target", help="run id or batch id")
    status.set_defaults(func=cmd_status)

    junit = sub.add_parser("junit", help="fetch JUnit XML for one run")
    _add_shared(junit, suppress=True)
    junit.add_argument("target", help="run id")
    junit.add_argument("-o", "--out", help="write to a file instead of stdout")
    junit.set_defaults(func=cmd_junit)

    report = sub.add_parser("report", help="fetch the JSON report for one run")
    _add_shared(report, suppress=True)
    report.add_argument("target", help="run id")
    report.add_argument("-o", "--out", help="write to a file instead of stdout")
    report.set_defaults(func=cmd_report)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # `run --json` writes a file, so argparse's dest is json_out; every command
    # gets a plain `json` attribute so the handlers can read it unconditionally.
    args.json = getattr(args, "json_out", None)
    try:
        return int(args.func(args))
    except TestForgeError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
