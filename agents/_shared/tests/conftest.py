import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "12345")
os.environ.setdefault(
    "VICTORIA_METRICS_URL",
    "http://victoria-metrics.observability.svc.cluster.local:8428/api/v1/import/prometheus",
)
