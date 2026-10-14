"""Installing a custom desktop application before recording it.

Covers the validation rules, the simulator path (which is what CI runs), the
agent dispatch contract, and the end-to-end flow: install, then start a desktop
recording against the installed application. Scenario IDs are documented in
tests/SCENARIOS.md.
"""
import unittest
from unittest.mock import patch

from tests.bootstrap import ensure_test_env

ensure_test_env()

from fastapi.testclient import TestClient

from app import desktop_install
from app.desktop_runner import (DESKTOP_ACTIONS, DesktopSimulator, validate_custom_app,
                                validate_desktop_selector)
from app.main import app

MSI = "C:\\Installers\\NorthwindOrders.msi"


def install_body(**overrides):
    body = {
        "name": "Northwind Orders",
        "installer_kind": "msi",
        "installer": MSI,
        "expected_process": "Orders.exe",
        "expected_window_title": "Northwind Orders",
        "framework": "winforms",
        "kind": "custom",
        "version": "4.2.1",
    }
    body.update(overrides)
    return body


class DesktopInstallTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        for row in desktop_install.installed_apps():
            desktop_install.remove_application(row["app_id"])

    def tearDown(self):
        for row in desktop_install.installed_apps():
            desktop_install.remove_application(row["app_id"])

    def test_UT_DSKINS_01_install_registers_the_application(self):
        """A successful install is recorded and the app becomes selectable."""
        response = self.client.post("/api/desktop/installs", json=install_body())
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        receipt = payload["install"]
        self.assertEqual(receipt["status"], "installed")
        self.assertEqual(receipt["mode"], "simulator")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["app_id"], "northwind-orders")
        self.assertTrue(receipt["install_log"])
        self.assertEqual(payload["app"]["id"], "northwind-orders")

        listed = self.client.get("/api/desktop/installs").json()
        self.assertEqual([row["app_id"] for row in listed["installs"]], ["northwind-orders"])
        self.assertEqual(listed["mode"], "simulator")
        self.assertEqual(sorted(listed["installer_kinds"]),
                         ["choco", "exe", "msi", "msix", "portable", "winget"])

        apps = {app_row["id"]: app_row for app_row in self.client.get("/api/desktop/apps").json()["apps"]}
        self.assertTrue(apps["northwind-orders"]["installed"])
        self.assertEqual(apps["northwind-orders"]["install"]["status"], "installed")

    def test_UT_DSKINS_02_command_is_built_from_the_installer_kind(self):
        """The command comes from the template, with the operator's extras appended."""
        receipt = self.client.post("/api/desktop/installs",
                                   json=install_body(install_args="/quiet")).json()["install"]
        self.assertEqual(receipt["command"], f'msiexec.exe /i "{MSI}" /qn /norestart /quiet')
        exe = self.client.post("/api/desktop/installs",
                               json=install_body(name="Legacy Client", installer_kind="exe",
                                                 installer="C:\\Installers\\LegacyClient.exe")).json()["install"]
        self.assertEqual(exe["command"], '"C:\\Installers\\LegacyClient.exe" /S')

    def test_UT_DSKINS_03_shell_metacharacters_are_refused(self):
        """Nothing that a shell would interpret reaches the Windows agent."""
        for installer in (MSI + " & del /f /q C:\\", MSI + " | more", MSI + "; shutdown /s"):
            with self.subTest(installer=installer):
                response = self.client.post("/api/desktop/installs", json=install_body(installer=installer))
                self.assertEqual(response.status_code, 422)
                self.assertIn("shell metacharacters", response.json()["detail"])
        chained = self.client.post("/api/desktop/installs", json=install_body(
            install_command='msiexec.exe /i "C:\\a.msi" /qn & calc.exe'))
        self.assertEqual(chained.status_code, 422)
        unknown = self.client.post("/api/desktop/installs", json=install_body(
            install_command='powershell -c "iwr http://x"'))
        self.assertEqual(unknown.status_code, 422)
        self.assertIn("must start with msiexec", unknown.json()["detail"])

    def test_UT_DSKINS_04_installer_must_be_an_absolute_path_or_https(self):
        """A relative path or a plain-http download is rejected."""
        for installer, message in (("setup.msi", "absolute Windows path"),
                                   ("http://builds.local/Northwind.msi", "https URL"),
                                   ("C:\\Installers\\Northwind.zip", "must be one of")):
            with self.subTest(installer=installer):
                response = self.client.post("/api/desktop/installs", json=install_body(installer=installer))
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(message, response.json()["detail"])
        https = self.client.post("/api/desktop/installs", json=install_body(
            installer="https://builds.example.com/NorthwindOrders.msi"))
        self.assertEqual(https.status_code, 201)

    def test_UT_DSKINS_05_package_managers_take_a_package_id(self):
        """winget and choco install by id, with no file path involved."""
        receipt = self.client.post("/api/desktop/installs", json=install_body(
            name="Contoso Scanner", installer_kind="winget", installer="Contoso.Scanner")).json()["install"]
        self.assertEqual(receipt["status"], "installed")
        self.assertTrue(receipt["command"].startswith("winget install --id Contoso.Scanner"))
        bad = self.client.post("/api/desktop/installs", json=install_body(
            installer_kind="choco", installer="C:\\not-a-package-id"))
        self.assertEqual(bad.status_code, 422)
        self.assertIn("package id", bad.json()["detail"])

    def test_UT_DSKINS_06_verify_opens_and_closes_the_application(self):
        """Verification launches the installed app once and counts its controls."""
        self.client.post("/api/desktop/installs", json=install_body())
        response = self.client.post("/api/desktop/installs/northwind-orders/verify")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["window_title"], "Northwind Orders")
        self.assertGreaterEqual(payload["controls"], 3)
        unknown = self.client.post("/api/desktop/installs/nothing-here/verify")
        self.assertEqual(unknown.status_code, 422)
        self.assertIn("Install that application", unknown.json()["detail"])

    def test_UT_DSKINS_07_recording_carries_the_install_receipt(self):
        """A recording started against an installed app reports how it was installed."""
        self.client.post("/api/desktop/installs", json=install_body())
        project = self.client.post("/api/projects", json={"name": "Desktop install project"}).json()
        started = self.client.post("/api/desktop/recordings", json={
            "project_id": project["id"], "name": "Installed app session",
            "app_id": "northwind-orders", "windows_version": "windows_11",
        }).json()
        self.assertTrue(started.get("active"), started)
        try:
            self.assertEqual(started["app_name"], "Northwind Orders")
            receipt = started["install"]
            self.assertEqual(receipt["status"], "installed")
            self.assertEqual(receipt["installer"], MSI)
            self.assertEqual(receipt["mode"], "simulator")
            session = self.client.get("/api/desktop/recording/session").json()
            self.assertEqual(session["install"]["app_id"], "northwind-orders")
        finally:
            self.client.post(f"/api/desktop/recordings/{started['recording_id']}/stop", json={})

    def test_UT_DSKINS_08_remove_forgets_the_install(self):
        """Removing an install drops the receipt and unregisters the profile."""
        self.client.post("/api/desktop/installs", json=install_body())
        self.assertEqual(self.client.delete("/api/desktop/installs/northwind-orders").status_code, 200)
        self.assertEqual(self.client.delete("/api/desktop/installs/northwind-orders").status_code, 404)
        ids = {row["id"] for row in self.client.get("/api/desktop/apps").json()["apps"]}
        self.assertNotIn("northwind-orders", ids)

    def test_UT_DSKINS_09_agent_install_uses_the_reported_exit_code(self):
        """With an agent configured the agent's own result decides the status."""
        calls = {}

        class FakeAgent:
            def __init__(self, *args, **kwargs):
                pass

            def dispatch(self, step, context=None, include_screenshot=False):
                calls["step"] = step
                calls["context"] = context
                calls["screenshot"] = include_screenshot
                return {"exit_code": 1603, "installed": False,
                        "log": "fatal error during installation\n" * 400}

        with patch.object(desktop_install, "agent_configured", lambda: True), \
                patch("app.desktop_runner.RemoteWindowsAgent", FakeAgent):
            payload = self.client.post("/api/desktop/installs", json=install_body()).json()["install"]
        self.assertEqual(payload["mode"], "agent")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["exit_code"], 1603)
        self.assertEqual(calls["step"]["action"], "app_install")
        self.assertEqual(calls["step"]["value"], payload["command"])
        self.assertEqual(calls["context"]["installer"], MSI)
        self.assertIs(calls["screenshot"], False)
        # The agent's log is bounded, not copied wholesale into the receipt.
        self.assertLessEqual(sum(len(line) for line in payload["install_log"]),
                             desktop_install.MAX_INSTALL_LOG_CHARS + 400)

        class OkAgent(FakeAgent):
            def dispatch(self, step, context=None, include_screenshot=False):
                return {"exit_code": 0, "installed": True, "log": ["installed 4.2.1"]}

        with patch.object(desktop_install, "agent_configured", lambda: True), \
                patch("app.desktop_runner.RemoteWindowsAgent", OkAgent):
            ok = self.client.post("/api/desktop/installs", json=install_body()).json()["install"]
        self.assertEqual(ok["status"], "installed")
        self.assertEqual(ok["install_log"], ["installed 4.2.1"])

    def test_UT_DSKINS_10_simulator_supports_the_install_action(self):
        """`app_install` is a first-class desktop action the simulator can run."""
        self.assertIn("app_install", DESKTOP_ACTIONS)
        validate_desktop_selector({"window_title": "Northwind Orders"}, "app_install")
        with self.assertRaises(ValueError):
            validate_desktop_selector({}, "app_install")
        profile = {
            "app_id": "northwind-orders", "name": "Northwind Orders",
            "window_title": "Northwind Orders", "process_name": "Orders.exe",
            "framework": "winforms", "kind": "custom",
        }
        simulator = DesktopSimulator(app_id="northwind-orders", app_profile=validate_custom_app(profile))
        result = simulator.perform({
            "action": "app_install",
            "selector": {"app_id": "northwind-orders", "window_title": "Northwind Orders"},
            "value": 'msiexec.exe /i "C:\\Installers\\NorthwindOrders.msi" /qn',
        })
        self.assertTrue(result["installed"])
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["registered"])
        self.assertEqual(result["process_name"], "Orders.exe")
        self.assertTrue(any("nothing was installed" in line for line in result["log"]))



if __name__ == "__main__":
    unittest.main()
