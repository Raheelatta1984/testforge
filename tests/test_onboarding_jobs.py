"""Background AI onboarding: the loop runs until every step validates, or until stopped.

The Anthropic transport is faked (see tests/test_ai_onboarding.py); validation,
per-step repair, budgets, the worker thread and the HTTP endpoints are real.
"""
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from tests.bootstrap import ensure_test_env
ensure_test_env()

from fastapi.testclient import TestClient

from app import ai_onboarding, onboarding_jobs
from app.ai_onboarding import AIPermanentError, Budget, onboard_until_valid, validate_steps
from app.main import app
from app.script_import import parse_file
from tests.test_ai_onboarding import FakeAI, GOOD_STEPS, LOOPY_PY, api_reply

BAD_STEP = {"action": "fill_form", "selector": {"kind": "label", "primary": "Name"}, "value": "{{username}}"}
BAD_ROUND = {"name": "Greeting flow", "start_url": "{{base_url}}/login",
             "steps": [{"action": "navigate", "selector": {}, "value": "{{base_url}}/login"}, BAD_STEP]}


def wait_done(client, job_id, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/imports/onboard/" + job_id).json()
        if snap["done"]:
            return snap
        time.sleep(0.05)
    raise AssertionError("onboarding job did not finish: %s" % snap)


class LoopTests(unittest.TestCase):
    def setUp(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def run_loop(self, answers, budget=None):
        static = parse_file("loopy_test.py", LOOPY_PY.encode())
        fake = FakeAI(answers)
        with patch.object(ai_onboarding, "_post", fake):
            result = onboard_until_valid("loopy_test.py", LOOPY_PY, static, budget or Budget())
        return result, fake

    def test_loop_repeats_the_whole_script_until_every_step_validates(self):
        # Round 1 proposes one step that cannot be repaired (3 fix attempts, all still invalid);
        # round 2 converts the whole script correctly. The loop must not stop after round 1.
        answers = [api_reply(BAD_ROUND)] + [api_reply(BAD_STEP)] * 3 + [api_reply(GOOD_STEPS)]
        result, fake = self.run_loop(answers)
        self.assertTrue(result["can_save"], result["diagnostics"])
        self.assertEqual(result["ai"]["onboarding"]["status"], "passed")
        self.assertEqual(result["ai"]["onboarding"]["percent"], 100)
        self.assertEqual(len(result["steps"]), 4)
        self.assertEqual(result["ai"]["onboarding"]["rounds"], 2)
        # The second conversion was told exactly which step failed and why.
        self.assertIn("Round 1 validated 1 of 2 step(s)", fake.prompts[4])
        self.assertIn("fill_form", fake.prompts[4])

    def test_stop_ends_the_loop_and_is_never_saveable(self):
        calls = {"n": 0}
        budget = None

        def stop_after_first_call():
            return calls["n"] >= 1

        budget = Budget(should_stop=stop_after_first_call)
        fake = FakeAI([api_reply(BAD_ROUND)] + [api_reply(BAD_STEP)] * 10)
        original = fake.__call__

        def counting(payload, key):
            calls["n"] += 1
            return original(payload, key)

        static = parse_file("loopy_test.py", LOOPY_PY.encode())
        with patch.object(ai_onboarding, "_post", counting):
            result = onboard_until_valid("loopy_test.py", LOOPY_PY, static, budget)
        self.assertEqual(result["ai"]["onboarding"]["status"], "stopped")
        self.assertFalse(result["can_save"])
        self.assertTrue(any("stopped" in d["message"] and d["severity"] == "error" for d in result["diagnostics"]))
        self.assertEqual(calls["n"], 1)

    def test_permanent_refusal_stops_at_once(self):
        result, fake = self.run_loop([AIPermanentError("AI service refused the request (HTTP 401): invalid x-api-key")])
        self.assertEqual(result["ai"]["onboarding"]["status"], "failed")
        self.assertFalse(result["can_save"])
        self.assertEqual(len(fake.prompts), 1)

    def test_round_limit_reports_incomplete_not_passed(self):
        with patch.object(ai_onboarding, "JOB_MAX_ROUNDS", 2):
            answers = ([api_reply(BAD_ROUND)] + [api_reply(BAD_STEP)] * 3) * 2
            result, _ = self.run_loop(answers)
        self.assertEqual(result["ai"]["onboarding"]["status"], "round-limit")
        self.assertFalse(result["can_save"])
        self.assertTrue(any("round limit" in d["message"] for d in result["diagnostics"]))
        self.assertTrue(result["ai"]["onboarding"]["failing"])

    def test_failed_step_is_reported_with_reason_not_silently_dropped(self):
        # Regression: a step whose last repair was still invalid used to vanish
        # without any failure being recorded.
        trace = []
        budget = Budget()
        fake = FakeAI([api_reply(BAD_STEP)] * 3)
        with patch.object(ai_onboarding, "_post", fake):
            accepted, failures = validate_steps([BAD_STEP], "src", budget, trace)
        self.assertEqual(accepted, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("still invalid", failures[0]["reason"])
        self.assertEqual(budget.step_fixes, 3)

    def test_empty_proposals_twice_end_as_no_steps(self):
        result, _ = self.run_loop([api_reply({"steps": []}), api_reply({"steps": []})])
        self.assertEqual(result["ai"]["onboarding"]["status"], "no-steps")
        self.assertFalse(result["can_save"])


class JobEndpointTests(unittest.TestCase):
    def setUp(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_text_job_runs_to_passed_and_reports_progress(self):
        fake = FakeAI([api_reply(GOOD_STEPS)])
        with patch.object(ai_onboarding, "_post", fake), TestClient(app) as client:
            started = client.post("/api/imports/onboard", json={"mode": "text", "content": LOOPY_PY,
                                                                 "filename": "loopy_test.py"})
            self.assertEqual(started.status_code, 202, started.text)
            snap = wait_done(client, started.json()["job_id"])
        self.assertEqual(snap["status"], "passed", snap)
        self.assertTrue(snap["result"]["can_save"])
        self.assertEqual(snap["progress"]["percent"], 100)
        self.assertEqual(snap["result"]["input_method"], "paste")
        self.assertFalse(snap["result"]["source_executed"])

    def test_stop_endpoint_ends_a_running_loop_with_partial_result(self):
        gate = threading.Event()

        def slow_invalid(payload, key):
            # Every answer is invalid, so the loop would run on; each call waits briefly.
            gate.wait(0.3)
            if str(payload.get("system", "")).startswith("You fix one"):
                return api_reply(BAD_STEP)
            return api_reply(BAD_ROUND)

        with patch.object(ai_onboarding, "_post", slow_invalid), TestClient(app) as client:
            started = client.post("/api/imports/onboard", json={"mode": "text", "content": LOOPY_PY,
                                                                 "filename": "loopy_test.py"})
            job_id = started.json()["job_id"]
            for _ in range(100):
                if client.get("/api/imports/onboard/" + job_id).json()["progress"].get("calls", 0) >= 2:
                    break
                time.sleep(0.05)
            stop = client.post("/api/imports/onboard/%s/stop" % job_id, json={})
            self.assertEqual(stop.status_code, 200)
            snap = wait_done(client, job_id)
        self.assertEqual(snap["status"], "stopped", snap)
        self.assertFalse(snap["result"]["can_save"])
        self.assertLess(snap["progress"]["calls"], 300)

    def test_second_job_is_refused_while_one_runs(self):
        gate = threading.Event()

        def hold(payload, key):
            gate.wait(5)
            return api_reply(BAD_ROUND)

        with patch.object(ai_onboarding, "_post", hold), TestClient(app) as client:
            first = client.post("/api/imports/onboard", json={"mode": "text", "content": LOOPY_PY,
                                                               "filename": "loopy_test.py"})
            second = client.post("/api/imports/onboard", json={"mode": "text", "content": LOOPY_PY,
                                                                "filename": "loopy_test.py"})
            gate.set()
            client.post("/api/imports/onboard/%s/stop" % first.json()["job_id"], json={})
            wait_done(client, first.json()["job_id"])
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)

    def test_job_needs_the_key_and_says_so(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with TestClient(app) as client:
            response = client.post("/api/imports/onboard", json={"mode": "text", "content": LOOPY_PY})
        self.assertEqual(response.status_code, 409)
        self.assertIn("ANTHROPIC_API_KEY", response.json()["detail"])

    def test_unknown_job_is_404(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/imports/onboard/does-not-exist").status_code, 404)
            self.assertEqual(client.post("/api/imports/onboard/does-not-exist/stop", json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
