import os
import time
from datetime import datetime, timezone

import kapso_client
import sheets_client
import telegram

FOLLOWUP_AFTER_DAYS = int(os.environ.get("FOLLOWUP_AFTER_DAYS", "3"))
NO_RESPONSE_AFTER_DAYS = int(os.environ.get("NO_RESPONSE_AFTER_DAYS", "6"))
TEMPLATE_INTRO = os.environ.get("KAPSO_TEMPLATE_INTRO", "outreach_intro")
TEMPLATE_FOLLOWUP = os.environ.get("KAPSO_TEMPLATE_FOLLOWUP", "outreach_followup")


def _days_since(last_update_iso):
    last = datetime.strptime(last_update_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() / 86400


def send_pending():
    sent, failed = 0, 0
    for contact in sheets_client.list_contacts_by_status("pending"):
        try:
            kapso_client.send_template(contact["telefono"], TEMPLATE_INTRO, {"nombre": contact["nombre"]})
            sheets_client.update_status(contact["row_number"], "sent")
            sent += 1
        except Exception as e:
            print(f"Fallo enviando a {contact['telefono']}, reintenta el próximo run: {e}")
            failed += 1
    return sent, failed


def send_followups():
    # last_update is only ever missing/malformed if a human hand-edited the
    # Sheet (the bot always writes status+last_update together) — skip
    # rather than let strptime("") crash the whole sweep for every contact.
    followed_up = 0
    for contact in sheets_client.list_contacts_by_status("sent"):
        if not contact.get("last_update"):
            continue
        if _days_since(contact["last_update"]) < FOLLOWUP_AFTER_DAYS:
            continue
        try:
            kapso_client.send_template(contact["telefono"], TEMPLATE_FOLLOWUP, {"nombre": contact["nombre"]})
            sheets_client.update_status(contact["row_number"], "followed_up")
            followed_up += 1
        except Exception as e:
            print(f"Fallo mandando seguimiento a {contact['telefono']}: {e}")

    expired = 0
    for contact in sheets_client.list_contacts_by_status("followed_up"):
        if not contact.get("last_update"):
            continue
        if _days_since(contact["last_update"]) >= NO_RESPONSE_AFTER_DAYS:
            sheets_client.update_status(contact["row_number"], "no_response")
            expired += 1
    return followed_up, expired


def push_status_metrics():
    counts = sheets_client.count_by_status()
    lines = [
        f'outreach_bot_contacts_total{{status="{status}"}} {count}'
        for status, count in counts.items()
    ]
    telegram.push_metrics(lines)


def main():
    start = time.time()
    success = False
    try:
        sent, failed = send_pending()
        followed_up, expired = send_followups()
        print(f"sent={sent} failed={failed} followed_up={followed_up} expired={expired}")
        try:
            push_status_metrics()
        except Exception as e:
            print(f"No se pudieron calcular métricas de estado, sigo igual: {e}")
        success = True
    finally:
        telegram.push_metrics([
            f'agent_run_success{{agent="outreach-bot"}} {int(success)}',
            f'agent_run_duration_seconds{{agent="outreach-bot"}} {time.time() - start:.2f}',
            f'agent_last_run_timestamp_seconds{{agent="outreach-bot"}} {int(time.time())}',
        ])


if __name__ == "__main__":
    main()
