# Build from repo root: docker build -f agents/watchdog/Dockerfile -t <tag> .
FROM python:3.12-slim
WORKDIR /app
COPY agents/watchdog/src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agents/_shared/telegram.py .
COPY agents/watchdog/src/agent.py .
CMD ["python", "agent.py"]
