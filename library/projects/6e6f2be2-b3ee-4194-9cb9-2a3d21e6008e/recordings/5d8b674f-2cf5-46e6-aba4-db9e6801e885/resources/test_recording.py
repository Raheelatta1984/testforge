"""pytest entry point for this recording.

Install and run: pip install -r requirements.txt && python -m playwright install chromium && pytest -q
Supply TF_VAR_<name> environment variables for recorded variables.
"""
import asyncio

from playwright_test import main


def test_recording_replays():
    asyncio.run(main())
