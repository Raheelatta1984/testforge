"""Explicit local/CI gate. No scheduler, installer, deployment or paid integration.

Requires Chromium, Node @playwright/test, TF_TEST_NODE_MODULES, and an isolated
publishing-disabled preview at TF_TEST_APP_URL. Runs suites sequentially and fails
on missing prerequisites; opt-in browser checks are never silently skipped.
"""
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    node = os.environ.get('TF_TEST_NODE_MODULES')
    if not node or not (Path(node)/'@playwright/test/cli.js').is_file():
        raise SystemExit('Set TF_TEST_NODE_MODULES to installed @playwright/test@1.49.1 dependencies; no auto-install is performed.')
    if not os.environ.get('TF_TEST_APP_URL'):
        raise SystemExit('Set TF_TEST_APP_URL to an isolated, publishing-disabled TestForge server. Never production.')
    env = {**os.environ, 'TF_TEST_BROWSER': '1'}
    commands = [
        [sys.executable, '-m', 'tests.harness', '--require-browser'],
        [sys.executable, '-m', 'unittest', 'tests.test_browser_features', 'tests.test_plan_browser', 'tests.test_scenario_browser', '-v'],
        [sys.executable, '-m', 'unittest', 'tests.test_dashboard_import', '-v'],
    ]
    for command in commands:
        subprocess.run(command, cwd=root, env=env, check=True)
    print('PASS: sequential quality gate; no push or deployment performed.')


if __name__ == '__main__':
    main()
