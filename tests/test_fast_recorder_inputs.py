"""Recorder START and the Inputs screen answer at once, and show what was just saved.

* START must not wait for the GitHub push: the recording is created from files and
  the push runs afterwards, in the background.
* The Inputs endpoint must list variables saved a moment ago (for example from an
  AI recording), mark the ones the recording uses, and not scan every recording.
"""
import json
import unittest
from unittest.mock import patch

from tests.bootstrap import ensure_test_env
ensure_test_env()

from fastapi.testclient import TestClient

from app import library_store
from app.main import app


class FastStartTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.client.__enter__()
        project = library_store.create_project("fast-start-project", "")
        self.project_id = project["id"]

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_create_recording_does_not_publish_before_returning(self):
        calls = []

        def spy(message):
            calls.append(message)
            return True

        with patch.object(library_store, "_finish", spy):
            response = self.client.post("/api/recordings", json={
                "project_id": self.project_id, "name": "Fast start", "start_url": "/demo.html"})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(calls, [], "START must not wait for a publish")
        self.assertEqual(body["publish_state"], "pending")
        self.assertFalse(body["published"])
        # The recording is already usable: it is in the catalog and on disk.
        self.assertIsNotNone(library_store.get_recording(body["id"]))

    def test_library_create_default_still_publishes_synchronously(self):
        # Other callers (imports, scripts) keep the original, immediate publish.
        calls = []
        with patch.object(library_store, "_finish", lambda message: calls.append(message) or True):
            library_store.create_recording(self.project_id, "Sync", "/demo.html")
        self.assertEqual(len(calls), 1)


class InputsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.client.__enter__()
        project = library_store.create_project("inputs-project", "")
        self.project_id = project["id"]
        recording = library_store.create_recording(self.project_id, "Login", "/login", steps=[
            {"order": 1, "action": "navigate", "value": "https://example.test/login", "label": "Open"},
            {"order": 2, "action": "fill", "selector": {"kind": "label", "primary": "User"},
             "value": "{{username}}", "label": "Type user"},
        ])
        self.recording_id = recording["id"]

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_variable_saved_just_now_is_listed_and_marked_used(self):
        self.client.post("/api/variables", json={"project_id": self.project_id, "name": "username",
                                                  "value": "ada", "category": "General"})
        data = self.client.get("/api/recordings/%s/inputs" % self.recording_id).json()
        names = [item["name"] for item in data["project_variables"]]
        self.assertIn("username", names)
        self.assertEqual(data["used_names"], ["username"])
        self.assertEqual(data["missing_names"], [])
        # The used variable is listed first.
        self.assertEqual(names[0], "username")

    def test_new_variable_appears_without_a_cache_wait(self):
        first = self.client.get("/api/recordings/%s/inputs" % self.recording_id).json()
        self.client.post("/api/variables", json={"project_id": self.project_id, "name": "password",
                                                  "value": "x", "category": "Parameter"})
        second = self.client.get("/api/recordings/%s/inputs" % self.recording_id).json()
        self.assertNotIn("password", [v["name"] for v in first["project_variables"]])
        self.assertIn("password", [v["name"] for v in second["project_variables"]])

    def test_name_used_by_recording_but_not_saved_is_reported_missing(self):
        data = self.client.get("/api/recordings/%s/inputs" % self.recording_id).json()
        self.assertEqual(data["used_names"], ["username"])
        self.assertEqual(data["missing_names"], ["username"])

    def test_secret_values_are_never_returned(self):
        self.client.post("/api/variables", json={"project_id": self.project_id, "name": "token",
                                                  "value": "s3cret", "is_secret": True})
        data = self.client.get("/api/recordings/%s/inputs" % self.recording_id).json()
        token = next(v for v in data["project_variables"] if v["name"] == "token")
        self.assertEqual(token["value"], "")
        self.assertTrue(token["is_secret"])
        self.assertNotIn("s3cret", json.dumps(data))

    def test_save_defaults_returns_before_publish(self):
        calls = []
        with patch.object(library_store, "_finish", lambda message: calls.append(message) or True):
            response = self.client.put("/api/recordings/%s/inputs" % self.recording_id,
                                       json={"variables": {"username": "grace"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, [], "SAVE RECORDING DEFAULTS must not wait for GitHub")
        self.assertEqual(response.json()["publish_state"], "pending")
        self.assertEqual(response.json()["defaults"]["username"], "grace")


if __name__ == "__main__":
    unittest.main()
