"""Dev & Deploy kit: the guides, SQL scripts and AI prompts the in-house team uses.

The dashboard's *Dev & Deploy* tab serves the development and deployment story
end to end — codebase map, local setup, SQL Server on Azure, deployment and
CI/CD — from the markdown files in ``docs/``, so the team reads the same text in
the app that is committed to the repository. The AI prompt library is generated
from :mod:`app.dev_prompts` and can be downloaded as markdown alongside the
guides, individually or as one zip.

Every read is size-capped and the slug is an allowlist key, never a path: a
client cannot ask this router for a file outside ``docs/``.
"""
from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Response

router = APIRouter()

DOCS_MAX_BYTES = 512 * 1024
# A bundle is the same documents again, so the same cap per file is enough; the
# archive itself is bounded by the number of documents times that cap.
MAX_BUNDLE_BYTES = 8 * 1024 * 1024

_DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")

# Order matters: this is the reading order the tab presents.
DEV_KIT_DOCS = [
    {
        "slug": "architecture",
        "title": "Architecture and codebase map",
        "blurb": "What each module owns, the single-worker constraints, and where a change belongs.",
        "audience": "Every developer, day one",
        "file": "dev-team-architecture.md",
    },
    {
        "slug": "local-setup",
        "title": "Local setup, running and testing",
        "blurb": "Clone to a running dashboard, plus the harness that proves a change works.",
        "audience": "Every developer, day one",
        "file": "dev-local-setup.md",
    },
    {
        "slug": "azure-sql",
        "title": "SQL Server on Azure, end to end",
        "blurb": "Azure SQL Database or SQL Server on an Azure VM: provisioning, driver, schema, pooling, migration.",
        "audience": "Developers and DBAs",
        "file": "dev-azure-sql-server.md",
    },
    {
        "slug": "deployment",
        "title": "Deployment end to end",
        "blurb": "Image build, Azure runtime, health checks, rollbacks and the environment table.",
        "audience": "Developers and operators",
        "file": "dev-deployment-end-to-end.md",
    },
    {
        "slug": "cicd",
        "title": "CI/CD pipelines",
        "blurb": "GitHub Actions and Azure Pipelines that run the harness and deploy by digest.",
        "audience": "DevOps",
        "file": "dev-cicd-pipelines.md",
    },
    {
        "slug": "sql-scripts",
        "title": "SQL scripts for Azure SQL",
        "blurb": "Login, grants, verification queries and the maintenance jobs, copy-paste ready.",
        "audience": "DBAs",
        "file": "dev-azure-sql-scripts.sql.md",
    },
    {
        "slug": "ai-prompts",
        "title": "AI prompt library",
        "blurb": "Prompts for each phase of the work, with the inputs to paste and the checks to run on the answer.",
        "audience": "Every developer",
        "file": "ai-prompt-library.md",
        "generated": "prompts",
    },
]


def _entry(slug):
    """Look a document up by slug at call time, so the list is the only source."""
    return next((entry for entry in DEV_KIT_DOCS if entry.get("slug") == slug), None)


def _generated_markdown(kind: str) -> str:
    if kind != "prompts":
        raise KeyError(kind)
    from app.dev_prompts import prompt_markdown
    return prompt_markdown()


def _doc_path(entry: dict) -> str:
    return os.path.join(_DOCS_DIR, entry["file"])


def doc_index() -> list[dict]:
    """The document list without bodies, for the tab's navigation."""
    rows = []
    for entry in DEV_KIT_DOCS:
        generated = entry.get("generated")
        if generated:
            body = _generated_markdown(generated)
            size, available = len(body.encode("utf-8")), True
        else:
            try:
                size = os.path.getsize(_doc_path(entry))
                available = True
            except OSError:
                size, available = 0, False
        rows.append({
            "slug": entry["slug"],
            "title": entry["title"],
            "blurb": entry["blurb"],
            "audience": entry["audience"],
            "filename": entry["file"],
            "path": None if generated else "docs/" + entry["file"],
            "generated": bool(generated),
            "bytes": size,
            "available": available,
        })
    return rows


def doc_markdown(slug: str) -> dict:
    """One document as markdown, with the metadata the tab shows next to it."""
    entry = _entry(slug)
    if entry is None:
        raise KeyError(slug)
    generated = entry.get("generated")
    if generated:
        body = _generated_markdown(generated)
        size = len(body.encode("utf-8"))
        if size > DOCS_MAX_BYTES:
            body = body.encode("utf-8")[:DOCS_MAX_BYTES].decode("utf-8", errors="ignore")
        markdown, truncated = body, size > DOCS_MAX_BYTES
    else:
        path = _doc_path(entry)
        try:
            size = os.path.getsize(path)
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                markdown = handle.read(DOCS_MAX_BYTES)
        except OSError:
            raise HTTPException(
                status_code=404,
                detail=f"docs/{entry['file']} is not part of this deployment. Read it in the "
                       f"repository instead: https://github.com/Raheelatta1984/testforge/blob/main/"
                       f"docs/{entry['file']}",
            ) from None
        truncated = size > DOCS_MAX_BYTES
    return {
        "slug": entry["slug"],
        "title": entry["title"],
        "filename": entry["file"],
        "path": None if generated else "docs/" + entry["file"],
        "generated": bool(generated),
        "bytes": size,
        "truncated": truncated,
        "markdown": markdown,
    }


def bundle_zip() -> tuple[bytes, str]:
    """Every kit document as one zip, so the team can be sent a single file."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in DEV_KIT_DOCS:
            try:
                payload = doc_markdown(entry["slug"])
            except HTTPException:
                continue  # a missing guide must not break the rest of the bundle
            if payload["truncated"]:
                continue
            archive.writestr(entry["file"], payload["markdown"])
        readme = (
            "# TestForge Dev & Deploy kit\n\n"
            f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}.\n\n"
            "Read in this order:\n\n"
            + "\n".join(f"{index}. `{entry['file']}` — {entry['blurb']}"
                        for index, entry in enumerate(DEV_KIT_DOCS, start=1))
            + "\n\nSource: https://github.com/Raheelatta1984/testforge/tree/main/docs\n"
        )
        archive.writestr("README.md", readme)
    payload = buffer.getvalue()
    if len(payload) > MAX_BUNDLE_BYTES:
        raise HTTPException(413, "The Dev & Deploy bundle exceeds the 8 MiB limit")
    return payload, f"testforge-dev-kit-{stamp}.zip"


def _attachment(filename: str) -> dict:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


@router.get("/api/dev/kit")
def dev_kit_index():
    """The tab's payload: document list plus the structured prompt library."""
    from app.dev_prompts import prompt_library
    library = prompt_library()
    return {
        "docs": doc_index(),
        "prompts": library["prompts"],
        "phases": library["phases"],
        "prompt_count": library["count"],
        "bundle_url": "/api/docs/dev-kit/bundle",
        "repository": library["repository"],
    }


@router.get("/api/docs/dev-kit")
def dev_kit_docs():
    return {"docs": doc_index(), "max_bytes": DOCS_MAX_BYTES}


@router.get("/api/docs/dev-kit/bundle")
def dev_kit_bundle():
    payload, filename = bundle_zip()
    return Response(payload, media_type="application/zip", headers=_attachment(filename))


@router.get("/api/docs/dev-kit/{slug}")
def dev_kit_doc(slug: str):
    try:
        return doc_markdown(slug)
    except KeyError:
        known = ", ".join(entry["slug"] for entry in DEV_KIT_DOCS)
        raise HTTPException(404, f"Unknown Dev & Deploy document '{slug}'. Known documents: {known}") from None


@router.get("/api/docs/dev-kit/{slug}/download")
def dev_kit_doc_download(slug: str):
    try:
        payload = doc_markdown(slug)
    except KeyError:
        raise HTTPException(404, f"Unknown Dev & Deploy document '{slug}'") from None
    return Response(payload["markdown"].encode("utf-8"), media_type="text/markdown; charset=utf-8",
                    headers=_attachment(payload["filename"]))
