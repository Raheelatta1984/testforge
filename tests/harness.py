"""Run the unit suite and the library scenarios, then write a timestamped log.

Usage, from the repository root:

    python -m tests.harness

Results are written to logs/<YYYYMMDD-HHMMSS>/ and indexed in logs/index.md.
Exit status is 0 only when every executed scenario passed. A missing browser
skips browser scenarios instead of failing the unit suite; pass --require-browser
to make that a failure.
"""

import argparse
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.bootstrap import ensure_test_env


def now():
    return datetime.now().astimezone()


def stamp():
    return now().strftime("%Y%m%d-%H%M%S")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def discover_chromium(log):
    """Use an explicit binary, Playwright's browser, or the local sandbox build."""
    explicit = os.environ.get("TF_CHROMIUM_PATH", "").strip()
    if explicit and os.path.isfile(explicit):
        log(f"chromium: {explicit}")
        return True
    sandbox = Path("/tmp/cr/chromium")
    if sandbox.is_file():
        os.environ["TF_CHROMIUM_PATH"] = str(sandbox)
        libs = os.environ.get("TF_CHROMIUM_LIBS", "").strip()
        if not libs and Path("/tmp/cr/al2023/lib").is_dir():
            os.environ["TF_CHROMIUM_LIBS"] = "/tmp/cr/al2023/lib:/tmp/cr/al2/lib"
        log(f"chromium: discovered {sandbox}")
        return True
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            path = playwright.chromium.executable_path
        if path and os.path.isfile(path):
            log(f"chromium: bundled {path}")
            return True
        log(f"chromium: bundled path missing ({path})")
    except Exception as exc:
        log(f"chromium: not available ({exc})")
    return False


class Log:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")

    def __call__(self, message):
        line = f"{now().isoformat(timespec='seconds')} {message}"
        print(line, flush=True)
        self.handle.write(line + "\n")
        self.handle.flush()

    def close(self):
        self.handle.close()


def git_rev():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def run_unit_tests(log, unit_log_path):
    log("unit: starting unittest suite")
    ensure_test_env()
    stream = io.StringIO()
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_unit")
    # The runner drops each test from the suite after it runs, so snapshot first.
    cases = [(test, test.id()) for test in _walk(suite)]
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    text = stream.getvalue()
    Path(unit_log_path).write_text(text, encoding="utf-8")
    log("unit: " + text.replace("\n", "\n           "))
    failed = {test.id(): err for test, err in list(result.failures) + list(result.errors)}
    skipped = {test.id(): reason for test, reason in result.skipped}
    rows = []
    for test, full_id in cases:
        name = full_id.split(".")[-1]
        scenario_id = "-".join(name[len("test_"):].split("_")[:3]) if name.startswith("test_") else name
        if full_id in failed:
            err = failed[full_id]
            status, error = "failed", (err.splitlines()[-1][:500] if err else "failed")
        elif full_id in skipped:
            status, error = "skipped", str(skipped[full_id])[:300]
        else:
            status, error = "passed", None
        rows.append({"id": scenario_id, "title": name, "layer": "unit", "status": status, "seconds": None, "error": error})
    log(f"unit: {result.testsRun} ran, {len(result.failures)} failed, {len(result.errors)} errors")
    return rows


def _walk(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _walk(item)
        else:
            yield item


def wait_health(base, proc, server_log, log, timeout=30):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited early:\n" + Path(server_log).read_text(errors="replace")[-2000:])
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as resp:
                payload = json.loads(resp.read().decode())
            if payload.get("recorder") and payload.get("executor"):
                log(f"server: healthy {payload}")
                return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("server did not become healthy:\n" + Path(server_log).read_text(errors="replace")[-2000:])


def run_library(log, run_dir, server_log, require_browser):
    from tests.library_runner import LibraryClient, ScenarioRunner

    catalog = json.loads((ROOT / "tests" / "scenarios.json").read_text())
    have_browser = discover_chromium(log)
    port = free_port()
    work = Path(tempfile.mkdtemp(prefix="tf-library-"))
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["DATABASE_URL"] = "sqlite:///" + str(work / "library.db")
    env["TF_ARTIFACTS"] = str(work / "artifacts")
    env.pop("LD_LIBRARY_PATH", None)
    log(f"server: starting on 127.0.0.1:{port}")
    handle = open(server_log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    rows = []
    try:
        wait_health(f"http://127.0.0.1:{port}", proc, server_log, log)
        client = LibraryClient(f"http://127.0.0.1:{port}", log)
        runner = ScenarioRunner(client, log)
        for scenario in catalog["scenarios"]:
            needs_browser = scenario.get("requires") == "browser"
            if needs_browser and not have_browser:
                message = "Chromium is not installed; scenario skipped"
                log(f"SKIP {scenario['id']} {message}")
                status = "failed" if require_browser else "skipped"
                row = {"id": scenario["id"], "title": scenario["title"], "layer": "library", "status": status, "seconds": 0, "error": message}
                rows.append(row)
                continue
            row = runner.run(scenario)
            row["layer"] = "library"
            rows.append(row)
            for covered in scenario.get("also_covers", []):
                rows.append({
                    "id": covered,
                    "title": scenario["title"] + " (covered)",
                    "layer": "library",
                    "status": row["status"],
                    "seconds": row["seconds"],
                    "error": row["error"],
                })
        return rows
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()
        shutil.rmtree(work, ignore_errors=True)
        log("server: stopped")


def write_reports(run_dir, rows, started, finished, revision):
    passed = sum(1 for row in rows if row["status"] == "passed")
    failed = sum(1 for row in rows if row["status"] == "failed")
    skipped = sum(1 for row in rows if row["status"] == "skipped")
    summary = {
        "started": started,
        "finished": finished,
        "revision": revision,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": len(rows),
        "ok": failed == 0,
        "scenarios": rows,
    }
    (run_dir / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = [
        f"# TestForge harness report",
        "",
        f"- Started: {started}",
        f"- Finished: {finished}",
        f"- Revision: {revision}",
        f"- Python: {summary['python']}",
        f"- Result: {'PASS' if summary['ok'] else 'FAIL'} ({passed} passed, {failed} failed, {skipped} skipped)",
        "",
        "| ID | Layer | Status | Seconds | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        detail = (row.get("error") or "").replace("|", "/").replace("\n", " ")[:180]
        seconds = "" if row.get("seconds") is None else f"{row['seconds']}"
        lines.append(f"| {row['id']} | {row['layer']} | {row['status']} | {seconds} | {detail} |")
    lines.append("")
    (run_dir / "results.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def update_index(logs_dir, run_name, summary):
    index = logs_dir / "index.md"
    if not index.exists():
        index.write_text(
            "# Harness run index\n\n"
            "| Started | Result | Passed | Failed | Skipped | Revision | Folder |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n",
            encoding="utf-8",
        )
    with index.open("a", encoding="utf-8") as handle:
        handle.write(
            f"| {summary['started']} | {'PASS' if summary['ok'] else 'FAIL'} | "
            f"{summary['passed']} | {summary['failed']} | {summary['skipped']} | "
            f"{summary['revision']} | [{run_name}]({run_name}/results.md) |\n"
        )
    (logs_dir / "LATEST.txt").write_text(run_name + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the TestForge test harness")
    parser.add_argument("--require-browser", action="store_true", help="Fail if Chromium is missing")
    parser.add_argument("--unit-only", action="store_true", help="Skip the live library scenarios")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    run_name = stamp()
    logs_dir = ROOT / "logs"
    run_dir = logs_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    log = Log(run_dir / "harness.log")
    started = now().isoformat(timespec="seconds")
    revision = git_rev()
    log(f"harness: start {run_name} revision {revision}")
    log(f"harness: python {platform.python_version()} {platform.platform()}")
    rows = []
    try:
        rows.extend(run_unit_tests(log, run_dir / "unit.log"))
        if args.unit_only:
            log("library: skipped (--unit-only)")
        else:
            scenario_log = Log(run_dir / "scenarios.log")
            try:
                rows.extend(run_library(scenario_log, run_dir, run_dir / "server.log", args.require_browser))
            finally:
                scenario_log.close()
    except Exception:
        detail = traceback.format_exc()
        log("harness: aborted\n" + detail)
        rows.append({"id": "HARNESS", "title": "Harness aborted", "layer": "harness", "status": "failed", "seconds": None, "error": detail.splitlines()[-1][:500]})
    finished = now().isoformat(timespec="seconds")
    summary = write_reports(run_dir, rows, started, finished, revision)
    update_index(logs_dir, run_name, summary)
    log(f"harness: {('PASS' if summary['ok'] else 'FAIL')} {summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped")
    log(f"harness: report {run_dir / 'results.md'}")
    log.close()
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
