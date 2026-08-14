import os

os.environ.setdefault("KAPSO_API_KEY", "test-kapso-key")
os.environ.setdefault("KAPSO_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("KAPSO_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault(
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    '{"type": "service_account", "project_id": "test", '
    '"private_key": "x", "client_email": "test@test.iam.gserviceaccount.com", '
    '"token_uri": "https://oauth2.googleapis.com/token"}',
)
os.environ.setdefault("SPREADSHEET_ID", "test-spreadsheet-id")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("SCHEDULING_LINK", "https://cal.example.com/intro")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "12345")
os.environ.setdefault(
    "VICTORIA_METRICS_URL",
    "http://victoria-metrics.observability.svc.cluster.local:8428/api/v1/import/prometheus",
)
