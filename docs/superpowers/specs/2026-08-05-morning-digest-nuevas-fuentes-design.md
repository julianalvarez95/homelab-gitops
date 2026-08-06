# Morning-digest: sumar fuentes no-RSS (Reddit + GitHub Trending) — Diseño

Fecha: 2026-08-05

## Contexto

`agents/morning-digest` corre hoy como CronJob diario: lee 13 feeds RSS
definidos en `feeds.yaml` (categorías `tech`, `agro`, `trading-ar`,
`ai-observabilidad`), arma un resumen con `gpt-4o-mini` agrupado por
tema, y lo entrega por Telegram. Es la única fuente de contenido:
`feedparser` contra URLs RSS/Atom.

Este documento diseña la primera evolución del agente: sumar dos
fuentes que no exponen RSS — Reddit (subreddits específicos) y GitHub
Trending — sin tocar el pipeline de resumen, sanitización de Telegram
ni métricas que ya funcionan.

## Alcance

Dentro de alcance:

- Fetchers nuevos para Reddit y GitHub Trending, integrados al mismo
  flujo de items que ya consume `summarize()`.
- Extensión mínima del esquema de `feeds.yaml` para soportar fuentes de
  distinto tipo.

Fuera de alcance (evoluciones futuras, no esta):

- Interactividad (responder preguntas sobre el digest por Telegram).
- Dedup semántico entre fuentes.
- Nuevas categorías (todo lo nuevo entra en `tech`).
- Otras fuentes no-RSS evaluadas y descartadas por ahora: Twitter/X
  (requiere API paga o scraping frágil), YouTube (requiere API key y
  cuota propia).

## Enfoques evaluados

- **(A) Extender `feeds.yaml` con un campo `type`.** Cada entrada gana
  un campo opcional `type: rss | reddit | github_trending` (default
  `rss`, retrocompatible con las 13 fuentes actuales sin tocarlas). El
  fetcher se convierte en un dispatcher chico; los tres tipos devuelven
  el mismo shape de item que ya consume `summarize()`.
- **(B) Config y fetchers separados por tipo** (`reddit.yaml` propio,
  llamadas explícitas en `main()`). Más explícito pero duplica la
  lógica de ventana de 24h y límite de items por fuente en cada tipo.
- **(C) Registro de "plugins" de fuentes**, genérico para que sumar
  YouTube/Twitter después sea trivial. Over-engineering para dos
  fuentes nuevas — mismo criterio que ya aplicó este repo para no
  compartir código entre agentes hasta el agente #3 (ver `CLAUDE.md`).

**Elegido: (A).** Mínimo código nuevo, cero riesgo para el pipeline que
ya funciona, no diseña para necesidades hipotéticas.

## Esquema de `feeds.yaml`

Se agrega el campo opcional `type` (default `"rss"` si no está
presente — las 13 entradas actuales no se modifican). El campo `url`
sigue siendo "lo que el fetcher pega", interpretado distinto según
`type`:

```yaml
feeds:
  - name: "Hacker News"                    # sin cambios, type implícito = rss
    url: "https://news.ycombinator.com/rss"
    category: "tech"

  - name: "r/programming"
    url: "https://www.reddit.com/r/programming/top.json?t=day&limit=15"
    category: "tech"
    type: reddit
  - name: "r/MachineLearning"
    url: "https://www.reddit.com/r/MachineLearning/top.json?t=day&limit=15"
    category: "tech"
    type: reddit
  - name: "r/LocalLLaMA"
    url: "https://www.reddit.com/r/LocalLLaMA/top.json?t=day&limit=15"
    category: "tech"
    type: reddit
  - name: "r/selfhosted"
    url: "https://www.reddit.com/r/selfhosted/top.json?t=day&limit=15"
    category: "tech"
    type: reddit

  - name: "GitHub Trending"
    url: "https://github.com/trending?since=daily"
    category: "tech"
    type: github_trending
```

Los 4 subreddits son un default propuesto (incluye `r/selfhosted` por
afinidad con el propio homelab) — ajustables en la revisión del spec o
más adelante sin tocar código, solo `feeds.yaml`.

## Fetchers nuevos

`fetch_rss_items` (en `agent.py`) se convierte en un dispatcher: por
cada entrada de `feeds.yaml` llama a `_fetch_rss`, `_fetch_reddit` o
`_fetch_github_trending` según `feed.get("type", "rss")`. Las tres
funciones devuelven una lista de tuplas `(pub_dt_or_None, item_dict)`
con el mismo shape que ya usa `summarize()` (`source`, `category`,
`title`, `summary`, `link`). El loop externo que aplica la ventana de
24h, ordena por fecha y trunca a `max_items_per_feed` **no cambia** —
es agnóstico al tipo de fuente.

**Nota sobre el orden de Reddit:** `top.json?t=day` ya devuelve los
posts ordenados por score, pero el loop externo los re-ordena por
`created_utc` (recencia) antes de truncar a `max_items_per_feed`. Con
`limit=15` en la URL y `max_items_per_feed=12`, el efecto práctico es
perder como máximo los 3 posts más viejos del día, sin importar su
score — aceptable dado el volumen (sería sobre-ingeniería filtrar por
score client-side para 3 items de diferencia), pero es una pérdida de
precisión real, no un no-op.

- **`_fetch_reddit(feed, cutoff)`**: `GET` al JSON endpoint en `url`
  (público, sin auth). De cada `data.children[].data` toma `title`,
  `score`, `created_utc` (para filtrar contra `cutoff`, igual que RSS),
  y arma el link:
  - si el post no es self-post (`is_self == False`), linkea al
    artículo externo (`data.url`) — mismo criterio que RSS, que linkea
    a la fuente original;
  - si es self-post, linkea al hilo de Reddit (`permalink`).
  - `summary` = el `selftext` truncado si es self-post, o vacío si no
    (igual límite de 400 caracteres que ya usa `strip_html` para RSS).

- **`_fetch_github_trending(feed, cutoff)`**: `GET` a `url`
  (`github.com/trending?since=daily`) y scrapea el HTML con
  `beautifulsoup4` (dependencia nueva, pineada en `requirements.txt`
  como marca el gotcha de `httpx`/`wrapt` en `CLAUDE.md`). Cada
  `<article class="Box-row">` da un repo: `title` = `owner/repo`,
  `summary` = descripción + lenguaje + estrellas ganadas hoy, `link` =
  URL del repo. GitHub Trending no tiene fecha por item (es "hoy" por
  construcción del propio listado) — no se filtra contra `cutoff`.

Ambas fuentes quedan con `category: "tech"` — no se toca `SYSTEM_PROMPT`
(ya menciona las 4 categorías existentes por nombre; ninguna nueva se
agrega).

## Manejo de errores

Cada fetcher nuevo (`_fetch_reddit`, `_fetch_github_trending`) envuelve
su request en try/except: si Reddit devuelve 429/403, o GitHub cambia
el markup del scraping, esa fuente puntual se loguea y se salta — el
resto de `feeds.yaml` sigue procesándose normalmente. Mismo criterio de
"falla abierta" que ya usan tracing y métricas en este agente (ver
`CLAUDE.md`): una fuente caída no puede tirar abajo la entrega del
digest completo.

**Riesgo a verificar en el primer test real:** Reddit exige un
`User-Agent` descriptivo en el request o responde 429 — no alcanza con
el default de `requests`. Usar algo como
`f"homelab-morning-digest/1.0 (contact: <telegram o email de contacto>)"`.

**Riesgo aceptado, no mitigado en este diseño:** el scraping de GitHub
Trending es contra HTML no versionado (no hay API oficial) — si GitHub
cambia el markup, `_fetch_github_trending` empieza a devolver 0 items
silenciosamente (capturado por el try/except de arriba, no rompe la
corrida, pero tampoco se nota sin mirar los logs). Aceptable dado el
volumen del proyecto (un agente, sin CI); se revisita si se vuelve
molesto.

## Testing / validación

Sin CI en este repo — validación manual:

1. Agregar las fuentes nuevas a `feeds.yaml` (ver esquema arriba).
2. Correr `agent.py` local o triggerear el CronJob a mano
   (`kubectl create job --from=cronjob/morning-digest ...`).
3. Confirmar en logs que `_fetch_reddit` y `_fetch_github_trending`
   devuelven items (no 0 por rate-limit o markup roto).
4. Confirmar que el digest de Telegram incluye contenido de Reddit/GH
   dentro del bloque "tech", sin romper el sanitizado HTML (nombres de
   repo con caracteres raros, links largos).
5. Confirmar que el bloque "tech" no se dispara de tamaño y el digest
   se mantiene en el rango de 600–800 palabras que pide el prompt pese
   al volumen extra de items.

## Fuera de alcance (por ahora)

- Nuevas categorías para Reddit/GitHub Trending (quedan en `tech`).
- Dedup entre Reddit/GitHub/RSS cuando cubren la misma noticia.
- Filtro configurable de subreddits/lenguajes vía env var (hoy es fijo
  en `feeds.yaml`, que ya es editable sin rebuild vía ConfigMap).
- Interactividad post-digest (preguntas por Telegram).
