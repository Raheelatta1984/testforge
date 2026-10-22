"""Locator-healing replay tests use page doubles only; no browser is launched."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.bootstrap import ensure_test_env
ensure_test_env()

from app.audit_trail import AuditTrail
from app.executor import replay_step
from app.locator_healing import (HealingFailure, _ai_re_rank_payload, pin_original,
                                 resolve_locator, is_pinned)
from tests.fakes import FakePage


class LocatorHealingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tf-heal-")
        self.stats = str(Path(self.tmp.name) / "local-stats.json")
        self.env = patch.dict(os.environ, {"TF_LOCATOR_HEAL_STATS": self.stats}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def _candidate():
        return {"tag": "button", "id": "", "testid": "save-order", "role": "button",
                "name": "Save order", "text": "Save order", "label": "", "css": "button.save",
                "xpath": "/html/body/button[1]", "depth": 3, "snippet": "button button Save order"}

    def test_replay_heals_missing_primary_and_records_all_deterministic_candidates(self):
        page = FakePage(known={'[data-testid="save-order"]'})
        page.healing_candidates = [self._candidate()]
        step = {"order": 3, "action": "click", "label": "Click Save order",
                "selector": {"kind": "css", "primary": "#old-save"}}
        result = asyncio.run(replay_step(page, step, {}, runtime=type("Runtime", (), {"healing_context": {
            "app_id": "fixture", "recording_id": "recording-1"}})()))
        self.assertEqual(page.clicks, [("locator", '[data-testid="save-order"]')])
        healed = result["locator_healed"]
        self.assertEqual(healed["original"]["primary"], "#old-save")
        self.assertEqual(healed["winning"]["kind"], "test_id")
        self.assertTrue(any(row["strategy"] == "role_name" for row in healed["candidate_scores"]))
        self.assertTrue(all(row["count"] in (0, 1) for row in healed["candidate_scores"]))
        self.assertTrue(Path(self.stats).exists())
        self.assertEqual(step["selector"]["primary"], "#old-save", "healing must not rewrite capture data")

    def test_action_retries_ranked_unique_candidate_within_the_budget_after_timeout(self):
        primary = '[data-testid="save-order"]'
        role = "role=button|name=Save order"
        page = FakePage(known={primary, role})
        page.healing_candidates = [self._candidate()]
        page.flaky = {primary: 1}
        step = {"order": 9, "action": "click", "label": "Click Save order",
                "selector": {"primary": "#old-save"}}
        result = asyncio.run(replay_step(page, step, {}, budgets={"action_ms": 3000}))
        healed = result["locator_healed"]
        self.assertEqual(page.clicks, [("locator", role)])
        self.assertEqual(healed["winning"]["kind"], "role")
        self.assertEqual(healed["action_attempts"], 2)

    def test_ambiguous_primary_requires_unique_candidate_and_failure_has_ranked_table(self):
        page = FakePage()
        page.counts["#ambiguous"] = 2
        page.healing_candidates = [self._candidate()]
        page.known.add('[data-testid="save-order"]')
        context = {"app_id": "fixture", "recording_id": "r1"}
        healed = asyncio.run(resolve_locator(page, {"order": 2, "action": "click", "label": "Save order",
                                                    "selector": {"primary": "#ambiguous"}}, 3000, context))
        self.assertEqual(healed["winning"]["kind"], "test_id")
        page.known.clear()
        with self.assertRaises(HealingFailure) as caught:
            asyncio.run(resolve_locator(page, {"order": 2, "action": "click", "label": "Save order",
                                               "selector": {"primary": "#ambiguous"}}, 3000, context))
        self.assertTrue(caught.exception.candidates)
        self.assertEqual(caught.exception.candidates[0]["score"], 0)
        self.assertEqual(caught.exception.candidates[0]["rejected"], "not_unique")

    def test_rejected_original_is_pinned_per_app_recording_and_step(self):
        context = {"app_id": "fixture", "recording_id": "r1"}
        pin_original(context, 7)
        self.assertTrue(is_pinned(context, 7))
        self.assertFalse(is_pinned({"app_id": "fixture", "recording_id": "r2"}, 7))
        payload = json.loads(Path(self.stats).read_text())
        self.assertIn("fixture/r1", payload["pins"])

    def test_ai_rerank_hashes_prompt_and_reply_and_receives_only_compact_snippets(self):
        candidate_table = [{"strategy": "role_name", "score": 60, "count": 1,
                            "snippet": "button Save order", "locator": {"kind": "role", "primary": "button", "name": "Save order"}}]
        with patch.dict(os.environ, {"TF_AGENT_AI_PROVIDER": "ollama"}, clear=False), \
             patch("app.qa_agents.ai._ask", return_value='{"index":0}') as ask:
            strategy, info = _ai_re_rank_payload(candidate_table)
        self.assertEqual(strategy, 0)
        self.assertTrue(info["used"])
        self.assertEqual(len(info["prompt_hash"]), 64)
        self.assertEqual(len(info["response_hash"]), 64)
        prompt = ask.call_args.args[1]
        self.assertIn("button Save order", prompt)
        self.assertNotIn("password", prompt.lower())
        self.assertNotIn("<html", prompt.lower())

    def test_locator_healed_event_is_hash_chained(self):
        folder = Path(self.tmp.name) / "run-a"
        trail = AuditTrail(folder, "run-a")
        trail.append("run_started", {"steps": 1})
        trail.append("locator_healed", {"order": 1, "strategy": "test_id", "candidate_scores": [{"score": 85}]})
        trail.append("run_finished", {"status": "passed"})
        result = trail.verify()
        self.assertTrue(result["ok"])
        self.assertTrue(result["complete"])
        self.assertEqual(trail.read()[1]["event"], "locator_healed")

    def test_execute_run_marks_healing_as_review_required_not_clean_pass(self):
        from app.config import ARTIFACTS
        from app.db import Project, Recording, RecordingStep, Run, SessionLocal, init_db
        init_db()
        import app.executor as executor
        from tests.fakes import FakePage, FakePlaywright
        page = FakePage(known={'[data-testid="save-order"]'})
        page.healing_candidates = [self._candidate()]
        with SessionLocal() as db:
            project = Project(name="heal-release-gate", base_url="")
            db.add(project)
            db.commit()
            db.refresh(project)
            recording = Recording(project_id=project.id, name="healing release gate",
                                  start_url="http://127.0.0.1/demo.html")
            db.add(recording)
            db.commit()
            db.refresh(recording)
            db.add(RecordingStep(recording_id=recording.id, order=1, action="click", label="Save order",
                                 selector={"primary": "#old-save"}))
            db.commit()
            run = Run(recording_id=recording.id, status="queued")
            db.add(run)
            db.commit()
            db.refresh(run)
            run_id = run.id
        original = executor.async_playwright
        executor.async_playwright = lambda: FakePlaywright(page)
        events = []
        async def on_event(event):
            events.append(event)
        try:
            asyncio.run(executor.execute_run(run_id, on_event, display_window=False))
        finally:
            executor.async_playwright = original
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            self.assertEqual(run.status, "healed")
            self.assertTrue(run.execution_log[0]["healed"])
        done = next(event for event in events if event.get("type") == "done")
        self.assertEqual(done["release_gate"], "review_required")
        trail = AuditTrail(Path(ARTIFACTS) / "runs" / run_id, run_id)
        self.assertTrue(trail.verify()["ok"])
        self.assertIn("locator_healed", [row["event"] for row in trail.read()])
        from tools.tf import summarise
        self.assertEqual(summarise({"status": "healed"})["verdict"], "failed")
        from app.ci_results import junit_xml, summary
        ci = summary([{"id": "run-healed", "status": "healed"}])
        self.assertEqual(ci["verdict"], "failed")
        self.assertEqual(ci["exit_code"], 1)
        self.assertIn("review the recording", junit_xml([{"id": "run-healed", "status": "healed"}]))

    def test_library_review_accept_rewrites_and_reject_pins_with_audit_events(self):
        from app import main
        from app.db import Project, Recording, RecordingStep, Run, SessionLocal
        from app.locator_healing import is_pinned
        reviewed = []
        for decision in ("accept", "reject"):
            with SessionLocal() as db:
                project = Project(name="heal-review-" + decision, base_url="")
                db.add(project)
                db.commit()
                db.refresh(project)
                recording = Recording(project_id=project.id, name="review " + decision,
                                      start_url="http://127.0.0.1/demo.html")
                db.add(recording)
                db.commit()
                db.refresh(recording)
                step = RecordingStep(recording_id=recording.id, order=4, action="click", label="Save",
                                     selector={"kind": "css", "primary": "#old"})
                db.add(step)
                run = Run(recording_id=recording.id, status="healed", execution_log=[{
                    "order": 4, "healed": True, "original_locator": {"kind": "css", "primary": "#old"},
                    "winning_locator": {"kind": "test_id", "primary": "save"}}])
                db.add(run)
                db.commit()
                db.refresh(run)
                rid, run_id, project_id = recording.id, run.id, project.id
            run_dir = Path(__import__("app.config", fromlist=["ARTIFACTS"]).ARTIFACTS) / "runs" / run_id
            trail = AuditTrail(run_dir, run_id)
            trail.append("run_started", {"steps": 1})
            trail.append("run_finished", {"status": "healed"})
            if decision == "accept":
                with patch.object(main.library_store, "export_recording", return_value={}):
                    result = asyncio.run(main.review_locator_heal(rid, run_id, 4, {"decision": decision}))
                with SessionLocal() as db:
                    self.assertEqual(db.query(RecordingStep).filter_by(recording_id=rid).one().selector["kind"], "test_id")
            else:
                result = asyncio.run(main.review_locator_heal(rid, run_id, 4, {"decision": decision}))
                self.assertTrue(is_pinned({"app_id": project_id, "recording_id": rid}, 4))
            self.assertTrue(result["ok"])
            checked = AuditTrail(run_dir, run_id).verify()
            self.assertTrue(checked["ok"])
            self.assertTrue(checked["complete"])
            self.assertEqual(AuditTrail(run_dir, run_id).read()[-1]["event"], "locator_heal_review")
            reviewed.append(result["decision"])
        self.assertEqual(reviewed, ["accept", "reject"])


if __name__ == "__main__":
    unittest.main()
