import os
import imaplib
import email
from email.header import decode_header
import feedparser
import yaml
from openai import OpenAI
import requests
from datetime import datetime, timedelta, timezone

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "Newsletter")
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

client = OpenAI(api_key=OPENAI_API_KEY)


def fetch_rss_items(max_age_hours=24):
    with open("/config/feeds.yaml") as f:
        feeds = yaml.safe_load(f)["feeds"]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items = []
    for feed in feeds:
        parsed = feedparser.parse(feed["url"])
        for entry in parsed.entries:
            published = entry.get("published_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            items.append({
                "source": feed["name"],
                "title": entry.get("title", ""),
                "summary": entry.get("summary", "")[:500],
                "link": entry.get("link", ""),
            })
    return items


def fetch_gmail_items():
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    imap.select(f'"{GMAIL_LABEL}"')

    status, msg_ids = imap.search(None, "UNSEEN")
    items = []
    for msg_id in msg_ids[0].split():
        _, data = imap.fetch(msg_id, "(RFC822)")
        msg = email.message_from_bytes(data[0][1])
        subject, encoding = decode_header(msg["Subject"])[0]
        if isinstance(subject, bytes):
            subject = subject.decode(encoding or "utf-8")

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode(errors="ignore")
                    break
        else:
            body = msg.get_payload(decode=True).decode(errors="ignore")

        items.append({
            "source": msg.get("From", ""),
            "title": subject,
            "summary": body[:1000],
            "link": "",
        })
        imap.store(msg_id, "+FLAGS", "\\Seen")

    imap.logout()
    return items


def summarize(items):
    if not items:
        return "No hay novedades hoy."

    content = "\n\n".join(
        f"[{i['source']}] {i['title']}\n{i['summary']}" for i in items
    )
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": (
                "Sos un asistente que arma resúmenes matutinos concisos en "
                "español rioplatense. Agrupá por tema, priorizá lo más "
                "relevante, máximo 300 palabras, formato para leer rápido "
                "en el celular."
            )},
            {"role": "user", "content": content},
        ],
    )
    return response.choices[0].message.content


def send_telegram(text):
    token = TELEGRAM_BOT_TOKEN.removeprefix("bot")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    print(f"Telegram response: {resp.status_code} {resp.text}")
    resp.raise_for_status()


def main():
    print("Buscando items de RSS...")
    rss_items = fetch_rss_items()
    print(f"RSS: {len(rss_items)} items encontrados")

    print("Buscando items de Gmail...")
    try:
        gmail_items = fetch_gmail_items()
        print(f"Gmail: {len(gmail_items)} items encontrados")
    except Exception as e:
        print(f"Error en Gmail: {e}")
        gmail_items = []

    all_items = rss_items + gmail_items
    print(f"Total items: {len(all_items)}. Resumiendo...")
    digest = summarize(all_items)
    print(f"Digest generado ({len(digest)} caracteres). Enviando a Telegram...")
    send_telegram(f"📰 Resumen matutino\n\n{digest}")
    print("Listo.")


if __name__ == "__main__":
    main()
