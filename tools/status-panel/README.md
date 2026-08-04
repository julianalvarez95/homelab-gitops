# status-panel

Panel de estado ambient para el monitor CRT (Samsung SyncMaster 793s)
conectado por VGA→HDMI al puerto `HDMI-A-1` de la iGPU Intel del Dell
Latitude 7490. Corre en texto plano directo en la consola virtual
`tty1`, sin X ni ningún componente gráfico — es una sesión de `tmux`
con 3 paneles que arranca sola cuando el usuario `usainbot` hace login
(automático) en `tty1`.

Layout `main-vertical` de tmux, panel de pods al 65% del ancho:

```
+-------------------------------+---------------------------+
|                               | argocd sync status (30s)  |
|      kubectl get pods -A     +---------------------------+
|            --watch            |   morning-digest logs    |
|                               |                            |
+-------------------------------+---------------------------+
```

## Archivos de este directorio

| Archivo | Qué hace |
|---|---|
| `launch.sh` | Crea (o adjunta a) la sesión tmux `status` con los 3 paneles. Idempotente: si la sesión ya existe, solo se adjunta. |
| `status-panel.tmux.conf` | Config de tmux *scoped* a esta sesión (`tmux -f ...`). No toca `~/.tmux.conf`. Barra de status apagada, sin mouse, panel de pods con 65% del ancho. |
| `argocd-status.sh` | Imprime sync/health de las `Application` de ArgoCD leyendo el CRD vía `kubectl` (namespace `argocd`), con el hash de revisión truncado a 7 caracteres para que entre en un panel angosto. |
| `follow-morning-digest.sh` | Ubica el pod más reciente del CronJob `morning-digest` (namespace `default`) por prefijo de nombre + timestamp de creación, y sigue sus logs con `kubectl logs -f`. Cuando el pod termina, espera 15s y vuelve a chequear. |

## Por qué `kubectl get application` y no el CLI de `argocd`

El `argocd-server` de este cluster es `ClusterIP` puro (sin NodePort ni
Ingress). El `~/.config/argocd/config` local apuntaba a
`192.168.0.214:8080`, resabio de un port-forward manual que no está
corriendo permanentemente. Mantener ese port-forward vivo solo para
este panel es un punto de falla extra y un token que expira. Leer el
CRD `Application` directamente con el mismo kubeconfig que ya usa el
panel de pods es más simple y no depende de nada externo al cluster.

## Por qué cada panel está envuelto en un loop de reinicio

El kernel detectó un HPD interrupt storm en `HDMI-A-1`
(`dmesg`/`journalctl -k`: *"HPD interrupt storm detected on connector
HDMI-A-1: switching from hotplug detection to polling"*), es decir, la
señal del adaptador VGA→HDMI genera hotplug events espurios. Para que
un blip de señal no deje un panel colgado, los tres arrancan como:

```bash
while true; do <comando>; sleep 2; done
```

`follow-morning-digest.sh` y `argocd-status.sh` además tienen su propio
loop interno (para no golpear la API de Kubernetes cada 2 segundos sin
necesidad); el `while true` externo de `launch.sh` solo reinicia el
script completo si ese proceso muere.

### Probado, no solo diseñado

Verificado a mano matando procesos dentro de la sesión real en tty1:

- Matar el `kubectl` (o el script) que corre *dentro* del `while true`:
  el loop-shell del panel (mismo PID) lo relanza en el siguiente ciclo.
  Esto es el caso normal — un blip de HDMI o un `kubectl` que se cuelga
  no deja el panel muerto.
- Matar el **loop-shell entero** (el `bash -c 'while true; ...'` en sí,
  no el comando de adentro): acá sí, tmux destruye ese panel
  (`remain-on-exit` es `off` por default) y la sesión queda con menos
  de 3 paneles. `launch.sh` originalmente solo chequeaba `has-session`,
  así que un login posterior se hubiera re-adjuntado a la sesión
  degradada sin repararla — se reprodujo este bug y se corrigió:
  `launch.sh` ahora cuenta los paneles del window `dashboard` y, si no
  son exactamente 3, mata la sesión entera y la reconstruye desde cero
  antes de adjuntarse.
- Caso límite verificado: matar la sesión de tmux completa mientras
  estaba attacheada desde tty1. El cliente `tmux attach-session`
  (que reemplazó al shell de login vía `exec`) termina, lo que hace
  que `getty@tty1.service` se reinicie solo (`Restart=always` del unit
  original), dispare el autologin de nuevo, y `launch.sh` reconstruya
  la sesión sana. Se probó de punta a punta sin reboot ni intervención
  manual más allá de matar el proceso.

## Qué se instaló en el host (fuera de este repo)

Esto es lo que toca estado del sistema por fuera de los archivos de
este directorio — documentado acá porque no queda registrado en ningún
otro lado.

### 1. Paquete `tmux`

```bash
sudo apt-get install -y tmux
```

### 2. Autologin en tty1 (systemd drop-in)

Archivo `/etc/systemd/system/getty@tty1.service.d/autologin.conf`:

```ini
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin usainbot --noreset --noclear - ${TERM}
```

Mismos flags (`--noreset --noclear - ${TERM}`) que el
`ExecStart` original de `getty@.service` en este Debian 13, solo se
agrega `--autologin usainbot` y se saca el `-o '-- \u'` (redundante con
autologin). Solo afecta a `tty1` — el resto de las virtual consoles
(`tty2`-`tty63`) y las sesiones SSH (`pts/N`) no se tocan.

### 3. Autostart de tmux, solo en tty1 (`~/.profile`)

Bloque agregado al final de `/home/usainbot/.profile`, entre
marcadores `# BEGIN status-panel` / `# END status-panel`:

```bash
# BEGIN status-panel (see homelab-gitops/tools/status-panel/README.md)
if [ "$(tty)" = "/dev/tty1" ] && [ -z "$TMUX" ]; then
    exec /home/usainbot/homelab-gitops/tools/status-panel/launch.sh
fi
# END status-panel
```

Se agregó a `~/.profile` y no a `~/.bash_profile` porque este último
no existía: bash solo lee uno de los dos, y de haber creado
`~/.bash_profile` desde cero, `~/.profile` (que sourcea `~/.bashrc`,
donde se exporta `KUBECONFIG`) hubiera dejado de leerse, rompiendo el
entorno de los paneles.

El `[ -z "$TMUX" ]` evita tmux anidado si alguna vez se entra a `tty1`
ya con una sesión tmux activa. El `exec` reemplaza el shell de login
por `launch.sh`, así que cuando la sesión tmux termina (o el proceso
de login muere), `getty@tty1.service` (que tiene `Restart=always` en
el unit original) vuelve a lanzar un login limpio y el ciclo se repite.

## Cómo probarlo sin reboot

```bash
sudo systemctl restart getty@tty1.service
```

Esto reinicia solo la consola de tty1 (corta y recrea esa sesión de
login), sin afectar SSH ni el resto del sistema. Es lo que se usó para
validar todo lo de arriba.

### Pendiente: no se probó contra un boot real

Todo lo anterior se validó con el sistema ya arriba (k3s, API server y
`~/.kube/config` disponibles). `getty@tty1.service` no tiene ninguna
dependencia de arranque sobre k3s (`After=systemd-user-sessions.service
plymouth-quit-wait.service getty-pre.target`), así que en un boot real
el panel va a arrancar e intentar hablar con `kubectl` antes de que la
API de Kubernetes esté escuchando. El diseño (loops de `while true;
sleep 2`) debería absorber eso sin problema — `kubectl` falla rápido
contra un server caído, no se cuelga — pero esto **no se verificó con
un reboot real**, solo con `systemctl restart getty@tty1`. Si querés
una validación completa, el próximo paso es un reboot con alguien
mirando la pantalla física para confirmar que los paneles se
autorreparan una vez que k3s termina de levantar.

## Cómo revertir todo

En orden inverso:

```bash
# 1. Sacar el autostart de tmux
#    Borrar el bloque entre "# BEGIN status-panel" y "# END status-panel"
#    en ~/.profile (a mano, o con sed):
sed -i '/# BEGIN status-panel/,/# END status-panel/d' ~/.profile

# 2. Sacar el autologin
sudo rm -r /etc/systemd/system/getty@tty1.service.d
sudo systemctl daemon-reload
sudo systemctl restart getty@tty1.service

# 3. (Opcional) matar la sesión de tmux si sigue corriendo
tmux kill-session -t status

# 4. (Opcional) desinstalar tmux, si no se usa para nada más
sudo apt-get remove -y tmux
```

Los archivos de este directorio (`tools/status-panel/`) no necesitan
tocarse para revertir — sin el autostart en `~/.profile` simplemente no
se ejecutan nunca.
