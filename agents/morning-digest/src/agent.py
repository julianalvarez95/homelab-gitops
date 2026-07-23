import os
import re
from contextlib import nullcontext
from html.parser import HTMLParser
import feedparser
import yaml
from openai import OpenAI
import requests
from datetime import datetime, timedelta, timezone

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

client = OpenAI(api_key=OPENAI_API_KEY)

# Tracing is best-effort: if Phoenix is unreachable or setup fails, the
# digest still has to go out. `tracer` stays None and every span below
# becomes a no-op via nullcontext().
tracer = None
try:
    from phoenix.otel import register
    from openinference.instrumentation.openai import OpenAIInstrumentor
    from opentelemetry import trace

    _tracer_provider = register(batch=False, project_name="morning-digest")
    OpenAIInstrumentor().instrument(tracer_provider=_tracer_provider)
    tracer = trace.get_tracer(__name__)
except Exception as e:
    print(f"Tracing no disponible, sigo sin instrumentación: {e}")


def _span(name):
    return tracer.start_as_current_span(name) if tracer else nullcontext()


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []

    def handle_data(self, data):
        self.chunks.append(data)


def strip_html(raw_html):
    parser = _TextExtractor()
    parser.feed(raw_html)
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.chunks)).strip()


def fetch_rss_items(max_age_hours=24, max_items_per_feed=12):
    with open("/config/feeds.yaml") as f:
        feeds = yaml.safe_load(f)["feeds"]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items = []
    for feed in feeds:
        parsed = feedparser.parse(feed["url"])
        feed_items = []
        for entry in parsed.entries:
            published = entry.get("published_parsed")
            pub_dt = None
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            feed_items.append((pub_dt, {
                "source": feed["name"],
                "title": entry.get("title", ""),
                "summary": strip_html(entry.get("summary", ""))[:400],
                "link": entry.get("link", ""),
            }))

        dated = sorted(
            (pair for pair in feed_items if pair[0] is not None),
            key=lambda pair: pair[0],
            reverse=True,
        )
        undated = [pair for pair in feed_items if pair[0] is None]
        items.extend(item for _, item in (dated + undated)[:max_items_per_feed])
    return items


SYSTEM_PROMPT = """\
Sos un asistente que arma el resumen matutino de noticias para enviar por
Telegram, en español rioplatense, con tono directo y ágil.

Agrupá las noticias por tema (no por fuente) y priorizá lo más relevante;
descartá lo irrelevante, duplicado o de bajo interés. Cubrí tantos temas
como sea razonable dado el volumen de noticias, apuntando a un total de
entre 600 y 800 palabras.

FORMATO DE SALIDA (se envía con parse_mode=HTML de Telegram, que sólo
soporta un subconjunto muy chico de HTML — cualquier etiqueta no permitida
hace que Telegram rechace el mensaje completo):

- Etiquetas permitidas, únicamente: <b>, <i>, <u>, <s>, <a href="URL">,
  <code>, <pre>. No uses ninguna otra etiqueta (nada de <div>, <p>, <ul>,
  <li>, <ol>, <br>, <h1>-<h6>, etc.).
- Los saltos de línea son saltos de línea de texto plano, nunca <br>.
- Las viñetas son texto literal "• " al inicio del renglón, nunca <ul>/<li>.
- Armá un bloque por tema, con este formato exacto:
    <b>Nombre del tema</b>
    • <a href="LINK_DE_LA_NOTICIA">Título corto de la noticia</a>: comentario
    de una línea sobre por qué importa.
    • <a href="LINK_DE_LA_NOTICIA">Título corto de otra noticia</a>: comentario.
- Separá cada bloque de tema del siguiente con una línea en blanco.
- Cada bloque tiene que ser autocontenido: toda etiqueta que abrís se cierra
  dentro del mismo bloque, nunca a medio cerrar entre un bloque y el
  siguiente (el mensaje puede partirse en varios envíos de Telegram por el
  límite de caracteres, y el corte siempre va a caer entre bloques).
- Usá el link real de cada noticia que te paso en el texto de entrada; si
  una noticia no tiene link, mencionala sin la etiqueta <a>.
"""


def summarize(items):
    if not items:
        return "No hay novedades hoy."

    content = "\n\n".join(
        f"[{i['source']}] {i['title']}\n{i['summary']}\nLink: {i['link']}"
        for i in items
    )
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    )
    return response.choices[0].message.content


_ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre"}
_TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)[^>]*>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def sanitize_telegram_html(text):
    text = _BR_RE.sub("\n", text)

    def _replace(match):
        tag = match.group(1).lower()
        return match.group(0) if tag in _ALLOWED_TAGS else ""

    return _TAG_RE.sub(_replace, text)


def _pack(pieces, max_chars, separator):
    chunks = []
    current = ""
    for piece in pieces:
        candidate = f"{current}{separator}{piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def split_telegram_message(text, max_chars=3500):
    if len(text) <= max_chars:
        return [text]

    chunks = []
    for block in _pack(text.split("\n\n"), max_chars, "\n\n"):
        if len(block) <= max_chars:
            chunks.append(block)
            continue
        # a single topic-block still overflows: fall back to bullet lines
        for sub in _pack(block.split("\n"), max_chars, "\n"):
            if len(sub) <= max_chars:
                chunks.append(sub)
            else:
                # a single line has no more separators: hard-slice as a
                # last resort so this never recurses/loops indefinitely
                chunks.extend(
                    sub[i:i + max_chars] for i in range(0, len(sub), max_chars)
                )
    return chunks


def send_telegram(text):
    token = TELEGRAM_BOT_TOKEN.removeprefix("bot")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    sanitized = sanitize_telegram_html(text)
    for chunk in split_telegram_message(sanitized):
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
        })
        print(f"Telegram response: {resp.status_code} {resp.text}")
        resp.raise_for_status()


def main():
    with _span("morning_digest_run"):
        print("Buscando items de RSS...")
        with _span("fetch_rss"):
            rss_items = fetch_rss_items()
        print(f"RSS: {len(rss_items)} items encontrados")

        all_items = rss_items
        print(f"Total items: {len(all_items)}. Resumiendo...")
        digest = summarize(all_items)
        print(f"Digest generado ({len(digest)} caracteres). Enviando a Telegram...")
        with _span("send_telegram"):
            send_telegram(f"📰 Resumen matutino\n\n{digest}")
        print("Listo.")


if __name__ == "__main__":
    main()
