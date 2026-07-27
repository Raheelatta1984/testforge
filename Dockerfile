FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

# Install Virtual Desktop and Keyboard dependencies
RUN apt-get update && apt-get install -y \
    xvfb fluxbox x11vnc dbus-x11 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY app ./app
RUN mkdir -p /app/artifacts/runs /app/artifacts/rec

ENV TF_ARTIFACTS=/app/artifacts
ENV DISPLAY=:99
ENV PORT=8000

# Start Virtual Display + App
CMD Xvfb :99 -screen 0 1280x800x24 & fluxbox & uvicorn app.main:app --host 0.0.0.0 --port 8000