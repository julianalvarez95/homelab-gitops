import os
import re
import time
from contextlib import nullcontext
from html.parser import HTMLParser
import feedparser
import yaml
from bs4 import BeautifulSoup
from openai import OpenAI
import requests
from datetime import datetime, timedelta, timezone

REDDIT_USER_AGENT = "homelab-morning-digest/1.0"

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
VICTORIA_METRICS_URL = os.environ.get("VICTORIA_METRICS_URL")
DIGEST_AGENT_URL = os.environ.get("DIGEST_AGENT_URL", "https://digest-agent.vercel.app/ingest")
DIGEST_INGEST_SECRET = os.environ.get("DIGEST_INGEST_SECRET")

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


def _fetch_rss(feed, cutoff):
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
            "category": feed.get("category", "tech"),
            "title": entry.get("title", ""),
            "summary": strip_html(entry.get("summary", ""))[:400],
            "link": entry.get("link", ""),
        }))
    return feed_items


def _fetch_reddit(feed, cutoff):
    try:
        resp = requests.get(
            feed["url"],
            headers={"User-Agent": REDDIT_USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        posts = resp.json()["data"]["children"]
    except Exception as e:
        print(f"Reddit ({feed['name']}) falló, la salteo: {e}")
        return []

    feed_items = []
    for child in posts:
        post = child.get("data", {})
        created = post.get("created_utc")
        pub_dt = datetime.fromtimestamp(created, tz=timezone.utc) if created else None
        if pub_dt and pub_dt < cutoff:
            continue

        if post.get("is_self"):
            link = f"https://www.reddit.com{post.get('permalink', '')}"
            summary = strip_html(post.get("selftext", ""))[:400]
        else:
            link = post.get("url", "")
            summary = ""

        feed_items.append((pub_dt, {
            "source": feed["name"],
            "category": feed.get("category", "tech"),
            "title": post.get("title", ""),
            "summary": summary,
            "link": link,
        }))
    return feed_items


def _fetch_github_trending(feed, cutoff):
    try:
        resp = requests.get(feed["url"], timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        articles = soup.select("article.Box-row")
    except Exception as e:
        print(f"GitHub Trending ({feed['name']}) falló, la salteo: {e}")
        return []

    feed_items = []
    for article in articles:
        link_tag = article.select_one("h2 a[href]")
        if not link_tag:
            continue
        repo = link_tag["href"].strip("/")

        desc_tag = article.select_one("p.col-9")
        description = desc_tag.get_text(strip=True) if desc_tag else ""
        lang_tag = article.select_one('span[itemprop="programmingLanguage"]')
        language = lang_tag.get_text(strip=True) if lang_tag else ""
        stars_tag = article.select_one("span.float-sm-right")
        stars_today = stars_tag.get_text(strip=True) if stars_tag else ""

        summary = " · ".join(p for p in (description, language, stars_today) if p)
        feed_items.append((None, {
            "source": feed["name"],
            "category": feed.get("category", "tech"),
            "title": repo,
            "summary": summary[:400],
            "link": f"https://github.com/{repo}",
        }))
    return feed_items


_FETCHERS = {
    "rss": _fetch_rss,
    "reddit": _fetch_reddit,
    "github_trending": _fetch_github_trending,
}


def fetch_rss_items(max_age_hours=24, max_items_per_feed=12):
    with open("/config/feeds.yaml") as f:
        feeds = yaml.safe_load(f)["feeds"]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items = []
    for feed in feeds:
        feed_type = feed.get("type", "rss")
        fetch = _FETCHERS.get(feed_type)
        if fetch is None:
            print(f"Tipo de fuente desconocido '{feed_type}' en '{feed['name']}', la salteo")
            continue
        feed_items = fetch(feed, cutoff)

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

Cada noticia viene etiquetada con su categoría entre corchetes (tech,
agro, trading-ar, ai-observabilidad). Las fuentes en inglés de tech
suelen ser más numerosas que las demás categorías — no dejes que ese
volumen tape a las otras: si hay contenido relevante de agro, trading-ar
o ai-observabilidad, asegurate de incluir al menos un bloque para cada
una de esas categorías que tenga noticias ese día, aunque tech ocupe más
bloques en total.

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
        return "No hay novedades hoy.", 0

    content = "\n\n".join(
        f"[{i['category']}][{i['source']}] {i['title']}\n{i['summary']}\nLink: {i['link']}"
        for i in items
    )
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    )
    tokens = response.usage.total_tokens if response.usage else 0
    return response.choices[0].message.content, tokens


_ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre"}
_TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)[^>]*>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def sanitize_telegram_html(text):
    text = _BR_RE.sub("\n", text)

    def _replace(match):
        tag = match.group(1).lower()
        return match.group(0) if tag in _ALLOWED_TAGS else ""

    return _TAG_RE.sub(_replace, text)


# Matches the bullet format the SYSTEM_PROMPT requires:
# • <a href="LINK">Título</a>: comentario
# Reused to feed digest-agent the same items that went out to Telegram,
# instead of re-summarizing the raw RSS items a second time.
_BULLET_RE = re.compile(r'•\s*<a href="([^"]+)">(.*?)</a>:\s*(.*?)(?=\n•|\n\n|\Z)', re.DOTALL)


def extract_digest_items(digest_text, source_by_link):
    items = []
    for link, headline, summary in _BULLET_RE.findall(digest_text):
        items.append({
            "headline": re.sub(r"\s+", " ", headline).strip(),
            "summary": re.sub(r"\s+", " ", summary).strip(),
            "source": source_by_link.get(link, "morning-digest"),
            "url": link,
        })
    return items


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


def push_metrics(success, duration, tokens):
    # Igual que la telemetría de tracing: falla en modo abierto. Un
    # VictoriaMetrics caído no puede tirar abajo la corrida del agente.
    if not VICTORIA_METRICS_URL:
        return
    lines = "\n".join([
        f'agent_run_success{{agent="morning-digest"}} {int(success)}',
        f'agent_run_duration_seconds{{agent="morning-digest"}} {duration:.2f}',
        f'agent_llm_tokens_total{{agent="morning-digest"}} {tokens}',
        f'agent_last_run_timestamp_seconds{{agent="morning-digest"}} {int(time.time())}',
    ])
    try:
        resp = requests.post(VICTORIA_METRICS_URL, data=lines, timeout=3)
        print(f"VictoriaMetrics response: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    except Exception as e:
        print(f"No se pudieron reportar métricas, sigo igual: {e}")


def push_portfolio_digest(generated_at, items):
    # Igual que tracing/push_metrics: mejor esfuerzo. El digest de Telegram
    # ya salió para cuando esto corre, así que digest-agent caído o el
    # secret sin configurar no puede tirar abajo la corrida.
    if not DIGEST_INGEST_SECRET or not items:
        return
    try:
        resp = requests.post(
            DIGEST_AGENT_URL,
            json={"generatedAt": generated_at, "items": items},
            headers={"X-Digest-Secret": DIGEST_INGEST_SECRET},
            timeout=10,
        )
        print(f"digest-agent response: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    except Exception as e:
        print(f"No se pudo publicar el digest en digest-agent, sigo igual: {e}")


def main():
    start = time.time()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    success = False
    tokens = 0
    try:
        with _span("morning_digest_run"):
            print("Buscando items de RSS...")
            with _span("fetch_rss"):
                rss_items = fetch_rss_items()
            print(f"RSS: {len(rss_items)} items encontrados")

            print(f"Total items: {len(rss_items)}. Resumiendo...")
            digest, tokens = summarize(rss_items)
            print(f"Digest generado ({len(digest)} caracteres). Enviando a Telegram...")
            with _span("send_telegram"):
                send_telegram(f"📰 Resumen matutino\n\n{digest}")
            print("Listo.")

            with _span("push_portfolio_digest"):
                source_by_link = {item["link"]: item["source"] for item in rss_items}
                portfolio_items = extract_digest_items(digest, source_by_link)
                push_portfolio_digest(generated_at, portfolio_items)
        success = True
    finally:
        push_metrics(success, time.time() - start, tokens)


if __name__ == "__main__":
    main()
