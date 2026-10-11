# Slim base on purpose.
#
# The Microsoft Playwright image previously used here bundles Chromium, Firefox
# and WebKit. This service only ever drives Chromium, so the other two browsers
# added several hundred megabytes to the image and to every deploy for nothing.
FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git publishes the library. `zip` was installed but never used - the app builds
# archives with Python's zipfile module.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Chromium only, plus just the shared libraries it needs.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
# The library tree is the dashboard's data store. Without it the container boots
# with an empty library/ and the GitHub tab reports "No projects in the branch".
COPY library ./library
# Harness reports ship with the image. This container has no .git directory, so
# without them the Logs tab has nothing to read: no committed coordinates and
# nothing on disk. They are a few hundred KB of text, and the reader that serves
# them is capped (see TF_MAX_LOG_FILE_BYTES).
COPY logs ./logs
# The Azure deployment guide ships with the image so the dashboard's Azure tab
# can render it (GET /api/docs/azure-guide). A few hundred KB of markdown.
COPY docs ./docs
RUN mkdir -p /app/artifacts/runs /app/artifacts/rec /app/artifacts/batches

ENV TF_ARTIFACTS=/app/artifacts \
    PORT=8000 \
    TF_BROWSER_MODE=bundled \
    # --- resource guardrails: one small instance, so every limit is explicit ---
    # Never more than one Chromium, across recording and execution together.
    TF_MAX_BROWSERS=1 \
    TF_BROWSER_SINGLE_PROCESS=1 \
    TF_VIEWPORT_WIDTH=1024 \
    TF_VIEWPORT_HEIGHT=640 \
    # Cheaper frames, and fewer of them, when nobody is looking.
    TF_PREVIEW_INTERVAL=0.25 \
    TF_VIEWER_RECHECK=0.25 \
    TF_JPEG_QUALITY=30 \
    # Bounded in-memory state per process.
    TF_MAX_LIVE_FRAME_RUNS=3 \
    TF_MAX_RUN_BUFFER_RUNS=6 \
    TF_MAX_RUN_BUFFER_EVENTS=60 \
    TF_RUN_LIST_LIMIT=25 \
    TF_MAX_ZIP_BYTES=67108864 \
    # --- queue hygiene: a restart must not leave runs pending forever ---
    TF_QUEUE_ORPHAN_GRACE=120 \
    TF_QUEUE_STALE_MINUTES=30 \
    TF_QUEUE_SWEEP_SECONDS=300 \
    TF_RUN_RETENTION_DAYS=14 \
    # --- batch execution: many recordings, one browser ---
    TF_BATCH_SCREENSHOTS=failure \
    TF_BATCH_MAX_RECORDINGS=40 \
    TF_MAX_BATCHES=1 \
    TF_BATCH_MAX_RSS_MB=420 \
    TF_BATCH_LOG_ENTRIES=200 \
    TF_BATCH_REPORTS=40 \
    # --- local log reader (the fallback when GitHub coordinates are unknown) ---
    TF_MAX_LOG_FILE_BYTES=262144 \
    TF_MAX_LOG_INDEX_ROWS=40
# TF_GITHUB_REPO and TF_GITHUB_TOKEN are NOT baked into the image. Set them at
# deploy time to let this checkout-less container publish library/ through the
# GitHub API; without them publishing is reported as disabled, and says why.

EXPOSE 8000

# Exactly one worker. The guardrails above are per-process, so a second worker
# would silently double every limit and both would compete for the same memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
