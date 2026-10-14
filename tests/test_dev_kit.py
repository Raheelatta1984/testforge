"""Dev & Deploy kit: the guides, SQL scripts, AI prompt library and downloads.

These call the API through FastAPI's TestClient, so they exercise the same
routes the dashboard's Dev & Deploy tab calls. Scenario IDs are documented in
tests/SCENARIOS.md.
"""
import io
import json
import re
import unittest
import zipfile
from pathlib import Path

from tests.bootstrap import ensure_test_env

ensure_test_env()

from fastapi.testclient import TestClient

from app import dev_kit
from app.dev_prompts import MAX_PROMPTS, PROMPT_PHASES, PROMPTS, prompt_library, prompt_markdown
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


class DevKitTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_UT_DEVKIT_01_index_lists_every_guide(self):
        """The tab's payload lists every guide, its size and the prompt library."""
        payload = self.client.get("/api/dev/kit").json()
        self.assertEqual(len(payload["docs"]), len(dev_kit.DEV_KIT_DOCS))
        for doc in payload["docs"]:
            with self.subTest(slug=doc["slug"]):
                self.assertTrue(doc["available"], doc["slug"] + " is missing from this deployment")
                self.assertGreater(doc["bytes"], 500)
                self.assertTrue(doc["title"] and doc["blurb"] and doc["audience"])
        self.assertEqual(payload["prompt_count"], min(len(PROMPTS), MAX_PROMPTS))
        self.assertEqual(len(payload["phases"]), len(PROMPT_PHASES))
        self.assertEqual(payload["bundle_url"], "/api/docs/dev-kit/bundle")

    def test_UT_DEVKIT_02_guide_markdown_is_served(self):
        """A guide is served as markdown with the repository path next to it."""
        payload = self.client.get("/api/docs/dev-kit/azure-sql").json()
        self.assertEqual(payload["path"], "docs/dev-azure-sql-server.md")
        self.assertFalse(payload["truncated"])
        self.assertIn("mssql+pyodbc", payload["markdown"])
        self.assertIn("## 5. Driver and connection string", payload["markdown"])
        # The SQL script companion is a guide too, not a raw .sql download.
        scripts = self.client.get("/api/docs/dev-kit/sql-scripts").json()
        self.assertIn("CREATE TABLE recording_steps", scripts["markdown"])

    def test_UT_DEVKIT_03_unknown_slug_is_rejected_not_traversed(self):
        """A slug is an allowlist key: no path reaches the file system."""
        response = self.client.get("/api/docs/dev-kit/nope")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Unknown Dev & Deploy document", response.json()["detail"])
        # A traversal attempt never reaches the handler: the router matches a
        # literal path segment, so Starlette answers 404 before our code runs.
        for slug in ("../../etc/passwd", "..%2f..%2fsecret", "architecture/../.."):
            with self.subTest(slug=slug):
                self.assertEqual(self.client.get("/api/docs/dev-kit/" + slug).status_code, 404)

    def test_UT_DEVKIT_04_prompt_library_is_generated_and_downloadable(self):
        """The downloadable markdown is generated from the same prompt list."""
        response = self.client.get("/api/docs/dev-kit/ai-prompts/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="ai-prompt-library.md"')
        text = response.text
        self.assertEqual(text, prompt_markdown())
        for prompt in PROMPTS:
            with self.subTest(prompt=prompt["id"]):
                self.assertIn(prompt["id"], text)
                self.assertIn(prompt["prompt"], text)
        # Every phase heading is present, so the file reads as seven sections.
        for phase in PROMPT_PHASES:
            self.assertIn("## " + phase["name"], text)

    def test_UT_DEVKIT_05_bundle_zip_contains_every_document(self):
        """One zip holds every guide plus a reading-order README."""
        response = self.client.get("/api/docs/dev-kit/bundle")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertTrue(response.headers["content-disposition"].startswith('attachment; filename="testforge-dev-kit-'))
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        names = set(archive.namelist())
        self.assertIn("README.md", names)
        for doc in dev_kit.DEV_KIT_DOCS:
            with self.subTest(file=doc["file"]):
                self.assertIn(doc["file"], names)
        self.assertIn("mssql+pyodbc", archive.read("dev-azure-sql-server.md").decode("utf-8"))

    def test_UT_DEVKIT_06_every_prompt_is_complete_and_bounded(self):
        """Each prompt carries its inputs, expected output and review checks."""
        phase_ids = {phase["id"] for phase in PROMPT_PHASES}
        library = prompt_library()
        self.assertLessEqual(library["count"], MAX_PROMPTS)
        seen = set()
        for prompt in library["prompts"]:
            with self.subTest(prompt=prompt.get("id")):
                self.assertTrue(prompt["id"] and prompt["id"] not in seen)
                seen.add(prompt["id"])
                self.assertIn(prompt["phase"], phase_ids)
                self.assertTrue(prompt["title"] and prompt["when"] and prompt["output"])
                self.assertTrue(prompt["inputs"] and prompt["prompt"] and prompt["review"])
                # A prompt that asks for a credential is a leak waiting to happen.
                for field in (prompt["prompt"], prompt["output"], *prompt["inputs"]):
                    self.assertNotIn("password=", field.lower())

    def test_UT_DEVKIT_07_dashboard_serves_the_tab(self):
        """The tab, its component file and its guides are all reachable."""
        index = self.client.get("/").text
        self.assertIn('<script src="/devdeploy-components.js"></script>', index)
        self.assertIn('"Dev & Deploy"', index)
        self.assertIn('tab === "devdeploy" && e(DevDeployTab', index)
        component = self.client.get("/devdeploy-components.js")
        self.assertEqual(component.status_code, 200)
        self.assertIn("function DevDeployTab", component.text)
        self.assertIn("/api/docs/dev-kit/bundle", component.text)

    def test_UT_DEVKIT_08_missing_guide_reports_where_to_read_it(self):
        """A deployment without docs/ still tells the team where the guide is."""
        original = dev_kit.DEV_KIT_DOCS[:]
        dev_kit.DEV_KIT_DOCS[:] = [dict(entry, file="not-shipped.md", slug="architecture") for entry in original
                                   if entry["slug"] == "architecture"]
        try:
            response = self.client.get("/api/docs/dev-kit/architecture")
            self.assertEqual(response.status_code, 404)
            self.assertIn("docs/not-shipped.md is not part of this deployment", response.json()["detail"])
            self.assertIn("github.com/Raheelatta1984/testforge", response.json()["detail"])
            # The bundle skips the missing file instead of failing.
            archive = zipfile.ZipFile(io.BytesIO(self.client.get("/api/docs/dev-kit/bundle").content))
            self.assertNotIn("not-shipped.md", archive.namelist())
        finally:
            dev_kit.DEV_KIT_DOCS[:] = original


class AzureSqlSupportTests(unittest.TestCase):
    """The SQL Server claims the guide makes, checked against the models."""

    def test_UT_DEVKIT_09_boolean_backfill_uses_the_dialect_literal(self):
        """SQL Server stores a boolean as BIT, which rejects the word 'false'."""
        from sqlalchemy import Boolean, Column
        from sqlalchemy.dialects import mssql, postgresql, sqlite

        from app.db import _backfill_sql

        column = Column("is_secret", Boolean, nullable=False)
        self.assertIn("BIT", column.type.compile(mssql.dialect()))
        self.assertEqual(_backfill_sql(column, postgresql.dialect()), "false")
        self.assertEqual(_backfill_sql(column, sqlite.dialect()), "false")
        self.assertEqual(_backfill_sql(column, mssql.dialect()), "0")

        flagged = Column("flag", Boolean, nullable=False, default=True)
        self.assertEqual(_backfill_sql(flagged, postgresql.dialect()), "true")
        self.assertEqual(_backfill_sql(flagged, mssql.dialect()), "1")

    def test_UT_DEVKIT_10_the_committed_sql_script_matches_the_models(self):
        """The DDL in the guide is what create_all() would produce on SQL Server."""
        from app.db import Base

        text = (ROOT / "docs" / "dev-azure-sql-scripts.sql.md").read_text(encoding="utf-8")
        blocks = dict(re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", text, re.S))
        self.assertEqual(set(blocks), {table.name for table in Base.metadata.sorted_tables})
        for table in Base.metadata.sorted_tables:
            listed = set()
            for line in blocks[table.name].splitlines():
                line = line.strip().rstrip(",")
                if not line or line.upper().startswith(("PRIMARY", "FOREIGN", "CONSTRAINT", "UNIQUE", "CHECK")):
                    continue
                listed.add(line.split(" ")[0].strip("[]"))
            with self.subTest(table=table.name):
                self.assertEqual(listed, {column.name for column in table.columns})
        # JSON columns are documented as NVARCHAR(max), which is what they compile to.
        self.assertIn("execution_plan NVARCHAR(max)", blocks["recordings"])


if __name__ == "__main__":
    unittest.main()
