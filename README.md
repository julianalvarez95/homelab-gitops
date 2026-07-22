# homelab-gitops

Un cluster Kubernetes de un solo nodo, corriendo en una Dell Latitude 7490 que
estaba juntando polvo con la batería fundida, gobernado 100% por Git.

No hay nada acá que se toque a mano en el cluster. Si algo cambia, cambia en
este repo, se hace commit, y ArgoCD lo aplica solo. Ese es el punto de todo
el ejercicio.

## Por qué existe esto

La 7490 tenía un problema simple: no arrancaba sin estar enchufada, y estaba
sin usar. Un server que vive enchufado 24/7 y no necesita batería no es un
defecto, es el caso de uso perfecto. La pregunta era qué tan lejos se podía
llevar esa máquina vieja como plataforma real de aprendizaje — no un tutorial
de juguete, sino la misma disciplina que usa un equipo de infra en producción,
comprimida en un solo nodo.

La decisión de fondo, que se repite en cada parte de este repo: **los
cerebros van por API, el fierro local se dedica a orquestar.** Un i5 de
8va gen con 16GB de RAM no compite con GPUs corriendo modelos grandes, pero
sobra y sobra para correr Kubernetes, ArgoCD, y agentes que llaman a Claude
u OpenAI por HTTP. Pelear esa batalla al revés (LLM pesado local, orquestación
mínima) hubiera sido jugar en contra del hardware que hay.

## Qué hay corriendo acá

- **k3s**: Kubernetes de un solo nodo, liviano, con Traefik y SQLite
  incluidos. Nada de etcd ni HA — no hace falta para un nodo.
- **ArgoCD**: el corazón operativo. Vigila este repo y aplica cualquier
  cambio al cluster automáticamente, con self-heal activado — si alguien
  edita algo a mano en el cluster, ArgoCD lo revierte al estado que dice
  Git.
- **`agents/morning-digest`**: el primer agente real. Un CronJob que todos
  los días lee un puñado de feeds RSS (tech, producto, negocios) más los
  newsletters etiquetados en Gmail, arma un resumen con OpenAI, y lo manda
  por Telegram. Corre, resume, se apaga. Nada queda vivo consumiendo RAM
  entre corrida y corrida.

## Cómo se armó, en orden real

**1. El sistema operativo.** Debian 13, instalación mínima, sin entorno
gráfico. Se evaluaron Ubuntu Server (de más, con snapd y capas que no
aportan nada acá), Fedora/openSUSE (ciclos de release demasiado cortos
para un server que se quiere dejar tranquilo) y Arch (rolling release en
una máquina desatendida es jugarse a que un update rompa algo mientras
dormís). Debian gana por aburrido, que es exactamente lo que se necesita.

**2. Acceso remoto, hecho bien.** SSH por clave únicamente. Se generó un
par de claves ed25519 en el desktop, se copió la pública al server, y
recién después de confirmar que el login sin password funcionaba se
deshabilitó `PasswordAuthentication` — con dos terminales abiertas en
paralelo por las dudas, porque quedarse afuera de tu propio server por
un typo en `sshd_config` es un clásico. También apareció el caso menos
obvio: `UsePAM yes` puede dejar un bypass de password vía
`KbdInteractiveAuthentication` aunque `PasswordAuthentication` esté en
`no`. Se verificó explícitamente con `ssh -o PubkeyAuthentication=no`
para confirmar que de verdad rechazaba sin clave.

**3. Red, con IP que no se mueve.** La 7490 arrancó por WiFi (funcional,
pero no lo que se quiere para un server 24/7), y después se le conectó
un cable Ethernet. La interfaz `enp0s31f6` no traía `dhclient`
preinstalado en Debian 13 — se resolvió con `isc-dhcp-client` — y se dejó
la configuración persistente en `/etc/network/interfaces` para que
levante sola en cada boot. La IP quedó reservada por MAC en el router
(`Pre-assigned DHCP IP Addresses`), así la dirección nunca cambia aunque
el DHCP reinicie.

**4. k3s y ArgoCD, en ese orden, desde el primer día.** La tentación
natural es instalar Kubernetes y empezar a tirar `kubectl apply` a mano
mientras "se prueban cosas". Se evitó eso a propósito: ArgoCD se instaló
antes del primer Deployment real, para que el hábito de "todo pasa por
Git" quedara fijado desde el arranque y no como una migración incómoda
después. El primer test fue un nginx dummy — no porque nginx importe,
sino para confirmar el ciclo completo: commit → push → ArgoCD sincroniza
→ `kubectl delete pod` a mano → el pod vuelve solo.

**5. El primer agente real.** Reemplazar el nginx de prueba por algo que
efectivamente hace algo útil: leer feeds y newsletters, resumir con un
LLM, mandar el resultado a Telegram. Decisiones tomadas en el camino:

- **Secrets fuera de Git.** Se evaluó SOPS+KSOPS para manejar secrets
  encriptados dentro del repo, pero para un solo CronJob con cinco
  variables es sobreingeniería. Se optó por crear el `Secret` de
  Kubernetes directo con `kubectl`, mientras el CronJob (que sí vive en
  Git) lo referencia por nombre. GitOps parcial, pragmático. Cuando haya
  tres o cuatro agentes con secrets distintos, ahí se justifica meter
  SOPS de una.
- **Build manual, no CI todavía.** La imagen se buildea a mano en la
  7490 y se pushea a GitHub Container Registry. Suficiente para un
  agente. Cuando el ciclo de iterar-rebuildear-pushear empiece a cansar,
  se migra a GitHub Actions.

## Los baches, porque son la parte que vale la pena releer

Nada de esto salió andando a la primera, y está bien que así sea:

- **`kubectl apply` falló en el CRD de ArgoCD** por exceder el límite de
  256KB en la annotation `last-applied-configuration`. Se resolvió con
  `--server-side --force-conflicts`, que no depende de esa annotation.
- **`k3s kubectl` no respetaba `~/.kube/config`** por default — a
  diferencia de `kubectl` normal, iba directo a
  `/etc/rancher/k3s/k3s.yaml` (con permisos solo para root) salvo que se
  exportara `KUBECONFIG` explícitamente.
- **`openai==1.54.0` rompía con un `TypeError` sobre `proxies`** al
  instanciar el cliente, porque no se había fijado la versión de
  `httpx` y `pip` instaló la última, que ya había sacado ese parámetro
  interno. Se resolvió fijando `httpx==0.27.2` en `requirements.txt`.
  Recordatorio permanente: pinnear versiones, siempre.
- **El primer test corrió sin errores (`Completed`, exit 0) pero no
  llegó ningún mensaje a Telegram.** El script original ignoraba
  silenciosamente cualquier error de la API de Telegram. Se agregó
  logging explícito y `resp.raise_for_status()`, lo que reveló el
  problema real: el token guardado en el Secret tenía el prefijo `bot`
  duplicado (`botbot123:...`) porque así lo entrega BotFather en el
  mensaje de confirmación, y es fácil copiarlo tal cual sin darse
  cuenta. Fix aplicado en dos capas: se corrigió el dato, y además el
  código ahora tolera el prefijo con `.removeprefix("bot")` para que el
  mismo error humano no vuelva a romper nada.

Ninguno de estos errores fue exótico. Son los errores normales de armar
infraestructura real: límites de API mal documentados, defaults de
herramientas que cambian entre versiones, y un copy-paste de token con
un prefijo de más. La diferencia entre que esto ande o no es tener
logging que efectivamente diga qué pasó, en vez de asumir que "no tiró
error" significa "funcionó".

## Estructura del repo

homelab-gitops/
├── apps/ # reservado para el patrón app-of-apps a futuro
├── agents/
│ └── morning-digest/
│ ├── src/
│ │ ├── agent.py
│ │ └── requirements.txt
│ ├── Dockerfile
│ ├── feeds.yaml
│ ├── cronjob.yaml
│ └── kustomization.yaml
└── infra/ # reservado para observabilidad (Phoenix/VictoriaMetrics) a futuro


## Qué sigue

- Timezone del sistema fijado a `America/Argentina/Buenos_Aires` para que
  el CronJob dispare a la hora real esperada, no en UTC.
- Observabilidad de agente (Arize Phoenix, liviano) para ver trazas de
  cada corrida en vez de leer logs de Kubernetes a mano.
- Más agentes bajo `agents/`, cada uno con su propia carpeta,
  Dockerfile, y CronJob — el patrón ya está probado y se repite.
