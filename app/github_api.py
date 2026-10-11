"""Publish ``library/`` through the GitHub REST API when there is no git checkout.

The hosted container is built from ``app/`` plus ``library/`` only, so there is no
``.git`` directory in it. Until now that meant the GitHub tab could only ever say
*"no git checkout here"*: no branch, no revision, push disabled, and a recording
saved in the dashboard never reached the repository.

This module closes that gap without installing git, without cloning, and without
a second dependency: it talks to ``api.github.com`` with the standard library and
commits the library tree the same way `git push` would, using the Git Data API
(blobs -> tree -> commit -> ref update). A push is only reported as published
after the branch ref is read back and matches the new commit.

Configuration (all optional, read at call time so tests can change them):

| Variable | Default | Effect |
| --- | --- | --- |
| ``TF_GITHUB_TOKEN`` / ``GITHUB_TOKEN`` / ``GH_TOKEN`` | none | Without a token this module stays disabled and says so. |
| ``TF_GITHUB_REPO`` | parsed from ``TF_GIT_REMOTE``, else none | ``owner/name`` to publish to. Required: there is no built-in default, so a stray CI token cannot push anywhere. |
| ``TF_GITHUB_BRANCH`` / ``TF_GIT_BRANCH`` | ``main`` | Branch the library is committed to. |
| ``TF_GITHUB_API`` | ``https://api.github.com`` | Override for a proxy or an Enterprise host. |
| ``TF_LIBRARY_PREFIX`` | ``library`` | Folder inside the repository that mirrors the local library. |

Everything here is bounded: a maximum number of files, a maximum size per file and
a maximum payload per push, so a library that grows cannot turn one save into an
out-of-memory request on a 512MB instance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_REPO = "Raheelatta1984/testforge"
DEFAULT_BRANCH = "main"
DEFAULT_API = "https://api.github.com"

# --- Guardrails -------------------------------------------------------------
# One save must never upload the world. These are generous for JSON plus a few
# hundred screenshots and small enough that the request bodies fit in memory.
MAX_FILES = int(os.environ.get("TF_GITHUB_MAX_FILES", "400") or 400)
MAX_FILE_BYTES = int(os.environ.get("TF_GITHUB_MAX_FILE_BYTES", str(2 * 1024 * 1024)) or 2 * 1024 * 1024)
MAX_PUSH_BYTES = int(os.environ.get("TF_GITHUB_MAX_PUSH_BYTES", str(24 * 1024 * 1024)) or 24 * 1024 * 1024)
TIMEOUT = float(os.environ.get("TF_GITHUB_TIMEOUT", "25") or 25)

_SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

# A branch name is interpolated into request paths (/branches/{branch},
# /git/refs/heads/{branch}), so it must not be able to leave its path segments.
# This is git check-ref-format plus the URL-significant characters: git itself
# allows '#' and '%' in ref names, but a path segment carrying them can rewrite
# where the request goes, so this publisher refuses them.
_BAD_REF_CHARS = set(" ~^:?*[\\#%")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def valid_branch(name: str) -> bool:
    """True when ``name`` is safe to interpolate into a GitHub API path."""
    if not name or name == "@" or len(name) > 255:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return False
    if any(ch in _BAD_REF_CHARS for ch in name):
        return False
    if ".." in name or "@{" in name or "//" in name:
        return False
    if name.startswith("/") or name.endswith("/") or name.endswith(".lock"):
        return False
    return not any(part.startswith(".") or part.endswith(".") for part in name.split("/"))


class GitHubAPIError(Exception):
    """Any failure of the REST publisher, with credentials already removed."""


def _token() -> str | None:
    for name in ("TF_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _slug_from_url(url: str) -> str | None:
    match = re.search(r"github\.com[/:]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", (url or "").strip())
    if not match:
        return None
    slug = match.group(1)
    return slug if _SLUG_RE.match(slug) else None


def repo_slug() -> str | None:
    """``owner/name`` to publish to, from configuration rather than a checkout.

    There is deliberately no built-in default. A stray ``GITHUB_TOKEN`` in the
    environment (CI runners have one) must not be enough to start committing to a
    repository nobody named: publishing over the API is opt-in through
    ``TF_GITHUB_REPO`` or a ``TF_GIT_REMOTE`` URL.
    """
    explicit = (os.environ.get("TF_GITHUB_REPO") or "").strip().rstrip("/")
    if explicit:
        slug = explicit.split("github.com/")[-1]
        slug = slug[:-4] if slug.endswith(".git") else slug
        return slug if _SLUG_RE.match(slug) else None
    for name in ("TF_GIT_REMOTE", "TF_GITHUB_URL"):
        slug = _slug_from_url(os.environ.get(name, ""))
        if slug:
            return slug
    return None


def branch() -> str:
    for name in ("TF_GITHUB_BRANCH", "TF_GIT_BRANCH"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return DEFAULT_BRANCH


def api_base() -> str:
    return (os.environ.get("TF_GITHUB_API") or DEFAULT_API).rstrip("/")


def library_prefix() -> str:
    return (os.environ.get("TF_LIBRARY_PREFIX") or "library").strip("/") or "library"


def describe() -> dict:
    """Cheap, network-free description of what this publisher could do."""
    token = _token()
    slug = repo_slug()
    if not slug:
        return {
            "available": False,
            "mode": "api",
            "slug": None,
            "branch": branch(),
            "reason": (
                "No GitHub repository is configured. Set TF_GITHUB_REPO=owner/name "
                "(or TF_GIT_REMOTE) together with TF_GITHUB_TOKEN to publish without "
                "a git checkout."
            ),
        }
    if not valid_branch(branch()):
        return {
            "available": False,
            "mode": "api",
            "slug": slug,
            "branch": branch(),
            "reason": (
                f"TF_GITHUB_BRANCH {branch()!r} is not a valid git branch name "
                "(see git check-ref-format), so publishing is refused rather than "
                "sending a malformed request. Fix the branch name to enable pushing."
            ),
        }
    if not token:
        return {
            "available": False,
            "mode": "api",
            "slug": slug,
            "branch": branch(),
            "reason": (
                f"{slug}@{branch()} is configured but no API token is present. Set "
                "TF_GITHUB_TOKEN (or GITHUB_TOKEN) with repo scope to enable pushing."
            ),
        }
    return {
        "available": True,
        "mode": "api",
        "slug": slug,
        "branch": branch(),
        "reason": None,
    }


def enabled() -> bool:
    return bool(describe()["available"])


# Header-shaped credentials, and the token prefixes GitHub actually issues. Both
# are scrubbed because an error body can quote either one back at us.
_HEADER_RE = re.compile(
    r"(?i)\b(authorization|token|api[-_]?key|password)\b[\"']?\s*[:=]?\s*(bearer\s+)?[\"']?[\w.\-]+"
)
# A real token is 36 alphanumerics, but the redactor is deliberately greedy: a
# half-redacted token in a dashboard is still a leaked token.
_TOKEN_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{4,}")
# https://user:token@github.com/... - git and urllib both put credentials in the
# URLs they report back, and TF_GIT_REMOTE may carry them.
_URL_CREDS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^@\s]+)(@)")


def _safe(text) -> str:
    """Strip the token and any other credential before a message leaves the process."""
    message = str(text)
    token = _token()
    if token:
        message = message.replace(token, "***")
    message = _HEADER_RE.sub(lambda match: f"{match.group(1)}: ***", message)
    message = _URL_CREDS_RE.sub(lambda match: f"{match.group(1)}***{match.group(3)}", message)
    message = _TOKEN_RE.sub("***", message)
    return message[:500]


def blob_sha(data: bytes) -> str:
    """The SHA-1 git would give this file, computed without a git binary."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class GitHubClient:
    """Minimal REST client. `request` is the only thing tests need to replace."""

    def __init__(self, slug: str | None = None, ref: str | None = None, token: str | None = None):
        info = describe()
        self.slug = slug or info["slug"]
        self.branch = ref or info["branch"]
        self.token = token if token is not None else _token()
        self.calls: list[tuple[str, str]] = []

    # --- transport ----------------------------------------------------------
    def request(self, method: str, path: str, payload=None, accept: str = "application/vnd.github+json"):
        if not self.slug:
            raise GitHubAPIError("No GitHub repository is configured")
        if not _SLUG_RE.match(self.slug):
            raise GitHubAPIError("Refusing to call GitHub for an unrecognised repository name")
        if not valid_branch(self.branch):
            raise GitHubAPIError(
                f"Refusing to call GitHub with the branch name {self.branch!r}: "
                "it is not a valid git branch name"
            )
        if not self.token:
            raise GitHubAPIError("No GitHub API token is configured")
        url = f"{api_base()}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", accept)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        self.calls.append((method, path.split("?")[0]))
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            try:
                parsed = json.loads(detail)
                detail = parsed.get("message") or detail
            except Exception:
                pass
            raise GitHubAPIError(f"GitHub {exc.code} for {method} {path.split('?')[0]}: {_safe(detail)}") from None
        except urllib.error.URLError as exc:
            raise GitHubAPIError(f"GitHub is unreachable: {_safe(getattr(exc, 'reason', exc))}") from None
        except Exception as exc:  # timeout and friends
            raise GitHubAPIError(f"GitHub request failed: {_safe(exc)}") from None
        if status == 204 or not body:
            return status, None
        try:
            return status, json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise GitHubAPIError(f"GitHub returned an unreadable body: {_safe(exc)}") from None

    # --- reads --------------------------------------------------------------
    def head_sha(self) -> str | None:
        try:
            _, payload = self.request("GET", f"/repos/{self.slug}/branches/{self.branch}")
        except GitHubAPIError as exc:
            if "404" in str(exc):
                return None
            raise
        if not isinstance(payload, dict):
            return None
        commit = payload.get("commit") or {}
        return commit.get("sha")

    def tree(self, sha: str) -> dict[str, dict]:
        """Remote blobs under the library prefix, keyed by repository path."""
        if not _SHA_RE.match(str(sha or "")):
            raise GitHubAPIError("Refusing to read a tree for a SHA that is not a git object id")
        _, payload = self.request("GET", f"/repos/{self.slug}/git/trees/{sha}?recursive=1")
        entries = (payload or {}).get("tree") or []
        prefix = library_prefix() + "/"
        found = {}
        for entry in entries:
            path = entry.get("path") or ""
            if entry.get("type") != "blob" or not path.startswith(prefix):
                continue
            found[path] = {"sha": entry.get("sha"), "size": entry.get("size") or 0}
        return found

    # --- writes -------------------------------------------------------------
    def create_blob(self, data: bytes) -> str:
        _, payload = self.request(
            "POST",
            f"/repos/{self.slug}/git/blobs",
            {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"},
        )
        sha = (payload or {}).get("sha")
        if not sha:
            raise GitHubAPIError("GitHub did not return a blob SHA")
        return sha

    def create_tree(self, base_sha: str | None, entries: list[dict]) -> str:
        payload: dict = {"tree": entries}
        if base_sha:
            payload["base_tree"] = base_sha
        _, created = self.request("POST", f"/repos/{self.slug}/git/trees", payload)
        sha = (created or {}).get("sha")
        if not sha:
            raise GitHubAPIError("GitHub did not return a tree SHA")
        return sha

    def create_commit(self, message: str, tree_sha: str, parent: str | None) -> str:
        payload: dict = {"message": message, "tree": tree_sha}
        if parent:
            payload["parents"] = [parent]
        _, created = self.request("POST", f"/repos/{self.slug}/git/commits", payload)
        sha = (created or {}).get("sha")
        if not sha:
            raise GitHubAPIError("GitHub did not return a commit SHA")
        return sha

    def update_ref(self, sha: str, force: bool = False) -> None:
        ref = f"/repos/{self.slug}/git/refs/heads/{self.branch}"
        try:
            self.request("PATCH", ref, {"sha": sha, "force": force})
        except GitHubAPIError as exc:
            if "422" in str(exc) and not force:
                # The branch does not exist yet: create it instead of updating it.
                self.request(
                    "POST",
                    f"/repos/{self.slug}/git/refs",
                    {"ref": f"refs/heads/{self.branch}", "sha": sha},
                )
                return
            raise


def collect_local_files(root: Path) -> dict[str, dict]:
    """Hash every file under the library folder, keyed by repository path.

    Returns ``{"path": {"sha", "size"}}`` plus a ``"_skipped"`` report so a caller
    can tell the user why an oversized file was not published instead of
    silently dropping it.
    """
    prefix = library_prefix()
    files: dict[str, dict] = {}
    skipped: list[dict] = []
    root = Path(root)
    if not root.is_dir():
        return {"files": {}, "skipped": skipped, "bytes": 0}
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.name.endswith(".pyc"):
            continue
        relative = path.relative_to(root).as_posix()
        repo_path = f"{prefix}/{relative}"
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            skipped.append({"path": repo_path, "reason": f"larger than {MAX_FILE_BYTES} bytes"})
            continue
        if len(files) >= MAX_FILES:
            skipped.append({"path": repo_path, "reason": f"more than {MAX_FILES} files in one push"})
            continue
        total += size
        if total > MAX_PUSH_BYTES:
            skipped.append({"path": repo_path, "reason": f"push would exceed {MAX_PUSH_BYTES} bytes"})
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            skipped.append({"path": repo_path, "reason": f"unreadable: {exc.__class__.__name__}"})
            continue
        files[repo_path] = {"sha": blob_sha(data), "size": size}
    return {"files": files, "skipped": skipped, "bytes": total}


def plan(local: dict[str, dict], remote: dict[str, dict]) -> dict:
    """What a push would change. No network, so it is cheap enough to expose."""
    added = sorted(p for p in local if p not in remote)
    changed = sorted(p for p in local if p in remote and remote[p].get("sha") != local[p]["sha"])
    removed = sorted(p for p in remote if p not in local)
    unchanged = len(local) - len(added) - len(changed)
    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged": unchanged,
        "total": len(added) + len(changed) + len(removed),
        "synced": not (added or changed or removed),
    }


def remote_status(client: GitHubClient | None = None, library_root: Path | None = None) -> dict:
    """Compare the branch with the local library. Never raises: it reports instead."""
    info = describe()
    if not info["available"]:
        return {
            "synced": False,
            "state": "api-unconfigured",
            "mode": "api",
            "branch": info["branch"],
            "slug": info["slug"],
            "reason": info["reason"],
        }
    client = client or GitHubClient()
    try:
        head = client.head_sha()
        if head is None:
            return {
                "synced": False,
                "state": "no-remote-branch",
                "mode": "api",
                "slug": client.slug,
                "branch": client.branch,
                "remote_sha": None,
                "reason": (
                    f"Branch {client.branch} does not exist on {client.slug} yet. "
                    "The first successful push creates it."
                ),
            }
        remote_tree = client.tree(head)
        diff = {}
        if library_root is not None:
            collected = collect_local_files(library_root)
            diff = plan(collected["files"], remote_tree)
        state = "synced" if diff.get("synced", False) else "dirty"
        reason = (
            "Remote branch matches the local library."
            if state == "synced"
            else (
                f"{len(diff.get('added', []))} new, {len(diff.get('changed', []))} changed, "
                f"{len(diff.get('removed', []))} deleted file(s) are not on {client.branch} yet."
            )
        )
        return {
            "synced": state == "synced",
            "state": state,
            "mode": "api",
            "slug": client.slug,
            "branch": client.branch,
            "remote_sha": head,
            "revision": head[:7],
            "files_on_remote": len(remote_tree),
            "diff": diff,
            "reason": reason,
        }
    except GitHubAPIError as exc:
        return {
            "synced": False,
            "state": "unreachable",
            "mode": "api",
            "slug": client.slug,
            "branch": client.branch,
            "error": _safe(exc),
            "reason": f"Could not reach the GitHub API for {client.slug}: {_safe(exc)}",
        }


def publish(message: str, library_root: Path, client: GitHubClient | None = None,
            dry_run: bool = False) -> dict:
    """Commit the local library to the configured branch and verify the ref.

    Returns a report rather than raising for the ordinary outcomes (nothing to do,
    branch missing) so the dashboard can explain each one. Failures that stop the
    push raise ``GitHubAPIError`` with the token already removed.
    """
    info = describe()
    if not info["available"]:
        raise GitHubAPIError(info["reason"] or "GitHub API publishing is not configured")
    client = client or GitHubClient()
    started = time.time()
    collected = collect_local_files(library_root)
    local = collected["files"]
    head = client.head_sha()
    remote_tree = client.tree(head) if head else {}
    diff = plan(local, remote_tree)
    report = {
        "published": False,
        "mode": "api",
        "slug": client.slug,
        "branch": client.branch,
        "local_sha": None,
        "remote_sha": head,
        "files": len(local),
        "bytes": collected["bytes"],
        "skipped": collected["skipped"],
        **diff,
        "dry_run": bool(dry_run),
    }
    if diff["synced"]:
        report["state"] = "synced"
        report["reason"] = "The library on the remote branch already matches this instance."
        report["elapsed"] = round(time.time() - started, 3)
        return report
    if dry_run:
        report["state"] = "pending"
        report["reason"] = "Dry run: nothing was pushed."
        report["elapsed"] = round(time.time() - started, 3)
        return report

    entries: list[dict] = []
    for path in diff["added"] + diff["changed"]:
        data = (Path(library_root) / path[len(library_prefix()) + 1:]).read_bytes()
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": client.create_blob(data)})
    for path in diff["removed"]:
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

    tree_sha = client.create_tree(head, entries)
    commit_sha = client.create_commit(message, tree_sha, head)
    client.update_ref(commit_sha)
    verified = client.head_sha()
    if verified != commit_sha:
        raise GitHubAPIError(
            f"Push completed but {client.branch} still points at {str(verified)[:12]}, "
            f"not {commit_sha[:12]}"
        )
    report.update({
        "published": True,
        "state": "synced",
        "local_sha": commit_sha,
        "remote_sha": verified,
        "revision": commit_sha[:7],
        "commit": commit_sha,
        "commit_url": f"https://github.com/{client.slug}/commit/{commit_sha}",
        "reason": f"{diff['total']} file(s) committed to {client.branch} and verified.",
        "elapsed": round(time.time() - started, 3),
    })
    return report
