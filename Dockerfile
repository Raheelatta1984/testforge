FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN npx -y @playwright/mcp@latest --help > /dev/null 2>&1 || true
COPY app ./app
RUN mkdir -p /app/artifacts
ENV TF_ARTIFACTS=/app/artifacts
ENV PORT=8000
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
