FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy
WORKDIR /app
RUN apt-get update && apt-get install -y zip git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
COPY app ./app
RUN mkdir -p /app/artifacts/runs /app/artifacts/rec
ENV TF_ARTIFACTS=/app/artifacts
ENV PORT=8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]