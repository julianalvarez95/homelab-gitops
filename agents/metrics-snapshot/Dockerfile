# Build from repo root: docker build -f agents/metrics-snapshot/Dockerfile -t <tag> .
FROM python:3.12-slim
WORKDIR /app
COPY agents/metrics-snapshot/src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agents/_shared/telegram.py .
COPY agents/metrics-snapshot/src/agent.py .
CMD ["python", "agent.py"]
