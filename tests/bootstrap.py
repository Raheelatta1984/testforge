"""Isolate unit tests from the developer's database before the app imports."""

import os
import tempfile


def ensure_test_env() -> str:
    """Point DATABASE_URL and TF_ARTIFACTS at a throwaway directory.

    Must run before `app.config` is imported. A second call is a no-op so the
    harness and the unit module can both invoke it.
    """
    if os.environ.get("TF_TEST_ENV") == "1" and os.environ.get("TF_TEST_ROOT"):
        return os.environ["TF_TEST_ROOT"]
    root = tempfile.mkdtemp(prefix="tf-unit-")
    os.environ["TF_TEST_ENV"] = "1"
    os.environ["TF_TEST_ROOT"] = root
    os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(root, "unit.db")
    os.environ["TF_ARTIFACTS"] = os.path.join(root, "artifacts")
    os.environ.setdefault("PORT", "8765")
    os.makedirs(os.environ["TF_ARTIFACTS"], exist_ok=True)
    return root
