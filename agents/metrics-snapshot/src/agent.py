import os
import time
from datetime import datetime, timezone

import requests
import telegram

VICTORIA_METRICS_QUERY_URL = os.environ["VICTORIA_METRICS_QUERY_URL"]
METRICS_INGEST_URL = os.environ.get(
    "METRICS_INGEST_URL", "https://agent-metrics-dashboard-mu.vercel.app/api/ingest"
)
METRICS_INGEST_SECRET = os.environ.get("METRICS_INGEST_SECRET")


def vm_query(query, timeout=5):
    """Instant MetricsQL query. Raises on HTTP/network failure — a snapshot
    is one atomic dataset, so a query that can't be answered fails the
    whole run rather than shipping a partial/misleading snapshot."""
    resp = requests.get(
        VICTORIA_METRICS_QUERY_URL, params={"query": query}, timeout=timeout
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"VM query status={data.get('status')}: {data}")
    return data["data"]["result"]


def _by_label(results, label):
    return {
        r["metric"][label]: float(r["value"][1])
        for r in results
        if label in r["metric"]
    }


def build_snapshot():
    last_run = _by_label(vm_query("last_over_time(agent_last_run_timestamp_seconds[25h])"), "agent")
    run_success = _by_label(vm_query("last_over_time(agent_run_success[25h])"), "agent")
    duration = _by_label(vm_query("last_over_time(agent_run_duration_seconds[25h])"), "agent")
    # sum_over_time, not rate(): agent_llm_tokens_total is a per-run gauge
    # despite the _total suffix (see CLAUDE.md).
    tokens_30d = _by_label(vm_query("sum_over_time(agent_llm_tokens_total[30d])"), "agent")

    agents = {}
    for name in last_run.keys() | run_success.keys() | duration.keys():
        agents[name] = {
            "last_run_timestamp": last_run.get(name),
            "last_run_success": bool(run_success[name]) if name in run_success else None,
            "last_run_duration_seconds": duration.get(name),
            "tokens_30d": tokens_30d.get(name),
        }

    alert_state = _by_label(vm_query("last_over_time(watchdog_alert_state[25h])"), "alert")
    alert_since = _by_label(vm_query("last_over_time(watchdog_alert_since_seconds[25h])"), "alert")
    watchdog_alerts = [
        {"alert": name, "state": int(state), "since": alert_since.get(name)}
        for name, state in alert_state.items()
    ]

    outreach_bot_contacts = _by_label(
        vm_query("sum by (status) (outreach_bot_contacts_total)"), "status"
    )

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agents": agents,
        "watchdog_alerts": watchdog_alerts,
        "outreach_bot_contacts": outreach_bot_contacts,
    }


def post_snapshot(snapshot):
    # Fails open: the public dashboard is a secondary surface, a blip in
    # its ingest must not make this job (or Telegram) noisy.
    try:
        resp = requests.post(
            METRICS_INGEST_URL,
            json=snapshot,
            headers={"X-Metrics-Secret": METRICS_INGEST_SECRET},
            timeout=10,
        )
        print(f"dashboard ingest response: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    except Exception as e:
        print(f"No se pudo publicar el snapshot en el dashboard, sigo igual: {e}")


def main():
    start = time.time()
    success = False
    try:
        snapshot = build_snapshot()
        post_snapshot(snapshot)
        success = True
    finally:
        telegram.push_metrics([
            f'agent_run_success{{agent="metrics-snapshot"}} {int(success)}',
            f'agent_run_duration_seconds{{agent="metrics-snapshot"}} {time.time() - start:.2f}',
            f'agent_llm_tokens_total{{agent="metrics-snapshot"}} 0',
            f'agent_last_run_timestamp_seconds{{agent="metrics-snapshot"}} {int(time.time())}',
        ])


if __name__ == "__main__":
    main()
