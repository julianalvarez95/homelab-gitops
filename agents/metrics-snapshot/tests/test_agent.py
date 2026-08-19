from unittest.mock import patch

import agent


def _vm_result(label, value):
    return {"metric": label, "value": ["ignored", str(value)]}


def _fake_vm_query(query, timeout=5):
    if query.startswith("last_over_time(agent_last_run_timestamp_seconds"):
        return [
            _vm_result({"agent": "morning-digest"}, 1000),
            _vm_result({"agent": "watchdog"}, 2000),
        ]
    if query.startswith("last_over_time(agent_run_success"):
        return [_vm_result({"agent": "morning-digest"}, 1)]
    if query.startswith("last_over_time(agent_run_duration_seconds"):
        return [_vm_result({"agent": "morning-digest"}, 12.5)]
    if query.startswith("sum_over_time(agent_llm_tokens_total"):
        return [_vm_result({"agent": "morning-digest"}, 4200)]
    if query.startswith("last_over_time(watchdog_alert_state"):
        return [_vm_result({"alert": "disk-full"}, 2)]
    if query.startswith("last_over_time(watchdog_alert_since_seconds"):
        return [_vm_result({"alert": "disk-full"}, 5000)]
    if query.startswith("sum by (status)"):
        return [_vm_result({"status": "sent"}, 3), _vm_result({"status": "pending"}, 1)]
    raise AssertionError(f"unexpected query: {query}")


@patch("agent.vm_query", side_effect=_fake_vm_query)
def test_build_snapshot_unions_agents_and_fills_missing_metrics(mock_vm_query):
    snapshot = agent.build_snapshot()

    # watchdog has a last-run timestamp but no run_success/duration/tokens
    # sample in this window: it must still show up, with the missing
    # fields as None rather than being dropped from the snapshot.
    assert snapshot["agents"]["watchdog"] == {
        "last_run_timestamp": 2000.0,
        "last_run_success": None,
        "last_run_duration_seconds": None,
        "tokens_30d": None,
    }
    assert snapshot["agents"]["morning-digest"] == {
        "last_run_timestamp": 1000.0,
        "last_run_success": True,
        "last_run_duration_seconds": 12.5,
        "tokens_30d": 4200.0,
    }
    assert snapshot["watchdog_alerts"] == [{"alert": "disk-full", "state": 2, "since": 5000.0}]
    assert snapshot["outreach_bot_contacts"] == {"sent": 3.0, "pending": 1.0}
