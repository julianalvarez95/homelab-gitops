# Build from repo root: docker build -f agents/outreach-bot/Dockerfile -t <tag> .
# One image, two entrypoints: CronJob runs the default CMD (sender.py),
# Deployment overrides `command` to run webhook.py via uvicorn instead.
FROM python:3.12-slim
WORKDIR /app
COPY agents/outreach-bot/src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agents/_shared/telegram.py .
COPY agents/outreach-bot/src/ .
CMD ["python", "sender.py"]
