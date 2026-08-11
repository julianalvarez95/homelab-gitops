# Outreach bot para consultas legales (WhatsApp + Kapso) — Diseño

Fecha: 2026-08-10

## Contexto

Servicio nuevo para la cuñada del usuario (abogada): un bot que le hace
outreach por WhatsApp a una lista determinada de contactos, conversa
para calificar el interés, e intenta cerrar con una reunión
introductoria agendada (ahí se produce la venta real). Es el primer
agente de este repo pensado para un tercero (no para el propio
usuario) y el primero que necesita un componente siempre activo —
hasta ahora todos los agentes (`morning-digest`, `watchdog`) son
`CronJob`s que corren y salen.

Usa [Kapso](https://kapso.ai) como capa de mensajería de WhatsApp
(API sobre WhatsApp Cloud oficial de Meta, sin riesgo de ban). Kapso
provee la infraestructura de mensajería (enviar plantillas, webhooks
de mensajes entrantes) pero **no** aloja lógica conversacional propia
— eso lo corre este agente.

## Alcance

Dentro de alcance:

- Envío diario de un mensaje de plantilla de WhatsApp a contactos
  nuevos de una Google Sheet.
- Recepción de respuestas vía webhook de Kapso y conversación
  calificadora con un LLM.
- Cierre exitoso: el LLM manda un link fijo de agendamiento (Google
  Calendar Appointment o Calendly) cuando decide que tiene contexto
  suficiente.
- Derivación a un humano (la abogada) cuando el LLM no logra cerrar,
  vía notificación de Telegram — reusando el patrón ya usado por
  `morning-digest`/`watchdog`.
- Seguimiento automático (una plantilla de recordatorio) si el
  contacto no responde en unos días.
- Exposición pública del webhook vía Cloudflare Tunnel (primera vez
  que este repo expone algo a internet, no solo a la LAN).

Fuera de alcance (ver sección final para detalle):

- Booking automático contra la Google Calendar API (se usa un link
  fijo de agendamiento en su lugar).
- CRM, multi-idioma, multi-tenant (una sola abogada, una sola lista).
- Automatizar el bump del tag de imagen post-build (Argo Image
  Updater) — se hace a mano al principio, como con los otros agentes.

## Enfoques evaluados

**Empaquetado del agente** (única decisión arquitectónica real: el
repo solo tiene el patrón `CronJob`, y acá hace falta además un
componente siempre activo para el webhook):

- **(A) Una imagen, dos entrypoints.** Un Dockerfile, un
  `requirements.txt`, un build/push en el pipeline de CI. Dos
  workloads de Kubernetes desde la misma imagen: un `CronJob`
  (`sender.py`) y un `Deployment` (`webhook.py`). Comparten los
  módulos cliente (`kapso_client.py`, `sheets_client.py`, `llm.py`).
- **(B) Dos carpetas, dos imágenes independientes**
  (`sender/` y `webhook/`, cada una con su Dockerfile). Más
  aislamiento, pero duplica Dockerfile/requirements y dobla los
  builds del pipeline de CI que se está armando para practicar
  justamente eso.
- **(C) Un único `Deployment` con scheduler interno** (sin `CronJob`
  separado, un timer tipo APScheduler dispara el envío diario dentro
  del mismo proceso que atiende el webhook). Menos objetos de k8s,
  pero se aleja del patrón `CronJob` que ya usa todo el repo y pierde
  la visibilidad de "corrió/no corrió" que da `kubectl get cronjob`.

**Elegido: (A).** Mínima duplicación, un solo build en CI, y mantiene
el `CronJob` para la parte que sí encaja en ese patrón (el envío
diario), separado del `Deployment` para la parte que no (el webhook).

**Agendamiento** — link fijo de agendamiento en vez de que el bot
consulte/reserve directamente contra la Google Calendar API. Evita
manejar OAuth/scopes de calendario real, conflictos de horario y
timezones dentro de la conversación; Google/Calendly ya resuelven
disponibilidad real y generan el link de Meet. Costo: el LLM no puede
confirmar en el momento que la reunión quedó agendada (no hay
callback de "se agendó" en este diseño) — ver "Fuera de alcance".

## Estructura del código

```
agents/outreach-bot/
├── Dockerfile              # una sola imagen, dos entrypoints
├── requirements.txt
├── src/
│   ├── sender.py             # entrypoint del CronJob diario
│   ├── webhook.py            # entrypoint del Deployment (FastAPI)
│   ├── kapso_client.py       # enviar plantilla/respuesta, leer conversación
│   ├── sheets_client.py      # leer/escribir la Google Sheet de contactos
│   ├── llm.py                 # conversación + decisión de cierre/derivación
│   └── telegram.py            # notificación de handoff/calificado (copiado
│                               # de morning-digest, no compartido — mismo
│                               # criterio que watchdog, ver CLAUDE.md)
├── cronjob.yaml
├── deployment.yaml
├── service.yaml
├── cloudflared/                # manifest del Tunnel
└── kustomization.yaml
```

## Esquema de la Google Sheet

Columnas: `nombre`, `telefono`, `status`, `last_update`. `status` es
la máquina de estados completa del contacto:

```
pending ──(sender envía plantilla)──▶ sent
sent ──(sin respuesta X días, propuesto 3)──▶ followed_up
followed_up ──(sin respuesta otros X días)──▶ no_response   [terminal]
sent / followed_up ──(contacto responde)──▶ in_conversation
in_conversation ──(LLM manda link de agenda)──▶ qualified   [terminal]
in_conversation ──(LLM deriva, o tope de 10 mensajes)──▶ handoff   [terminal]
in_conversation ──(LLM detecta rechazo explícito)──▶ not_interested   [terminal]
```

`qualified` y `handoff` disparan notificación de Telegram a la
abogada (con el resumen de la conversación); `not_interested` no —
evita ruido de notificaciones por rechazos que no necesitan su
atención. El historial de mensajes en sí **no** se duplica en la
Sheet: se pide a la API de Kapso cada vez que el webhook necesita
contexto para el LLM (Kapso ya lo guarda).

## Flujo de datos

**A. Envío diario (`sender.py`, CronJob)**

```
Sheet (status="pending") ──▶ sender.py ──envía plantilla──▶ Kapso API
                                    └──actualiza status="sent"──▶ Sheet
Sheet (status="sent"/"followed_up", vencido) ──▶ sender.py ──envía
  plantilla de seguimiento o marca "no_response"──▶ Kapso API / Sheet
sender.py ──push_metrics (patrón morning-digest/watchdog)──▶ VictoriaMetrics
```

**B. Conversación entrante (`webhook.py`, Deployment detrás del
Cloudflare Tunnel)**

```
Contacto responde ──▶ Kapso ──POST webhook──▶ webhook.py
webhook.py:
  1. busca el contacto en la Sheet por teléfono
  2. pide el historial de la conversación a la API de Kapso
  3. llama al LLM (system prompt: calificar y cerrar con reunión
     introductoria; tope duro de 10 mensajes de contexto)
  4. según la decisión del LLM:
     - sigue conversando → responde por Kapso, status="in_conversation"
     - cierra → manda el link de agenda, status="qualified", Telegram
     - deriva (LLM o tope de 10 mensajes) → status="handoff", Telegram
     - detecta rechazo explícito → status="not_interested"
```

El webhook procesa todo dentro de la misma request (llamado al LLM +
respuesta) y devuelve 200 OK a Kapso al final. A esta escala (una
abogada, contactos determinados, no volumen masivo) no hace falta
cola/procesamiento async.

## Manejo de errores

- **Kapso API caída o rate-limited**: no se actualiza `status` en la
  Sheet — la fila queda como estaba y el próximo run del sender la
  reintenta. Se loguea con `resp.raise_for_status()` (nunca fallar en
  silencio, por el gotcha ya documentado en `CLAUDE.md`).
- **Google Sheets API caída**: `sender.py` aborta el run completo (sin
  fuente de contactos no hay qué hacer) y loguea. `webhook.py`, si no
  puede leer la Sheet para un contacto puntual, igual responde al
  contacto con un mensaje genérico de espera en vez de colgar la
  conversación, y loguea el fallo para revisión manual.
- **LLM caída/timeout**: el webhook responde con un mensaje de
  fallback fijo (no requiere plantilla, es dentro de una conversación
  ya abierta) y no cambia `status` — la siguiente respuesta del
  contacto reintenta el flujo completo.
- **Webhook sin firma válida o de un número no reconocido**: se
  descarta devolviendo 200 OK (para que Kapso no reintente en loop) y
  se loguea como evento sospechoso.
- **Cloudflare Tunnel caído**: Kapso reintenta el webhook según su
  propia política de reintentos; el sender no depende del tunnel y
  sigue funcionando igual.

## Observabilidad

Reusa la infra que ya está corriendo en este cluster, sin agregar
piezas nuevas:

- Métricas a VictoriaMetrics (`infra/victoria-metrics`) vía
  `push_metrics`: mensajes enviados/día, leads calificados, leads
  derivados, tasa de no-respuesta.
- Trazas del LLM a Phoenix (`infra/phoenix`), igual que
  `morning-digest`.

## Secretos

Creados a mano en el cluster (`kubectl create secret`), nunca en git,
según la convención de este repo:

- API key de Kapso
- Credenciales de service account de Google (scope de Sheets
  solamente — no hace falta Calendar API por la decisión de usar un
  link fijo de agendamiento)
- API key del LLM
- Token y chat id de Telegram
- Token del Cloudflare Tunnel

## Rollout / CI-CD

1. GitHub Actions: lint + build de la imagen en cada push.
2. Push a GHCR en merge a `main`.
3. Bump manual del tag de imagen en `cronjob.yaml`/`deployment.yaml`
   (commit del usuario), igual que los agentes existentes hoy —
   automatizarlo con Argo Image Updater queda para más adelante.
4. Antes de apuntar a contactos reales: probar el webhook con un
   número de prueba propio, y confirmar que la plantilla de WhatsApp
   ya fue aprobada por Meta vía Kapso (la aprobación puede demorar,
   conviene mandarla a revisión temprano).

## Fuera de alcance (por ahora)

- Confirmación de que la reunión quedó efectivamente agendada (no hay
  callback desde Google Calendar/Calendly hacia el bot en este
  diseño) — `status="qualified"` significa "se mandó el link", no
  "se confirmó el horario". Si se vuelve necesario saber esto, requiere
  webhook de Calendar o revisar la Calendar API — evaluar cuando
  aparezca la necesidad real, no antes.
- Booking automático contra la Google Calendar API.
- CRM, multi-idioma, multi-tenant.
- Automatizar el bump de tag de imagen post-build.
- Cola/procesamiento async del webhook (no hace falta al volumen
  actual).
