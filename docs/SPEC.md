# PROMPT — Recolecta

> Prompt de ingeniería listo para pegar en Claude Code (o en un chat largo).
> Construye desde cero un descargador programado de archivos para servidores FTP/FTPS/SFTP/WebDAV/WebDAVS/SMB,
> desplegable en Windows 10/11 x64 **sin internet**, con el mismo patrón de empaquetado y autoarranque
> que `castellanosfelipe/StabilityMonitor`.

---

## 0. Cómo usar este prompt

Pégalo completo como primer mensaje del proyecto. Si trabajas con Claude Code, guárdalo como `docs/SPEC.md`
en el repo vacío y arranca con: *"Lee `docs/SPEC.md` y ejecuta la Fase 0. No avances de fase sin que los
tests de la fase anterior pasen."*

Reglas de trabajo para el agente:

- No inventes dependencias fuera de las listadas en §11.2. Cada dependencia nueva debe justificarse y agregarse al `wheelhouse/`.
- No uses CDNs, fuentes remotas, telemetría, ni chequeos de actualización. La máquina destino **no tiene internet**.
- Escribe los mensajes de UI y de error en español; el código, los identificadores y los docstrings en inglés.
- Registra toda decisión no trivial en `docs/DECISIONS.md` con formato `D-0NN · Título`.
- Sin co-autoría de IA en los commits (`Co-authored-by` prohibido). Enfórzalo con `CLAUDE.md` + hook `commit-msg`.

---

## 1. Rol y contexto

Actúa como **ingeniero de software senior especializado en aplicaciones Windows offline, transferencia de
archivos y sistemas de ejecución desatendida**. Vas a construir `Recolecta`, una aplicación local que
descarga cada madrugada los archivos del último día desde uno o varios servidores de archivos corporativos,
los deja ordenados en una carpeta local, y deja evidencia auditable de qué se descargó, cuándo y con qué resultado.

El proyecto es el hermano operativo de un proyecto previo del mismo autor, **StabilityMonitor**, que
monitorea la *disponibilidad* de esas mismas conexiones. Recolecta reutiliza su patrón de despliegue,
su modelo de configuración y su formato de exportación, pero cambia el propósito: de *observar* a *traer*.

---

## 2. Objetivo del producto

Una sola frase: **que a las 2 de la mañana los archivos del día anterior aparezcan solos en `D:\Descargas\<cliente>\...`,
y que a las 8 de la mañana exista un log descargable que pruebe exactamente qué pasó.**

Escenarios que debe resolver:

| Escenario | Comportamiento esperado |
|---|---|
| Corrida nocturna normal | Descarga los archivos de la ventana configurada, sin duplicar los ya traídos. |
| El equipo estaba apagado a la hora programada | Al encender, detecta la corrida perdida y la ejecuta (catch-up con ventana de gracia). |
| Reinicio de Windows | El servicio arranca solo, cierra como interrumpida la corrida anterior y una nueva ejecución redescubre el trabajo pendiente y puede reanudar sus `.part`. |
| Caída de red a mitad de descarga | Reintenta con backoff y **reanuda** el archivo parcial, no lo reinicia desde cero. |
| Un archivo aún se está escribiendo en el servidor | Lo omite en esta corrida y lo toma en la siguiente (quiet period). |
| El operador quiere saber qué pasó | Dashboard local + export CSV/JSONL descargable por corrida y global. |
| Corrida en curso | Barra de progreso por archivo (bytes, velocidad, ETA) y progreso global. |

---

## 3. Restricciones no negociables

1. **Offline total.** La máquina destino no tiene internet ni Python instalado. Todo el runtime viaja en el paquete.
2. **Windows 10/11 Pro x64** como plataforma objetivo. Desarrollo y CI pueden ser no-Windows (modo `dev`).
3. **Sin instalación con administrador en el modo por defecto.** No se toca `HKLM` ni `Program Files`.
4. **Portable:** todo el estado (`data/`, `logs/`, `downloads/` si es relativo) vive junto al ejecutable.
5. **Nunca borrar archivos remotos por defecto.** Ninguna operación destructiva sin confirmación explícita.
6. **Los secretos nunca salen del servidor local ni aparecen en logs, exports o respuestas de la API.**
7. **Bajo impacto:** la descarga nocturna no puede saturar el servidor de origen ni el enlace WAN.

---

## 4. Patrón de despliegue heredado (obligatorio)

Replica exactamente el patrón "Modo A" de StabilityMonitor, ya validado en producción:

### 4.1 Empaquetado

- **PyInstaller `--onedir --noconsole --noconfirm --clean`** sobre un `launcher.py` de nivel raíz
  (grafo de imports estático), con `multiprocessing.freeze_support()` y un flag `--self-test` que
  importa explícitamente los módulos que PyInstaller suele omitir y sale con código 0/1.
- `--add-data "static;static"` y `--add-data "templates;templates"`.
- `--hidden-import` para todo import perezoso (`win32crypt`, `winotify`, `winsound`, `pystray`, `pystray._win32`,
  `PIL.Image`, `PIL.ImageDraw`, cadena de `cryptography.hazmat.*`) y `--collect-submodules apscheduler`, `cryptography`, `tzdata`.
- **Build 100% offline:** `wheelhouse/` con wheels `cp312 / win_amd64` versionados en el repo;
  `pip install --no-index --find-links .\wheelhouse`. `vendor/` con el instalador oficial de Python 3.12 para bootstrap.
- `build.ps1` en PowerShell: crea `.venv-build` (`py -3.12` con fallback a `python`), instala desde wheelhouse,
  **corre `pytest` y aborta si falla**, ejecuta PyInstaller, corre el `--self-test` sobre el `.exe` congelado,
  y copia `install*.ps1` / `uninstall.ps1` dentro de `dist\Recolecta\`.
- CI en GitHub Actions sobre `windows-latest`, disparado por tags `v*.*.*` y `workflow_dispatch`:
  compila, empaqueta ZIP, verifica contenido, sube artifact y publica Release.

### 4.2 Resolución de rutas

```python
def base_dir() -> Path:
    env = os.environ.get("RECOLECTA_DATA_DIR", "").strip()
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):        # bundle PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
```

De ahí cuelgan `data/` (SQLite + `known_hosts`), `logs/`, `exports/` y, si la ruta de descarga es relativa, `downloads/`.

### 4.3 Autoarranque

Dos modos, ambos entregables. **Este es el punto donde Recolecta debe superar a StabilityMonitor**,
porque una descarga a las 02:00 no puede depender de que alguien tenga sesión abierta.

**Modo A — usuario, sin administrador** (`install.ps1`, por defecto):

```powershell
$action  = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName "Recolecta" -Action $action -Trigger $trigger -Settings $settings -Force
```

Tras registrar: `Start-ScheduledTask`, luego polling de `http://127.0.0.1:8091/healthz` hasta 20 s con mensaje
verde/amarillo. Bandeja del sistema y toasts disponibles.

**Modo B — desatendido, sobrevive al logoff** (`install-service.ps1`, requiere administrador):

```powershell
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet ... -WakeToRun -StartWhenAvailable
```

Implicaciones que **debes** manejar en el código y documentar en `docs/USER_GUIDE.md`:

- Bajo `SYSTEM` no hay sesión interactiva → **sin bandeja ni toasts** (aislamiento de sesión 0).
  Detéctalo en `runtime_mode()` y degrada a modo headless: alertas a `logs/`, Windows Event Log (`pywin32`), SMTP o webhook.
  El dashboard en `127.0.0.1:8091` sigue siendo accesible desde el navegador del usuario interactivo.
- **DPAPI con ámbito de usuario no sirve entre cuentas.** Si los secretos se cifraron como usuario interactivo
  y el servicio corre como `SYSTEM`, el descifrado falla. Usa `CRYPTPROTECT_LOCAL_MACHINE` + *entropy* adicional
  guardada en `data\.entropy` con ACL restringida a `SYSTEM` y `Administrators`. Prefija el token como
  `dpapi-machine:` para distinguirlo de `dpapi:` y `fernet:` (ver §10.2).
- `SYSTEM` no ve unidades de red mapeadas ni credenciales interactivas para UNC. Para SMB registra una sesión
  SMB2/SMB3 explícita mediante `smbprotocol` con la credencial cifrada de la conexión, o usa una cuenta de servicio de dominio.

`uninstall.ps1` detiene y desregistra la tarea, mata el proceso y **no borra datos ni descargas**.

---

## 5. Arquitectura objetivo

Proceso único que sirve dashboard, agenda corridas, descarga y persiste.

```
UI local (127.0.0.1:8091, HTML/CSS/JS vanilla + Chart.js local)
        │
FastAPI + Uvicorn ── /healthz sin auth · Basic Auth opcional por env
        │
Scheduler (APScheduler BackgroundScheduler, tz configurable)
   ├─ CronTrigger diario (hora configurable)  → RunOrchestrator
   ├─ Catch-up al arranque (corridas perdidas)
   └─ Housekeeping (purga de historial, verificación de espacio)
        │
RunOrchestrator ── por conexión: Lister incremental → Planner por lotes
        │              │                                │
        │              │                                └─ ventana o comparación local, filtros, dedupe
        │              └─ FTP/FTPS · SFTP · WebDAV(S) · SMB/UNC
        │
Cola persistente SQLite (`run_files`) → reclamo acotado → DownloadPool
        │                                      │
        │                                      └─ N workers, progreso por bloque
        └─ fases, totales y destinos reservados sin cargar el listado completo en RAM
        │
Throttle (lock por host · spacing · rate limit · concurrencia global · bandwidth cap)
        │
SQLite WAL (data/recolecta.db) ─ connections · runs · run_files · settings · alerts_log
        │
logs/app.log (rotativo) + logs/runs/*.jsonl (estructurado, descargable)
```

**Puerto 8091** por defecto, no 8090: StabilityMonitor puede convivir en la misma máquina.

Módulos (`app/`): `main.py`, `config.py`, `models.py`, `db.py`, `errors.py`, `logging_setup.py`,
`scheduler.py`, `orchestrator.py`, `progress.py`, `throttle.py`, `naming.py`, `integrity.py`,
`settings_store.py`, `alerts.py`, `api/{routes,schemas}.py`,
`transports/{base,ftp,sftp,webdav,smb}.py`, `platform/{detect,secretstore,secrets_dpapi,secrets_fernet,notify_windows,tray_windows,eventlog}.py`.

---

## 6. Modelo de datos (SQLite WAL, migraciones secuenciales)

```sql
CREATE TABLE connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, client TEXT NOT NULL DEFAULT '',
    protocol TEXT NOT NULL,                 -- FTP|FTPS|SFTP|WEBDAV|WEBDAVS|SMB
    host TEXT NOT NULL, port INTEGER NOT NULL,
    username TEXT NOT NULL DEFAULT '', secret_encrypted TEXT,
    auth_type TEXT NOT NULL DEFAULT 'password',   -- password|key
    key_path TEXT, ssl_mode TEXT NOT NULL DEFAULT 'required',
    remote_paths_json TEXT NOT NULL DEFAULT '[]', -- carpetas origen
    recursive INTEGER NOT NULL DEFAULT 0, max_depth INTEGER NOT NULL DEFAULT 3,
    include_globs_json TEXT NOT NULL DEFAULT '[]',
    exclude_globs_json TEXT NOT NULL DEFAULT '[]',
    min_size_bytes INTEGER, max_size_bytes INTEGER,
    window_mode TEXT NOT NULL DEFAULT 'calendar_day',  -- calendar_day|rolling_hours|since_last_run
    window_hours INTEGER NOT NULL DEFAULT 24,
    window_overlap_min INTEGER NOT NULL DEFAULT 15,
    quiet_period_s INTEGER NOT NULL DEFAULT 120,
    full_local_reconciliation INTEGER NOT NULL DEFAULT 0,
    timezone TEXT NOT NULL DEFAULT 'America/Bogota',
    schedule_time TEXT,                         -- HH:MM; NULL hereda agenda global
    dest_root TEXT NOT NULL,
    dest_template TEXT NOT NULL DEFAULT '{remote_tree}',
    on_conflict TEXT NOT NULL DEFAULT 'skip',     -- skip|overwrite|keep_both
    verify_mode TEXT NOT NULL DEFAULT 'size',     -- size|sha256
    max_parallel_files INTEGER NOT NULL DEFAULT 2,
    bandwidth_limit_kbps INTEGER,
    timeout_s REAL NOT NULL DEFAULT 30, retries INTEGER NOT NULL DEFAULT 3,
    post_action TEXT NOT NULL DEFAULT 'none',     -- solo none; otros valores reservados
    post_action_path TEXT,
    enabled INTEGER NOT NULL DEFAULT 1, notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL,                  -- schedule|catchup|manual|cli
    window_start_utc TEXT NOT NULL, window_end_utc TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL,                   -- running|ok|partial|failed|cancelled
    scan_mode TEXT NOT NULL DEFAULT 'window', -- window|full_local_reconciliation
    phase TEXT NOT NULL DEFAULT 'discovering', -- discovering|downloading|finished
    files_found INTEGER DEFAULT 0, files_downloaded INTEGER DEFAULT 0,
    files_planned INTEGER DEFAULT 0, planned_bytes INTEGER DEFAULT 0,
    files_skipped INTEGER DEFAULT 0, files_failed INTEGER DEFAULT 0,
    bytes_downloaded INTEGER DEFAULT 0,
    error_type TEXT, error_msg TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_runs_conn_started ON runs(connection_id, started_at);

CREATE TABLE run_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    connection_id INTEGER NOT NULL,
    remote_path TEXT NOT NULL, local_path TEXT,
    identity_key TEXT NOT NULL, plan_status TEXT, reason TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER, bytes_done INTEGER NOT NULL DEFAULT 0,
    mtime_utc TEXT,
    timestamp_reliable INTEGER NOT NULL DEFAULT 0,
    timestamp_source TEXT NOT NULL DEFAULT '',
    sha256 TEXT,
    status TEXT NOT NULL,   -- pending|downloading|ok|skipped|duplicate|failed
    attempts INTEGER NOT NULL DEFAULT 0,
    error_type TEXT, error_msg TEXT NOT NULL DEFAULT '',
    started_at TEXT, finished_at TEXT, duration_s REAL
);
CREATE INDEX idx_run_files_run ON run_files(run_id, status);
CREATE INDEX idx_file_identity_lookup
    ON run_files(connection_id, remote_path, mtime_utc, size_bytes, status);
CREATE UNIQUE INDEX idx_run_file_identity
    ON run_files(run_id, identity_key) WHERE identity_key <> '';
CREATE INDEX idx_run_files_queue ON run_files(run_id, status, id);

CREATE TABLE destination_reservations (
    connection_id INTEGER NOT NULL,
    mapping_scope TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    local_path TEXT NOT NULL,
    local_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (connection_id, mapping_scope, remote_path)
);
```

`settings` y `alerts_log` como en el proyecto previo.

### 6.1 Estado canónico y resultado descriptivo

`runs.status` conserva exclusivamente los valores canónicos
`running|ok|partial|failed|cancelled`. Este contrato alimenta persistencia,
agenda, catch-up, métricas y compatibilidad de API; una corrida que lista
correctamente y no encuentra archivos sigue siendo `ok`.

La API, el dashboard y los reportes añaden un resultado derivado, sin
reemplazar el estado canónico:

| Condición canónica | `result_status` | Etiqueta visible |
|---|---|---|
| `status='ok'` y `files_found=0` | `no_files` | **Archivos no existentes** |
| `status='ok'`, `files_found>0` y `files_downloaded=0` | `no_changes` | **Sin archivos nuevos** |
| `status='ok'` y `files_downloaded>0` | `completed` | **Descarga completada** |
| `status='running'` | `running` | **En ejecución** |
| `status='partial'` | `partial` | **Completada con incidencias** |
| `status='cancelled'` | `cancelled` | **Cancelada por el usuario** |
| `status='failed'` | `failed` | Causa específica según `error_type` |

La evaluación del estado canónico tiene precedencia sobre los contadores. Por
lo tanto, una autenticación rechazada, una ruta remota inexistente o cualquier
otro fallo con `files_found=0` nunca puede presentarse como
**Archivos no existentes**. Los exports conservan `status` como dato estable y
pueden añadir `result_status` y `status_label` para consumo humano.

---

## 7. Requisitos funcionales

### RF-1 · Gestión de conexiones

- CRUD completo desde el dashboard, con **"Probar conexión y rutas"** en el
  editor y **"Simular corrida" (dry-run)** para una conexión guardada: calcula
  los totales y muestra una muestra acotada del plan sin descargar un solo byte.
- Cada tarjeta y el editor exponen **Comparación completa con carpeta local**.
  El cambio se persiste por conexión y la interfaz confirma o revierte el
  checkbox según la respuesta del backend.
- El editor mantiene **Guardar conexión** bloqueado hasta validar el borrador
  actual: credencial, todas las rutas remotas y capacidad de escritura y
  renombrado en el destino local. Cualquier cambio posterior exige repetir la
  prueba; el backend aplica la misma regla antes de persistir cambios de
  conectividad o rutas. Si se configuró una acción de movimiento remoto,
  también comprueba que su carpeta de destino sea accesible.
- La validación remota llama cada raíz de forma independiente y consume como
  máximo 101 metadatos: conserva una muestra de 100 y usa el elemento adicional
  exclusivamente para detectar truncamiento. Debe cerrar el iterador antes de
  cerrar el transporte, también ante errores, y nunca descargar contenido.
  `remote_files_found` representa el tamaño de la muestra de las raíces de
  origen y no incluye una carpeta usada únicamente como destino de movimiento;
  `remote_files_found_is_exact=false` y una advertencia por raíz indican que no
  es un inventario total. Las raíces válidas y vacías se aceptan.
- Puertos por defecto: FTP/FTPS 21, SFTP 22, WEBDAV 80, WEBDAVS 443, SMB 445.
- **Importación del backup de StabilityMonitor** (`monitor-backup.json`), formato exacto en §16.1:
  - Acepta solo los protocolos de archivos: `FTP`, `FTPS`, `SFTP`, `WEBDAV`, `WEBDAVS`, `SMB`.
  - Ignora `POSTGRES`, `MYSQL`, `MARIADB`, `SQLSERVER`, `ORACLE` reportando
    `"N conexiones de base de datos omitidas: no aplican a descarga de archivos"`.
  - Mapea `targets_json` → `remote_paths_json` (los objetivos monitoreados son justamente las carpetas a descargar).
  - Todas las conexiones importadas nacen **en pausa** hasta validar sus rutas.
    Si el archivo trae `secret` en claro, lo cifra localmente; si no, conserva
    el estado `"falta credencial"`.
  - Reutiliza la clave de deduplicación `(protocol, host, port, name)`.
- Exportación propia en el mismo formato (`"app": "Recolecta"`), **sin secretos**.

### RF-2 · Descubrimiento y ventana temporal

Esta es la regla de negocio más delicada del sistema. Impleméntala con tests exhaustivos.

- Tres modos de ventana:
  - `calendar_day`: día calendario anterior completo `[ayer 00:00:00, hoy 00:00:00)` en la zona horaria de la conexión.
  - `rolling_hours`: últimas N horas contadas desde el inicio de la corrida.
  - `since_last_run`: desde el `window_end_utc` de la última corrida `ok`, menos `window_overlap_min` de solape.
    **Este es el modo recomendado por defecto para producción**; `calendar_day` es el que pide la lectura literal
    del requisito y debe seguir siendo el default de la UI.
- **Normaliza todos los timestamps a UTC antes de comparar.** Fuentes por protocolo:
  - FTP/FTPS: en listados usa `MLSD` (`modify=` en UTC, RFC 3659) de forma incremental y reserva `MDTM`
    para `stat` individual; así evita una orden adicional por cada archivo. **No confíes en el `LIST`**:
    da hora local del servidor, sin zona, con precisión de minutos, y para archivos de más de 6 meses omite
    la hora y da el año. Si `MLSD` no está soportado, cae a `LIST` y **marca la corrida como `partial` con
    advertencia explícita de precisión temporal**.
  - SFTP: `st_mtime` de `sftp.listdir_attr()` (epoch UTC).
  - WebDAV: `PROPFIND` con `Depth: 1`, propiedad `getlastmodified` (RFC 1123, GMT).
  - SMB/UNC: `smbclient.stat().st_mtime` mediante SMB2/SMB3 (epoch → UTC); `Path.stat()` queda limitado a rutas locales de desarrollo y pruebas.
- **Quiet period:** omite archivos cuyo `mtime` sea más reciente que `quiet_period_s` (default 120 s) — probablemente
  aún se están escribiendo. Opcionalmente valida estabilidad de tamaño entre dos listados separados 5 s.
- Filtros: globs de inclusión/exclusión (`*.csv`, `!tmp_*`), tamaño mínimo/máximo, recursión con `max_depth`,
  exclusión de directorios, sin seguir symlinks.
- **Deduplicación:** un archivo con la misma `(remote_path, mtime_utc, size)` ya descargado con éxito se marca
  `duplicate` y no se vuelve a bajar, sin importar cuántas veces se dispare la corrida.
- **Comparación completa local opcional:** cuando
  `full_local_reconciliation=true`, el listado recorre de forma recursiva todo
  el árbol de cada raíz remota e ignora tanto la ventana temporal como el
  historial exitoso. Conserva globs, límites de tamaño, quiet period y la regla
  de no seguir symlinks. Un archivo local es equivalente únicamente si es un
  archivo regular, coincide su tamaño cuando el remoto lo anuncia y su `mtime`
  coincide dentro de una tolerancia de dos segundos cuando está disponible.
  Los ausentes y diferentes se encolan para descarga; los equivalentes se
  registran como presentes. Es una reconciliación unidireccional
  **remoto → local**: nunca elimina, mueve ni modifica archivos locales extra.
  Este modo rechaza plantillas con cualquier variante de `{run_id}`, porque
  una ruta distinta por corrida impediría comparar de forma estable contra el
  destino local.

### RF-3 · Motor de descarga

- **Atomicidad obligatoria:** descarga a
  `<dest>\.staging\<2-hex>\<uuid>.part` y al terminar usa `os.replace()` hacia
  el destino final. El UUIDv5 sigue derivándose de la misma identidad remota;
  el shard evita concentrar millones de entradas en un único directorio.
  Después del pre-flight, un parcial legado
  `<dest>\.staging\<uuid>.part` se migra atómicamente al shard. Si la
  migración falla se reutiliza el legado, y si existen ambos se prefiere el
  shard. Nunca puede quedar un archivo incompleto con nombre definitivo.
- **Limpieza conservadora de staging:** al arrancar recorre, sin seguir
  symlinks, `.staging` una vez por `dest_root` único. Elimina parciales vacíos
  de inmediato y parciales con datos solo si son anteriores a
  `max(7 días, catchup.max_days + 1 día)`. Conserva archivos activos,
  recientes y ajenos a `.part`, elimina shards vacíos y emite únicamente
  contadores agregados de archivos, bytes y errores. Una raíz inaccesible
  produce una advertencia y no bloquea el arranque.
- **Contenido opaco:** la transferencia, el staging, la reanudación, el hash y
  la publicación operan con bytes. Recolecta no decodifica, recodifica,
  normaliza saltos de línea ni transforma documentos; el archivo publicado
  conserva exactamente la secuencia de bytes recibida.
- **Reanudación:** ante corte, retoma desde el offset ya escrito:
  - FTP/FTPS: `retrbinary(..., rest=offset)` (comando `REST`); si el servidor no lo soporta, reinicia y regístralo.
  - SFTP: `open()` + `seek(offset)` + lectura por bloques.
  - WebDAV: solicita `Accept-Encoding: identity`, lee el cuerpo HTTP crudo y
    envía `Range: bytes=offset-`. Solo acepta `206 Partial Content` cuando
    `Content-Range` es válido y comienza en el offset exacto; si devuelve
    `200`, reinicia desde cero, y si el rango es ambiguo rechaza la respuesta
    sin tocar el parcial.
  - Un parcial solo es reutilizable si tamaño, `mtime` y confiabilidad del
    timestamp identifican la versión remota. Tamaño por sí solo no basta.
- **Verificación de integridad:** siempre compara tamaño final contra el anunciado (`SIZE`, `st_size`,
  `getcontentlength`/`Content-Length`). Con `verify_mode='sha256'`, calcula el hash en streaming durante la
  descarga (sin segunda lectura del disco) y guárdalo en `run_files.sha256`.
  Una respuesta más corta se clasifica como `partial_transfer` reintentable;
  una más larga es `integrity`. Antes de publicar, aplica el `mtime` remoto al
  `.part`; si ese paso falla, conserva el parcial y no crea el archivo final.
- **Reintentos:** hasta `retries` con backoff exponencial + jitter, respetando la política de cortesía.
  Clasifica el error antes de reintentar: `auth` y `permission` no se reintentan, `tcp_timeout` sí.
- **Concurrencia y cortesía** (hereda `throttle.py`, orden de adquisición: lock por host → spacing → rate limit → semáforo global):
  - `max_parallel_files` por conexión (default 2, jamás abrir 20 sesiones contra un FTP corporativo).
  - Una sola sesión de control por host cuando el protocolo lo exija.
  - `bandwidth_limit_kbps` opcional por conexión, implementado como token bucket sobre el callback de bloques.
  - La cancelación interrumpe de forma cooperativa la espera de locks,
    spacing, token bucket y backoff; no reclama otro lote ni inicia otro
    intento después de recibirse.
- **Pre-flight de espacio en disco:** antes de arrancar calcula el crecimiento
  local neto y el staging simultáneo (`min(tamaño_remoto, tamaño_local)` por
  reemplazo activo), añade la reserva configurada y compáralo con el espacio
  libre. Revalida cada lote; si no alcanza, aborta con
  `error_type='disk_space'` sin publicar archivos incompletos.
  Para tamaño conocido descuenta un parcial confiable sharded o legado. Para
  tamaño desconocido reserva 64 MiB solo por worker activo y comprueba el
  espacio incrementalmente durante el stream, antes de escribir cada ventana
  acotada. Un parcial sin identidad remota confiable se reinicia desde cero.
- **Cola para gran volumen:** cada transporte entrega metadatos de forma
  incremental; el orquestador planifica e inserta en SQLite en lotes de hasta
  500 entradas. `run_files` conserva todas las descargas accionables y hasta
  500 decisiones omitidas como muestra; los totales exactos quedan en
  `runs`. Los trabajadores reclaman lotes acotados —como máximo 64 y nunca
  más de dos veces el paralelismo configurado— y reutilizan una sesión por
  worker. El proceso no conserva millones de rutas, futuros ni resultados
  terminales en memoria.
- **Seguridad de rutas:** sanitiza todo nombre proveniente del servidor. Rechaza `..`, rutas absolutas y separadores
  embebidos; resuelve la ruta final y verifica con `Path.resolve().is_relative_to(dest_root)` que no se escapa del destino.
  Un servidor comprometido o mal configurado no puede escribir fuera de la
  carpeta de descargas. Una ruta inválida se registra como `path_invalid` de
  ese archivo, mientras el resto del listado válido continúa.

### RF-4 · Programación, catch-up y disparo manual

- `CronTrigger` diario con hora y minuto configurables por conexión o globales (`schedule.hour`, `schedule.minute`),
  en la zona horaria configurada, con jitter opcional de ±N minutos para no golpear el servidor con todas las conexiones a la vez.
- `misfire_grace_time=None` y `coalesce=True`: una corrida perdida por suspensión del equipo se ejecuta al despertar
  en vez de descartarse en silencio.
- **Catch-up al arranque:** si al iniciar existe una ventana programada del día sin corrida `ok` asociada,
  ejecútala inmediatamente (o tras `startup_delay_s`, default 60 s, para no competir con el arranque de Windows).
  Configurable: `catchup.enabled`, `catchup.max_days` (default 3). En
  comparación completa se colapsan todas las ventanas pendientes a una sola
  reconciliación reciente.
- **Ejecución manual** desde el dashboard (por conexión o todas), con selector de fecha para re-descargar
  una ventana histórica.
- **CLI** sobre el mismo ejecutable, útil para operación y diagnóstico:
  `Recolecta.exe --run-now [--connection ID] [--date YYYY-MM-DD] [--dry-run] [--self-test]`.
- **Instancia única:** mutex nombrado de Windows para que el CLI y el proceso residente jamás corran la misma
  ventana dos veces; si ya hay una instancia, el CLI le delega vía HTTP local.

### RF-5 · Autoarranque y resiliencia

Según §4.3, ambos modos, con `docs/USER_GUIDE.md` explicando cuál elegir. Además:

- Recuperación de corridas colgadas: al arrancar, toda corrida en estado
  `running` y sus filas pendientes se cierran como `failed` con
  `error_type='interrupted'`, recalculando sus agregados. La siguiente
  ejecución vuelve a descubrir el remoto y puede reanudar el staging
  determinista. No se presenta la corrida interrumpida como si hubiera
  continuado o terminado correctamente.
- Detección de suspensión/reanudación del equipo (salto de reloj) sin duplicar corridas.
- Recomendación operativa documentada: `powercfg /change standby-timeout-ac 0` y desactivar hibernación
  en el equipo que hace las descargas.

### RF-6 · Progreso en vivo

- Estado en memoria (`progress.py`) actualizado desde el callback de bloques de cada transporte,
  con throttling de escritura: máximo 1 update persistido por segundo por archivo (no escribas SQLite por cada 8 KB).
- Endpoint `GET /api/runs/current` devuelve: corridas activas, archivo actual por worker,
  `bytes_done/size_bytes`, velocidad instantánea y promedio, ETA por archivo y ETA global,
  contadores `descargados / omitidos / fallidos / pendientes`.
- Para listados masivos, el progreso conserva en memoria solo los archivos
  activos y contadores agregados. Dry-run, respuesta de ejecución y detalle
  devuelven como máximo 500 elementos de muestra, junto con los totales reales
  y un indicador `items_truncated`/`files_truncated`; limitar la presentación
  no limita la cola ni la descarga.
- Dashboard con polling adaptativo: **1 s con corrida activa, 10 s en reposo** (coherente con la decisión
  D-027 del proyecto previo de preferir polling sobre SSE; documenta SSE como alternativa evaluada y descartada
  por complejidad en bundle congelado).
- Barra global + tabla de archivos con estado en vivo, y botón **Cancelar corrida** que termina de forma limpia
  (cierra sesiones, deja `.part` para reanudar después).

### RF-7 · Registro e historial

- `logs/app.log` rotativo (`RotatingFileHandler`, 5 MB × 5, UTF-8), formato `%(asctime)s %(levelname)s %(name)s: %(message)s`.
- **Log estructurado por corrida:** `logs/runs/<YYYY-MM-DD>_<conn-slug>_<run_id>.jsonl`, una línea JSON por evento
  (`run_started`, `file_planned`, `file_started`, `file_progress` cada 10 %, `file_done`, `file_failed`, `run_finished`).
  JSONL porque es apendable, resistente a corte de energía y grepeable sin parser.
- Historial completo de toda descarga accionable en `run_files`: ruta remota,
  ruta local, tamaño, mtime, hash, estado, intentos, causa, duración y
  velocidad. Las decisiones no accionables conservan totales exactos en
  `runs` y una muestra de hasta 500 filas para evitar crecimiento de millones
  de registros idénticos por corrida.
- **Nunca registres credenciales.** Filtro de logging que enmascara `password`, `secret`, `passphrase` y
  las credenciales embebidas en URLs.

### RF-8 · Exportación y descarga de logs

Requisito explícito del cliente: *"mantener los logs de todo lo que se descarga y permitir que se puedan descargar"*.

- `GET /api/export/files.csv?run_id=&connection_id=&from=&to=&status=`
- `GET /api/export/runs.csv?days=`
- `GET /api/runs/{id}/log.jsonl` — descarga directa del log de la corrida.
- `GET /api/export/bundle.zip?days=7` — paquete de soporte: `app.log` + logs de corridas + CSVs + configuración
  **sin secretos**. Es lo que el operador adjunta a un ticket.
- Reporte HTML autocontenido por rango/cliente: totales, tasa de éxito, volumen descargado, top errores, timeline.
  Sin CSS ni JS remoto, listo para enviar por correo.
- **Retención:** purga nocturna configurable del historial en base de datos y de los `.jsonl`
  (default 180 días). **Los archivos descargados jamás se borran automáticamente**; si se implementa una
  política de limpieza local, debe estar apagada por defecto y exigir confirmación escrita.

### RF-9 · Organización del destino local

- Plantilla configurable con tokens: `{root}`, `{client}`, `{connection}`, `{protocol}`, `{yyyy}`, `{MM}`, `{dd}`,
  `{HH}`, `{remote_tree}`, `{remote_dir}`, `{filename}`, `{basename}`, `{ext}`, `{run_id}`.
- La plantilla predeterminada es `{remote_tree}`. Conserva bajo `dest_root`
  la jerarquía completa informada por el remoto —sin su separador inicial y,
  para SMB, desde el nombre del recurso compartido—, por lo que dos archivos
  homónimos en carpetas distintas no se aplanan ni se sobrescriben.
- **Saneamiento Windows obligatorio** (`naming.py`): caracteres inválidos `<>:"/\|?*` y control, nombres reservados
  (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`), puntos y espacios finales, y **límite MAX_PATH de 260**:
  usa el prefijo `\\?\` para rutas largas o trunca el nombre preservando la extensión, dejando registro en el log.
- `on_conflict`: `skip` (default), `overwrite`, `keep_both` (sufijo `__YYYYMMDDHHMMSS`).
- Preserva el `mtime` original del archivo remoto en el archivo local (`os.utime`) — clave para auditoría posterior.

### RF-10 · Seguridad y secretos

- Cifrado local con **DPAPI** en Windows (usuario o máquina según el modo de instalación, §4.3) y **Fernet**
  con keyfile en `dev`/CI. Tokens con prefijo de esquema: `dpapi:`, `dpapi-machine:`, `fernet:`.
  Una base movida entre entornos debe fallar con mensaje accionable, no con un error criptográfico.
- SFTP: host keys con **TOFU** en `data/known_hosts`; un cambio posterior de clave falla el chequeo con causa `tls`.
- FTPS: `prot_p()` protege también el canal de datos y los certificados se verifican por defecto (`ssl_mode='required'`); `insecure` requiere una decisión explícita para una LAN controlada.
  (los certificados autofirmados son la norma en LAN).
- Dashboard en `127.0.0.1` por defecto; `RECOLECTA_BIND_LAN=1` para exponerlo, con advertencia en log y
  Basic Auth vía `RECOLECTA_DASH_USER` / `RECOLECTA_DASH_PASS`. `/healthz` siempre sin auth.
- Los secretos **nunca** viajan al frontend: la API devuelve `has_secret: true/false`, jamás el valor.

### RF-11 · Alertas

- Toast de Windows + ícono de bandeja con color por estado (verde ok, amarillo parcial, rojo fallo, gris en pausa)
  cuando hay sesión interactiva; Windows Event Log en modo `SYSTEM`.
- Canales opcionales: SMTP (servidor interno) y webhook.
- Disparadores: corrida fallida, corrida parcial con más de N archivos fallidos, cero archivos encontrados
  cuando históricamente sí había (**"silencio sospechoso"** — señal temprana de que cambió una ruta o expiró una credencial),
  espacio en disco bajo, credencial rechazada.
- Anti-spam estructural: una alerta por corrida y causa, con log de envíos en `alerts_log`.

### RF-12 · Configuración y respaldo

- Ajustes globales editables desde la UI: hora de corrida, zona horaria, retención, concurrencia global,
  límite de ancho de banda, umbral de espacio libre, canales de alerta.
- Backup/restore JSON de la configuración completa (sin secretos), compatible en ambos sentidos con §16.1.

---

## 8. Requisitos no funcionales

| Atributo | Objetivo |
|---|---|
| Arranque | Dashboard respondiendo `/healthz` en < 5 s desde el lanzamiento del `.exe`. |
| Memoria | < 250 MB en reposo; streaming por bloques de 64 KB, **jamás cargar un archivo completo en RAM**. |
| Volumen | Listados de millones de documentos mediante descubrimiento incremental y cola SQLite, sin crecimiento de memoria proporcional al número total; el volumen en bytes queda limitado por disco, red y política de cortesía. |
| Robustez | Corte de energía a mitad de descarga → al reiniciar no hay archivos corruptos en el destino. |
| Tamaño del bundle | < 120 MB (sin drivers de BD, a diferencia del proyecto previo). |
| Observabilidad | Toda descarga trazable a: quién la disparó, qué ventana cubrió, cuánto tardó, con qué resultado. |
| i18n | UI y errores en español; código e identificadores en inglés. |

---

## 9. Taxonomía de errores

Reutiliza y extiende la del proyecto previo (`errors.py`, clasificación centralizada de excepciones):

`dns` · `tcp_connect` · `tcp_timeout` · `auth` · `tls` · `permission` · `target_missing` · `protocol` · `unknown`

Nuevos, específicos de descarga:

`disk_space` · `disk_write` · `integrity` (tamaño o hash no coincide) · `partial_transfer` ·
`path_invalid` (saneamiento imposible) · `interrupted` (corrida cortada por reinicio) · `timestamp_unreliable`
(el servidor no soporta `MDTM`/`MLSD`).

La interfaz, el detalle y los reportes deben traducir `error_type` a una causa
accionable, en vez de mostrar únicamente **Fallida**:

| `error_type` | Etiqueta visible |
|---|---|
| `auth` | Credencial rechazada |
| `dns` | Servidor no encontrado |
| `tcp_connect` | Servidor no disponible |
| `tcp_timeout` | Tiempo de conexión agotado |
| `tls` | Seguridad TLS/SSH no validada |
| `permission` | Acceso denegado |
| `target_missing` | Ruta remota no existente |
| `disk_space` | Espacio local insuficiente |
| `disk_write` | No se pudo escribir en el destino |
| `integrity` | Validación del archivo fallida |
| `partial_transfer` | Transferencia incompleta |
| `path_invalid` | Ruta no permitida |
| `interrupted` | Ejecución interrumpida |
| `protocol` | Error de protocolo |
| `timestamp_unreliable` | Fecha remota no confiable |
| `unknown` | Error no identificado |

El mensaje técnico saneado permanece disponible en el detalle, pero no
reemplaza la etiqueta y nunca puede contener credenciales.

---

## 10. Dashboard

Sin build frontend: HTML + CSS + JS vanilla servidos por FastAPI, Chart.js vendorizado en `static/vendor/`.

Vistas:

1. **Inicio** — tarjeta por conexión: última corrida, estado, archivos traídos, próxima ejecución, botón "Ejecutar ahora".
2. **Corrida en vivo** — progreso global, tabla de archivos con barras individuales, velocidad, ETA, botón Cancelar.
3. **Historial** — corridas filtrables por conexión/fecha/estado, con drill-down al detalle de archivos.
4. **Archivos** — buscador global sobre `run_files` (por nombre, fecha, estado) con enlace a la ruta local y export CSV.
5. **Conexiones** — CRUD, probar, dry-run, duplicar (nace en pausa), importar backup.
6. **Ajustes** — hora, zona horaria, retención, cortesía, alertas, backup/restore.

Accesibilidad mínima: contraste AA, foco visible, tabla navegable por teclado, estados no comunicados solo por color.
Inicio, Historial y el detalle usan las etiquetas descriptivas de §6.1 y §9;
el valor canónico continúa disponible para filtros, automatización y auditoría.

---

## 11. Empaquetado y dependencias

### 11.1 Estructura de repositorio esperada

```
Recolecta/
├── app/                     # código de la aplicación
├── static/  templates/      # frontend sin build
├── tests/                   # pytest (unitarios + integración con servidores locales)
├── docs/                    # SPEC.md · USER_GUIDE.md · DECISIONS.md · ACCEPTANCE.md · OPERATIONS.md
├── wheelhouse/              # wheels cp312/win_amd64 versionados (build offline)
├── vendor/                  # instalador oficial de Python 3.12
├── .github/workflows/build-windows.yml
├── launcher.py  build.ps1  install.ps1  install-service.ps1  uninstall.ps1
├── requirements.txt  requirements-dev.txt  conftest.py  CLAUDE.md  README.md  LICENSE
```

### 11.2 Dependencias (fijadas, todas en `wheelhouse/`)

```
fastapi==0.115.8
uvicorn==0.34.0
APScheduler==3.11.0
tzlocal
tzdata                 # imprescindible: Windows no trae la base IANA para zoneinfo
paramiko==3.5.1        # SFTP
httpx==0.28.1          # WebDAV/WebDAVS
smbprotocol==1.17.0    # SMB2/SMB3; incluye el módulo smbclient
pyspnego==0.12.1       # autenticación Negotiate usada por smbprotocol
sspilib==0.5.0 ; sys_platform == "win32"  # SSPI nativo para pyspnego
cryptography==44.0.2
pywin32==308  ; sys_platform == "win32"     # DPAPI y Event Log
winotify==1.1.0 ; sys_platform == "win32"
pystray==0.19.5 ; sys_platform == "win32"
Pillow==11.1.0  ; sys_platform == "win32"
```

Dev: `pytest==8.3.4`, `pyftpdlib` (servidor FTP/FTPS local para tests de integración).
FTP/FTPS usa `ftplib` de la stdlib; SMB/UNC usa `smbprotocol`/`smbclient`, `pyspnego` y SSPI. **No agregues drivers de bases de datos.**

### 11.3 `--self-test` del ejecutable congelado

Debe importar y validar: `cryptography.hazmat.*`, `win32crypt`, `paramiko`, `httpx`, `apscheduler`,
`smbclient`, `smbprotocol`, `spnego` y `sspilib`,
y además **resolver una zona horaria IANA** (`ZoneInfo("America/Bogota")`) para detectar en build time
que `tzdata` no quedó incluido — un fallo que de otro modo aparece recién a las 2 AM del día siguiente.

---

## 12. Testing

- **Unitarios:** cálculo de ventana temporal (los tres modos, cambios de horario, fin de mes, año bisiesto),
  saneamiento de nombres Windows, plantillas de destino, dedupe, token bucket, backoff, clasificación de errores.
- **Integración:** `pyftpdlib` levantando un FTP y un FTPS reales en `127.0.0.1` con archivos de mtime controlado;
  WebDAV con `httpx.MockTransport`; SFTP con paramiko mockeado. Casos obligatorios:
  corte a mitad de descarga → reanudación correcta; archivo escribiéndose → omitido por quiet period;
  archivo repetido → marcado `duplicate`; servidor que devuelve `../../evil.txt` → rechazado;
  árboles remotos con homónimos → destinos distintos; comparación completa con archivo ausente,
  diferente, equivalente y extra local; contenido binario no UTF-8 → mismos bytes en destino.
- **Escala:** un listado sintético grande verifica que descubrimiento,
  consultas de identidad, inserciones y reclamos ocurren en lotes acotados,
  que la muestra visible se trunca con sus totales y que el número de archivos
  activos en memoria no crece con la cola.
- **Reloj inyectable** en scheduler y throttle: nada de `sleep()` real en los tests.
- **Test del script de instalación** (patrón heredado de `test_install_script.py`): verifica por regex que
  `install.ps1` conserva `RestartCount`, `MultipleInstances IgnoreNew`, `ExecutionTimeLimit 0` y `StartWhenAvailable`.
- Meta: **cobertura ≥ 85 %** en `app/`, y `build.ps1` aborta el empaquetado si un test falla.

---

## 13. Plan de fases

Cada fase termina con tests verdes, `docs/DECISIONS.md` actualizado y un commit limpio.

| Fase | Alcance | Entregable verificable |
|---|---|---|
| **0** | Andamiaje: repo, `config.py`, `logging_setup.py`, `errors.py`, CI, `CLAUDE.md`, hook de commits | `pytest` corre en vacío; CI verde |
| **1** | Núcleo: modelos, SQLite WAL + migraciones, secretos DPAPI/Fernet, CRUD de conexiones | Crear/leer conexión con secreto cifrado |
| **2** | Transportes: FTP/FTPS/SFTP/WebDAV/SMB con `list()` y `stat()`; ventana temporal; filtros; dry-run | Dry-run lista los archivos correctos contra servidores locales de prueba |
| **3** | Motor de descarga: atómica, reanudación, integridad, throttle, bandwidth cap, pre-flight de disco | Descarga 1 GB con corte simulado y reanuda sin corrupción |
| **4** | Scheduler: cron diario, catch-up, instancia única, CLI `--run-now` | Corrida programada + corrida perdida recuperada al arrancar |
| **5** | Dashboard: vistas, progreso en vivo, cancelación | Progreso visible con ETA durante una corrida real |
| **6** | Logs, exports CSV/JSONL/ZIP, reporte HTML, retención, alertas, bandeja | Bundle de soporte descargable desde la UI |
| **7** | Empaquetado offline: `build.ps1`, `install.ps1`, `install-service.ps1`, Release por CI, `USER_GUIDE.md` | ZIP instalable en Windows sin internet, arranca solo tras reiniciar |

---

## 14. Criterios de aceptación

Escríbelos en `docs/ACCEPTANCE.md` como checklist verificable:

1. En una máquina Windows sin internet ni Python, descomprimir el ZIP y correr `install.ps1` deja el dashboard
   activo en `http://127.0.0.1:8091` en menos de 60 segundos.
2. Tras **reiniciar Windows**, la aplicación vuelve a estar activa sin intervención humana (modo A tras login, modo B tras boot).
3. Con la hora programada a `T+2 min`, la corrida se dispara sola y descarga exclusivamente los archivos
   de la ventana configurada.
4. Apagar el equipo antes de la hora programada y encenderlo 6 horas después ejecuta la corrida perdida (catch-up).
5. Matar el proceso a mitad de una descarga y reiniciar: **no queda ningún archivo incompleto en el destino**,
   y el archivo interrumpido se reanuda desde su offset.
6. Ejecutar la misma corrida dos veces no vuelve a descargar nada: todo se marca `duplicate`.
7. Un archivo que está siendo escrito en el servidor se omite en la corrida actual y se descarga en la siguiente.
8. Durante una corrida activa, la UI muestra el archivo en curso con bytes, porcentaje, velocidad y ETA,
   actualizados al menos una vez por segundo.
9. `GET /api/export/bundle.zip` descarga un paquete con `app.log`, los `.jsonl` de las corridas, los CSVs
   y la configuración, **sin una sola credencial**.
10. Importar un `monitor-backup.json` de StabilityMonitor crea las conexiones FTP/FTPS/SFTP/WebDAV/WebDAVS
    y reporta explícitamente cuántas de base de datos se omitieron.
11. Dos conexiones pueden usar horas diarias diferentes y el catch-up conserva
    la hora propia de cada una; una conexión sin hora hereda la agenda global.
12. Con el volumen destino casi lleno, la corrida aborta antes de descargar y emite alerta `disk_space`.
13. Un servidor que devuelve `../../../Windows\System32\evil.dll` en el listado provoca `path_invalid`
    y **no escribe nada fuera de `dest_root`**.
14. Ninguna credencial aparece en `logs/`, en los exports, ni en ninguna respuesta de la API.
15. `pytest` pasa completo y `build.ps1` genera el paquete sin acceso a la red.
16. Una corrida `ok` con `files_found=0` se presenta como **Archivos no
    existentes**, no como fallo; los demás resultados correctos distinguen
    **Sin archivos nuevos** y **Descarga completada**.
17. Una corrida `failed` con cero archivos conserva el fallo y muestra la causa
    específica de `error_type`; nunca se reclasifica como `no_files`.
18. Dos archivos remotos con el mismo nombre en carpetas distintas conservan
    su árbol relativo bajo `dest_root` y ambos llegan al destino.
19. Con **Comparación completa con carpeta local** activa, un archivo ausente
    o diferente se descarga aunque quede fuera de la ventana o figure en el
    historial exitoso; uno equivalente no se vuelve a transferir.
20. La comparación completa no elimina ni modifica archivos locales que no
    existen en el remoto y sigue respetando filtros, quiet period y symlinks.
21. Un listado masivo se descubre, persiste y consume por lotes acotados; las
    respuestas visibles indican cuándo contienen solo una muestra y conservan
    los contadores reales.
22. Un archivo con bytes no válidos como texto conserva exactamente su
    contenido después de descargarlo; no existe una etapa de codificación.

---

## 15. Entregables finales

1. Repositorio con la estructura de §11.1 y commits limpios por fase.
2. `dist\Recolecta\` autocontenido + ZIP publicado como GitHub Release.
3. `README.md` con capturas, arquitectura (diagrama Mermaid) e instalación rápida.
4. `docs/USER_GUIDE.md`: instalación en ambos modos, configuración de una conexión paso a paso,
   interpretación de logs, resolución de los 10 problemas más comunes.
5. `docs/OPERATIONS.md`: runbook — qué revisar si una corrida falla, cómo forzar una re-descarga,
   cómo rotar credenciales, cómo mover la instalación a otra máquina (y por qué los secretos DPAPI no viajan).
6. `docs/DECISIONS.md` con el registro de decisiones.

---

## 16. Anexos

### 16.1 Formato de exportación de StabilityMonitor (entrada de importación)

```json
{
  "app": "StabilityMonitor",
  "version": "2.0.0",
  "exported_at": "2026-07-26T05:00:00+00:00",
  "note": "Los secretos no se exportan: deberán reingresarse al restaurar.",
  "settings": { },
  "connections": [
    {
      "name": "SFTP Producción",
      "client": "Cliente A",
      "protocol": "SFTP",
      "host": "10.0.0.10",
      "port": 22,
      "username": "monitor",
      "auth_type": "password",
      "key_path": null,
      "db_name": null,
      "sql_instance": null,
      "ssl_mode": "required",
      "targets_json": "[\"/entrada\"]",
      "aliases_json": "[]",
      "health_query": null,
      "interval_s": 60,
      "timeout_s": 10,
      "retries": 2,
      "degraded_ms": null,
      "write_check": 0,
      "enabled": 1,
      "notes": ""
    }
  ]
}
```

Campos que Recolecta consume: `name`, `client`, `protocol`, `host`, `port`, `username`, `auth_type`,
`key_path`, `ssl_mode`, `targets_json` → `remote_paths_json`, `timeout_s`, `retries`, `notes`,
y `secret` en claro si viene (solo en importación, se cifra al vuelo).
Campos que ignora: `db_name`, `sql_instance`, `health_query`, `interval_s`, `degraded_ms`, `write_check`, `aliases_json`.

### 16.2 Variables de entorno

| Variable | Efecto |
|---|---|
| `RECOLECTA_DATA_DIR` | Sobrescribe la carpeta base (datos, logs, exports). |
| `RECOLECTA_PORT` | Puerto del dashboard (default `8091`). |
| `RECOLECTA_BIND_LAN` | `1` para escuchar en toda la LAN (log de advertencia). |
| `RECOLECTA_DASH_USER` / `RECOLECTA_DASH_PASS` | Activan Basic Auth si ambas están definidas. |
| `RECOLECTA_MODE` | Fuerza `windows` \| `service` \| `dev` (útil en CI). |
| `RECOLECTA_SECRET_KEY` | Clave Fernet en modo `dev`; ignorada en Windows. |

### 16.3 Nombre del proyecto

`Recolecta` es el nombre de trabajo. Alternativas coherentes con el naming del autor:
`NightFetch`, `DailyDrop`, `FileHarvest`, o `Recolector` si se prefiere español.
Si se cambia, ajusta el nombre de la tarea programada, el `.exe`, el prefijo de variables de entorno
y los nombres de módulo de forma consistente en todo el repositorio.
