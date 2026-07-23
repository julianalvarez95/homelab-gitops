# Telemetría para homelab-gitops — Diseño

Fecha: 2026-07-23

## Contexto

El repo tiene un único agente en producción (`morning-digest`) corriendo
como CronJob. `infra/` está reservado en el `CLAUDE.md` para "Arize
Phoenix, VictoriaMetrics", pero está vacío — nunca se implementó. Este
documento diseña esa pieza.

## Motivación, en orden de prioridad

1. **Debugging de agentes.** Ya hubo un incidente real (commit
   `4313ce3`) donde una entrega de Telegram falló en silencio: el job
   terminó `Completed`, exit 0, pero no llegó el mensaje. Sin tracing,
   ese tipo de falla es invisible.
2. **Costo/uso de LLM.** Ver tokens y llamadas por agente para no
   llevarse sorpresas de facturación a medida que se sumen más agentes.
3. **Salud del node.** CPU/memoria/disco del Latitude 7490 y si los
   CronJobs corrieron a horario — la prioridad más baja de las tres.

## Restricciones de recursos (relevadas en vivo contra el cluster)

- Node único `homelab`: 15Gi RAM (10Gi libres), 8 cores, `k3s v1.36.2`.
- **Disco: 12Gi libres en `/`** — es la restricción real del diseño, no
  RAM/CPU. Retención corta y PVCs chicos son mandatorios.
- `local-path-provisioner` es el único StorageClass disponible (hostPath
  sobre el disco del node, no hay almacenamiento externo).
- Traefik ya corre como Ingress controller (`kube-system`), reusable
  para exponer las UIs nuevas.
- Los `Application` de ArgoCD se aplican a mano con `kubectl` (no viven
  en git todavía — `apps/` sigue vacío) y siguen el patrón de
  `morning-digest`: `syncPolicy.automated.{prune,selfHeal}` +
  `CreateNamespace=true`.

## Arquitectura: dual-write directo, sin collector

Se evaluaron tres enfoques:

- **(A) OTel Collector como hub** — más estándar/extensible, pero suma
  un servicio persistente 24/7 más en un node ya ajustado de disco.
- **(B) Dual-write directo** — cada agente exporta traces directo al
  endpoint OTLP nativo de Phoenix, y al final de la corrida hace un
  `POST` directo a la API de import de VictoriaMetrics. Sin pieza
  intermedia.
- **(C) Solo Phoenix, métricas para después** — cubre prioridad 1 nada
  más, no cumple con costo ni salud de node.

**Elegido: (B).** Coherente con el principio del repo ("el hardware
local solo orquesta, nada corre de más"), y cubre las tres prioridades
sin agregar un componente extra al cluster.

La excepción es **Fase 3 (node-exporter)**: como es un proceso
persistente (no un CronJob efímero), tiene sentido que VictoriaMetrics
lo *scrapee* directamente (modelo pull, como Prometheus) en lugar de que
node-exporter empuje datos. VictoriaMetrics single-node soporta scrape
config y remote-write/import al mismo tiempo, así que ambos modelos
conviven sin problema.

## Namespace y layout

Namespace nuevo: `observability`. Layout de repo, calcando el patrón de
`agents/`:

```
infra/
  phoenix/
    kustomization.yaml
    deployment.yaml       # Deployment + Service + PVC
  victoria-metrics/
    kustomization.yaml
    deployment.yaml       # Deployment + Service + PVC
  node-exporter/
    kustomization.yaml
    daemonset.yaml        # DaemonSet (un solo node hoy, pero es 1:1 con nodos)
  ingress.yaml            # IngressRoute de Traefik para ambas UIs
```

Cada componente se registra con su propio `Application` de ArgoCD
(aplicado a mano, mismo patrón que `morning-digest`), apuntando a su
subcarpeta bajo `infra/`.

## Fase 1 — Phoenix (tracing de LLM)

**Objetivo:** ver cada corrida de `morning-digest` como un trace: fetch
de RSS → llamada a OpenAI (prompt, respuesta, tokens) → entrega a
Telegram, con éxito/error en cada paso.

- Imagen: `arizephoenix/phoenix` (self-hosted OSS), un solo `Deployment`
  replica=1 en `observability`.
  - Persistencia: PVC de **3Gi** (las traces guardan texto completo de
    prompt/respuesta, pesan más que métricas puras).
  - Retención: configurar el límite de espacio/edad de Phoenix para
    apuntar a ~14 días — verificar el nombre exacto de la env var
    (`PHOENIX_*`) contra la versión de imagen que se baje al
    implementar, porque cambió entre versiones de Phoenix.
  - Sin autenticación propia (confía en el perímetro de la LAN de casa,
    igual que el resto del acceso a este cluster).
- Instrumentación de `morning-digest`:
  - Agregar `openinference-instrumentation-openai` +
    `opentelemetry-exporter-otlp` a `requirements.txt` (pinneados,
    siguiendo la convención ya establecida en este repo tras el
    incidente de `httpx`).
  - En `agent.py`, inicializar el tracer OTLP apuntando a
    `PHOENIX_COLLECTOR_ENDPOINT` (nueva env var, Service
    `phoenix.observability.svc.cluster.local:4317`), instrumentar el
    cliente de OpenAI automáticamente vía el instrumentor de
    OpenInference (esto ya captura tokens de prompt/completion como
    atributos del span, cubriendo parte de la prioridad 2 sin trabajo
    extra).
  - Envolver el paso de fetch RSS y el de entrega a Telegram en spans
    manuales (`tracer.start_as_current_span`) para que el trace
    completo de una corrida sea navegable de punta a punta en la UI de
    Phoenix.

## Fase 2 — VictoriaMetrics (métricas de costo y heartbeat)

**Objetivo:** métricas time-series consultables (tokens totales por
agente, duración de corrida, éxito/error, timestamp de última corrida)
para responder "¿cuánto gasté este mes?" y "¿corrió el cron hoy?".

- `Deployment` single-node de `victoriametrics/victoria-metrics`,
  replica=1, en `observability`.
  - Persistencia: PVC de **2Gi**.
  - Retención: `-retentionPeriod=14d`.
- Al final de cada corrida, `agent.py` hace un `POST` a
  `http://victoria-metrics.observability.svc.cluster.local:8428/api/v1/import/prometheus`
  con líneas tipo:
  ```
  agent_run_success{agent="morning-digest"} 1
  agent_run_duration_seconds{agent="morning-digest"} 12.4
  agent_llm_tokens_total{agent="morning-digest"} 1830
  agent_last_run_timestamp_seconds{agent="morning-digest"} 1753260000
  ```
  Igual que con Telegram, el `POST` debe loguear la respuesta y hacer
  `raise_for_status()` — no repetir el error silencioso del incidente
  anterior.
- Consulta: vía `vmui` (UI embebida en VictoriaMetrics, no hace falta
  Grafana para esta fase — se evalúa sumarlo más adelante si el volumen
  de dashboards lo justifica).
- Alerting (ej. avisar si `agent_last_run_timestamp_seconds` no se
  actualizó en 24h) queda **fuera de alcance** de esta fase — requeriría
  `vmalert` u otro componente persistente adicional; se revisita si
  hace falta.

## Fase 3 — node-exporter (salud del node)

**Objetivo:** CPU/memoria/disco del `homelab` node en el tiempo (hoy
sólo existe `kubectl top`, sin historial).

- `node-exporter` como `DaemonSet` en `observability` (1:1 con nodos;
  hoy es un solo pod, pero el manifiesto queda correcto si algún día se
  suma un segundo node).
- VictoriaMetrics (ya desplegado en Fase 2) agrega un `scrape_config`
  apuntando al Service de `node-exporter` — acá sí es scrape/pull,
  porque node-exporter es un proceso persistente, no un CronJob
  efímero (ver sección de arquitectura).
- Sin PVC nuevo — reusa el mismo VictoriaMetrics de Fase 2.

## Acceso a las UIs

Traefik `IngressRoute` (ya corre en `kube-system`) para:

- `phoenix.homelab.local` → Service de Phoenix, puerto UI (6006).
- `metrics.homelab.local` → Service de VictoriaMetrics, puerto `vmui`
  (8428).

Ambos hostnames resuelven sólo dentro de la LAN de casa (entrada
manual en `/etc/hosts` del lado cliente, o quien administre DNS local);
no hay exposición pública. Sin TLS ni auth propios en esta fase — mismo
modelo de confianza que el resto del cluster (perímetro de red, no
autenticación de aplicación).

## Retención y disco — presupuesto

| Componente        | PVC   | Retención |
|-------------------|-------|-----------|
| Phoenix            | 3Gi   | ~14 días (a confirmar env var exacta) |
| VictoriaMetrics    | 2Gi   | 14 días (`-retentionPeriod=14d`) |
| node-exporter      | —     | (sin persistencia propia) |
| **Total**          | 5Gi   | de 12Gi libres hoy |

Deja ~7Gi de margen sobre el disco actual del node para el resto del
cluster (imágenes de contenedores, logs, etc.) — ajustado pero
viable dado el volumen bajo esperado (un agente, una corrida diaria).

## Testing / validación

- **Fase 1:** correr `morning-digest` manualmente (`kubectl create job
  --from=cronjob/morning-digest`) y verificar que el trace completo
  (RSS → OpenAI → Telegram) aparece en la UI de Phoenix con los tres
  spans y los tokens de la llamada a OpenAI visibles.
- **Fase 2:** después de una corrida, consultar `vmui` y confirmar que
  `agent_run_success`, `agent_llm_tokens_total` y
  `agent_last_run_timestamp_seconds` tienen el valor esperado.
- **Fase 3:** confirmar en `vmui` que aparecen series de `node_exporter`
  (ej. `node_memory_MemAvailable_bytes`) para el node `homelab`.
- En cada fase, verificar uso real de disco (`df -h` en el node) contra
  el presupuesto de la tabla anterior antes de dar la fase por cerrada.

## Fuera de alcance (por ahora)

- Grafana (se evalúa si `vmui` se queda corto).
- Alerting activo (`vmalert`, notificaciones a Telegram por métricas).
- TLS/auth en las UIs expuestas.
- Instrumentación de agentes que todavía no existen — el patrón de Fase
  1 (instrumentor de OpenInference + spans manuales) es el que se
  replica cuando se sume el próximo agente.
