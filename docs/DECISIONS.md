# Registro de decisiones

## D-001 · Estado portable bajo una única raíz

Toda ruta mutable se deriva de `RECOLECTA_DATA_DIR`, del directorio del ejecutable congelado o de la raíz del repositorio en desarrollo. Así el paquete no depende del directorio de trabajo y puede moverse como una unidad.

## D-002 · Configuración de proceso por variables de entorno validadas

El puerto, la exposición a LAN, las credenciales del dashboard y el modo de ejecución se validan al arrancar. Una configuración incompleta falla con un mensaje accionable antes de iniciar trabajos.

## D-003 · Taxonomía de errores estable

Las excepciones de librerías y del sistema se convierten a identificadores persistentes definidos en `ErrorType`. La lógica de reintento consume esos identificadores y no textos variables de excepciones.

## D-004 · Redacción en el límite del sistema de logging

El filtro se instala en el handler rotativo y procesa el mensaje ya interpolado. De este modo cubre llamadas con argumentos y evita que credenciales nombradas, URLs autenticadas o tokens cifrados lleguen al archivo.

## D-005 · Fases verificadas por pruebas

Cada fase termina con pruebas verdes. El pipeline de Windows ejecuta `pytest` y el autodiagnóstico antes de considerar válido el andamiaje.

## D-006 · Migraciones secuenciales sin ORM

SQLite se gestiona con la biblioteca estándar y una tabla `schema_migrations`. Cada versión contiene sentencias explícitas ejecutadas dentro de una transacción. Esto mantiene pequeño y predecible el bundle offline y evita introducir un ORM no autorizado.

## D-007 · El repositorio es el límite de seguridad de credenciales

`Connection` nunca contiene el token cifrado y solo expone `has_secret`. `ConnectionRepository` es el único componente que lee `secret_encrypted`, cifra antes de insertar y descifra únicamente mediante una llamada explícita de backend.

## D-008 · Esquema criptográfico visible y fallo accionable

Cada token lleva el prefijo `fernet:`, `dpapi:` o `dpapi-machine:`. Un proceso que recibe un esquema incompatible no propaga errores criptográficos: indica que la credencial pertenece a otro equipo, cuenta o modo y solicita reingresarla.

## D-009 · Fernet persistente para desarrollo y CI

En modo `dev`, `RECOLECTA_SECRET_KEY` tiene prioridad. Si no existe, se crea atómicamente `data/.secret.key` con permisos restrictivos. Esto permite reiniciar el proceso sin perder acceso a secretos y conserva builds reproducibles cuando CI inyecta una clave.

## D-010 · DPAPI de máquina con entropía protegida

El modo `service` usa `CRYPTPROTECT_LOCAL_MACHINE` y 32 bytes adicionales en `data/.entropy`. La ACL del archivo admite únicamente `SYSTEM` y `Administrators`; si no puede aplicarse, la configuración falla antes de almacenar credenciales con una protección incompleta.

## D-011 · Ventanas semiabiertas calculadas en hora local y comparadas en UTC

`calendar_day` construye los dos límites en la zona IANA de la conexión y solo después los convierte a UTC. Esto conserva días de 23 o 25 horas durante cambios DST. Todos los modos producen `[inicio, fin)`, evitando que un archivo exactamente en el límite aparezca en dos ventanas contiguas.

## D-012 · Arranque de `since_last_run` sin historial

Si aún no existe una corrida exitosa, `since_last_run` usa `window_hours` hacia atrás desde el inicio actual. Las corridas siguientes parten del último `window_end_utc` exitoso menos el solape configurado.

## D-013 · Un único planner para dry-run y descarga

Ventana, quiet period, globs, tamaños, symlinks y deduplicación se aplican en `orchestrator.plan_listing`. El dry-run solo cambia el consumidor del plan; así no puede prometer archivos distintos de los que procesará el motor.

## D-014 · Jerarquía temporal FTP con degradación explícita

FTP usa `modify` de `MLSD` durante listados —UTC según RFC 3659— y reserva `MDTM` para `stat` de un archivo individual. Esto evita un comando adicional por cada documento. Si el servidor no implementa MLSD, cae a `LIST`; la precisión temporal limitada queda registrada como observación técnica, pero no convierte una corrida sin archivos fallidos en parcial.

## D-015 · Adaptadores de metadatos sin lectura de contenido

SFTP usa `listdir_attr` y TOFU en `data/known_hosts`; WebDAV usa `PROPFIND` con `getlastmodified`; SMB usa `smbprotocol`/`smbclient` para SMB2/SMB3, registra una sesión con credenciales explícitas y obtiene metadatos mediante `stat` sobre UNC. Ningún adaptador de Fase 2 ejecuta comandos de descarga.

## D-016 · Staging determinista por identidad remota

El `.part` usa un UUIDv5 derivado de conexión, ruta remota, mtime y tamaño.
Sigue teniendo un nombre opaco, pero puede localizarse tras reiniciar sin
persistir un nombre aleatorio adicional. Un cambio real del archivo remoto
produce otro staging y evita mezclar contenidos. Para no concentrar millones
de entradas en un directorio, el mismo UUID se ubica en
`.staging/<dos primeros hex>/<uuid>.part`; el cálculo de identidad no cambió.
Después del pre-flight, el formato plano legado se migra con `os.replace`. Si
no puede migrarse se reutiliza en su lugar original y, si ambas rutas existen,
prevalece el shard.

## D-017 · Publicación atómica después de integridad

El staging vive bajo el mismo `dest_root` que el archivo definitivo. Después de `flush`, `fsync` y validación de tamaño/SHA-256, `os.replace` publica el archivo atómicamente. No se crea ninguna carpeta ni sesión remota si falla el pre-flight de espacio.

## D-018 · Hash de parciales antes de reanudar

Con SHA-256, un proceso reiniciado lee una vez el parcial existente para reconstruir el estado del hash y continúa actualizándolo con los bloques de red. Nunca relee el archivo completo después de terminar. En modo `size`, el parcial no se relee.

## D-019 · Reinicio controlado cuando resume no está disponible

FTP intenta `REST`, SFTP hace `seek` y WebDAV exige `206` para un `Range`. En
WebDAV, un `206` solo es válido si `Content-Range` está bien formado y comienza
exactamente en el offset solicitado; una respuesta ambigua se rechaza antes de
modificar el parcial. Si el servidor responde `200`, el transporte trunca el
staging, reinicia los contadores/hash y registra `resume_supported=false`.
WebDAV solicita `Accept-Encoding: identity` y consume bytes crudos para impedir
que una descompresión HTTP transparente cambie el contenido publicado.

## D-020 · Saneamiento y truncación resistente a colisiones

Los componentes Windows se limpian de caracteres inválidos, nombres reservados y terminaciones prohibidas. Las rutas remotas con `..`, drive o UNC se rechazan. Si MAX_PATH obliga a truncar, se preserva la extensión y se incorpora un hash corto del nombre original para reducir colisiones.

## D-021 · Cortesía compartida entre trabajadores

El motor usa un lock breve por host para espaciar aperturas, un semáforo global y un token bucket compartido por conexión. Los reintentos aplican backoff exponencial con jitter; autenticación, permisos e integridad no se reintentan.

## D-022 · Un job cron por conexión y zona IANA

APScheduler mantiene un `CronTrigger` por conexión con su propia zona IANA. Para obtener jitter simétrico, el trigger parte de `hora-N` y aplica un jitter nativo de `2N`, mientras la ventana se asocia a la hora nominal. Los jobs usan `misfire_grace_time=None`, `coalesce=True` y `max_instances=1`; una suspensión no descarta silenciosamente la ejecución y varias pérdidas contiguas no crean una estampida.

## D-023 · Catch-up asociado por ventana, no por fecha de proceso

Una ventana se considera atendida si existe una corrida `ok` con los mismos límites UTC. Por compatibilidad, también se reconoce un `partial` creado por versiones anteriores únicamente cuando no tiene archivos fallidos, `error_type`, mensaje de error ni advertencias accionables persistidas: esas versiones elevaban observaciones FTP benignas a parcial. El catch-up examina hasta `catchup.max_days`, ordena candidatos del más antiguo al reciente y vuelve a comprobar dentro del coordinador para cerrar carreras con un misfire de APScheduler. La hora nominal calcula la ventana, mientras `runs.started_at` conserva la hora real de ejecución.

## D-024 · Recuperación conservadora de estados interrumpidos

Al arrancar, las corridas `running` se terminalizan como `failed/interrupted`; sus archivos `pending` o `downloading` también quedan terminalizados como `failed/interrupted` dentro de la corrida original y no vuelven a `pending`. El archivo `.part` determinista permanece, por lo que una corrida nueva —manual, programada o de catch-up— puede reutilizarlo y reanudar los bytes sin presentar la corrida antigua como exitosa.

## D-025 · Mutex global y delegación HTTP local

Windows usa `Global\Recolecta.Singleton`; desarrollo usa un lock de archivo. Un CLI que no obtiene el mutex no compite por SQLite ni por el servidor: envía la orden a `127.0.0.1` y la instancia propietaria aplica el lock por conexión.

## D-026 · Salud rápida y catch-up diferido

El API y el scheduler arrancan antes del `startup_delay_s`. El catch-up espera en un hilo daemon para que `/healthz` no dependa de red, credenciales ni descargas. Un detector periódico considera tanto saltos relativos como intervalos largos cuando el monotonic de Windows también avanza durante la suspensión.

## D-027 · Progreso vivo en memoria y checkpoints a 1 Hz

Los callbacks de bloque actualizan un registro protegido por lock con velocidad, porcentaje y ETA. SQLite recibe como máximo un checkpoint por segundo y archivo; el resultado terminal no se limita. Esto mantiene fluida la UI sin convertir la base en un cuello de botella.

## D-028 · Dashboard sin build ni dependencias remotas

FastAPI sirve HTML, CSS, JavaScript vanilla y Chart.js 4.4.7 vendorizado. El sondeo usa un segundo durante corridas activas y diez segundos en reposo. La misma distribución funciona desconectada y los recursos se resuelven tanto desde el árbol fuente como desde `_MEIPASS`.

## D-029 · API pública de salud y Basic Auth perimetral

Cuando existen las dos variables de credenciales, un middleware protege dashboard, estáticos, documentación y API con comparación constante. `/healthz` siempre queda exento. Exponer a la LAN sin Basic Auth produce una advertencia explícita.

## D-030 · JSONL apendable con progreso por umbrales

Cada corrida abre un archivo propio y hace `flush` después de cada evento. El progreso se registra únicamente cuando cruza un múltiplo del 10 %, mientras la UI conserva granularidad por bloque en memoria. Un corte puede perder como máximo el evento en curso, no el historial previo.

## D-031 · Exports seguros y reproducibles

CSV usa UTF-8 con BOM y antepone una comilla a valores que Excel podría ejecutar como fórmula. El bundle reúne CSV, HTML, configuración pública y logs por un rango común. Las claves sensibles se excluyen y todos los errores persistidos pasan por el mismo redactor de credenciales.

## D-032 · Retención por evidencia, nunca por contenido descargado

Una tarea global a las 03:30 UTC elimina corridas anteriores al periodo configurado; las claves foráneas limpian archivos y alertas relacionados. JSONL y exports se purgan por mtime. `downloads/` no se pasa al servicio de retención y no existe una política automática para borrarlo.

## D-033 · Anti-spam mediante claim transaccional

Antes de enviar, cada canal inserta `(run_id, cause, channel)` en `alerts_log`. El índice único convierte reintentos, reinicios o evaluaciones concurrentes en no-op. La fila termina en `sent` o `failed`, conservando evidencia de entrega sin duplicar avisos.

## D-034 · Integraciones de escritorio cargadas de forma perezosa

`runtime_mode()` separa desarrollo, Windows interactivo y `SYSTEM`. `winotify`, `pystray`, Pillow y Event Log sólo se importan dentro del código que los usa. El residente interactivo crea bandeja; el modo headless conserva dashboard, log, Event Log, SMTP y webhook sin depender de la sesión 0.

## D-035 · Los fallos de listado también son corridas auditables

La ventana se calcula antes de autenticar o listar. Si ese pre-flight falla durante una ejecución real, se crea una corrida `failed`, un JSONL terminal y las alertas correspondientes. El dry-run continúa sin efectos laterales.

## D-036 · Distribución portable `onedir` y compilación offline verificable

El paquete oficial es PyInstaller `onedir` sin consola. `build.ps1` valida
SHA-256 y rechaza artefactos offline no inventariados, acepta únicamente
CPython.org 3.12 x64 —o instala el bootstrap oficial
vendorizado— y obliga a `pip --no-index --find-links wheelhouse`. Las pruebas
se ejecutan antes de congelar y un autodiagnóstico del ejecutable importa
explícitamente DPAPI, bandeja, toast, Pillow, APScheduler, cryptography,
la cadena SMB (`smbclient`, `smbprotocol`, `spnego`, `sspilib`) y tzdata.
Un fallo impide crear el ZIP.

## D-037 · Dos tareas programadas, un modo criptográfico explícito

El modo usuario usa una tarea al logon y DPAPI de usuario. El modo headless usa
una tarea al startup como `SYSTEM`, privilegio máximo y el argumento interno
`--service`, que fuerza DPAPI de máquina aunque el entorno heredado no defina
`RECOLECTA_MODE`. Ambos usan `IgnoreNew`, reinicio continuo, inicio tardío,
batería permitida y duración ilimitada; el modo `SYSTEM` añade `WakeToRun`.

## D-038 · La desinstalación separa ejecución de datos

`uninstall.ps1` desregistra ambos nombres de tarea y solo detiene procesos
`Recolecta.exe` cuya carpeta coincide con el bundle desde el que se
ejecuta. No elimina aplicación, base, logs, exports, staging ni descargas. La
eliminación de evidencia y contenido queda como decisión manual posterior.

## D-039 · CPython 3.12.10 es el bootstrap binario de la línea 3.12

Se vendoriza el instalador oficial x64 3.12.10 porque es la última versión de
Python 3.12 publicada con instaladores binarios de Windows. Versiones de
seguridad posteriores de la línea 3.12 se publican solo como código fuente. El
instalador sirve únicamente para la estación de build; el equipo destino
recibe el runtime ya congelado.

## D-040 · Cobertura como compuerta offline, no como reporte opcional

`coverage` y `pytest-cov` son las únicas dependencias de desarrollo añadidas
fuera de la lista inicial, justificadas por la meta explícita de cobertura.
También se fijan, se vendoriza su wheel y se incluyen en SHA256SUMS. `pytest`
falla por debajo de 85 % de `app/`, por lo que `build.ps1` no puede empaquetar
una revisión que incumpla el umbral.

## D-041 · Aceptación portable sobre el ZIP real

El smoke test no ejecuta código fuente ni Python: valida el hash de release,
extrae el ZIP en una carpeta nueva, ejecuta el autodiagnóstico congelado,
arranca `Recolecta.exe` en un puerto libre y exige `/healthz`, dashboard y
JavaScript local en menos de cinco segundos. Siempre detiene el PID exacto y
elimina únicamente su directorio temporal verificado. CI publica el JSON de
evidencia junto al ZIP.

## D-042 · La sesión de Windows se consulta mediante `win32ts`

`ProcessIdToSessionId` pertenece a `win32ts`, no a `win32process`. La detección
de modo interactivo usa ese módulo y está probada para sesión 0, sesión de
usuario, cuenta SYSTEM, plataforma no Windows y error conservador. Esto evita
que el modo usuario falle antes de crear la bandeja.

## D-043 · Recolecta es la identidad única del producto

La renombrada abarca interfaz, API, alertas, exportaciones, base de datos,
variables `RECOLECTA_*`, mutex, tareas programadas, Event Log, ejecutable,
bundle y ZIP. El build limpia `dist` antes de congelar para impedir que una
release mezcle artefactos con identidades anteriores.

Los enlaces de GitHub y LinkedIn usan SVG embebido para conservar la operación
offline. Se abren en una pestaña nueva con `noopener noreferrer`, incluyen
etiquetas accesibles y no incorporan scripts, fuentes ni imágenes remotas.

## D-044 · GitHub Releases publica un instalador offline autoextraíble

`Recolecta-Setup.exe` se congela como `onefile` y contiene el bundle `onedir`
ya verificado. La instalación predeterminada es por usuario en
`%LOCALAPPDATA%\Recolecta`; al actualizar desregistra de forma segura la
instancia de esa carpeta, copia la nueva aplicación, conserva el estado y
registra nuevamente la tarea mediante `install.ps1`.

El modo `--extract-only` permite que CI valide hash, contenido y
autodiagnóstico sin alterar tareas programadas. El workflow adjunta el Setup,
el ZIP portable, los dos reportes smoke y `SHA256SUMS.txt` a cada release
creada desde una etiqueta `v*.*.*`.

## D-045 · La importación es parcial, segura e idempotente

El endpoint de importación acepta backups de StabilityMonitor y exportaciones
seguras de Recolecta. Cada entrada se procesa por separado: FTP, FTPS, SFTP,
WebDAV(S) y SMB se normalizan con el modelo vigente; SQL Server, Oracle y
otros protocolos se omiten con motivo explícito. Una entrada inválida no
revierte las válidas. La huella nombre–protocolo–host–puerto evita duplicados
al repetir el archivo. Toda conexión importada se guarda en pausa hasta probar
sus rutas en esta instalación. Si trae `secret`, se cifra inmediatamente
mediante el `SecretStore` y nunca aparece en la respuesta; si no lo trae, el
operador debe ingresarlo antes de validar y activar.

## D-046 · La hora global es un valor heredable por conexión

`connections.schedule_time` es nullable y usa `HH:MM`. Un valor presente
define el `CronTrigger` y el catch-up de esa conexión en su propia zona IANA;
un valor nulo hereda `schedule.hour` y `schedule.minute`. Así se mantiene la
compatibilidad con bases existentes y se pueden distribuir cargas a lo largo
del día sin duplicar el mecanismo de agenda.

## D-047 · El ejecutable congelado no consulta WMI para identificar Windows

CPython 3.12 intenta consultar WMI desde `platform.py` antes de arrancar
Uvicorn. En equipos administrados, un proveedor de seguridad inyectado en
`fastprox.dll` puede terminar el proceso nativo antes de que exista un log.
El build excluye el módulo opcional `_wmi`; la biblioteca estándar usa entonces
su fallback soportado (`sys.getwindowsversion()` y el registro). Recolecta no
usa WMI para ninguna función, por lo que se evita ese punto de fallo sin
reducir capacidades del producto.

## D-048 · Guardar exige validar el borrador y ambos extremos

El editor prueba la configuración todavía no persistida mediante
`POST /api/connections/validate`. En una edición, omitir el secreto reutiliza
la credencial cifrada existente solo en memoria; escribir uno nuevo lo prueba
sin guardarlo. Si cambia servidor, puerto, protocolo, usuario o autenticación,
debe volver a ingresarlo para impedir que una credencial almacenada se envíe
silenciosamente a otro destino. La validación exige al menos una ruta remota,
autentica y lista cada raíz sin recursión ni descarga. Ese listado confirma el
acceso a la raíz, pero no demuestra que no existan archivos en subcarpetas; la
corrida operativa aplica `recursive` y `max_depth` según la conexión.

El destino local y su plantilla se resuelven con las mismas reglas de operación
y se prueban creando, escribiendo, renombrando y eliminando un archivo
temporal. Las carpetas creadas únicamente para la prueba se eliminan si siguen
vacías. La interfaz mantiene **Guardar conexión** bloqueado hasta un resultado
correcto; cualquier cambio o respuesta obsoleta invalida esa aprobación.

El backend vuelve a validar antes de crear, habilitar o cambiar conectividad,
credencial o rutas, de modo que una llamada directa no evite la regla.
Como salvaguarda operativa del API, cambios puramente descriptivos y la acción
de pausar siguen disponibles si el servidor está caído; el editor mantiene la
regla más estricta y vuelve a probar cualquier cambio. Importación y duplicado
conservan su excepción segura:
crean borradores en pausa para validarlos posteriormente; el duplicado no
copia la credencial y la importación cifra la que venga en el archivo.

Toda prueba SFTP usa una copia temporal de `known_hosts`, incluso la
revalidación del guardado, por lo que un borrador fallido nunca modifica el
almacén real. La primera sesión operativa conserva la política TOFU existente
y fija allí una clave nueva. El backend serializa las mutaciones de conexión y
toma conexión y secreto como un solo snapshot para no mezclar cambios
concurrentes.

## D-049 · El resultado descriptivo no reemplaza el estado canónico

`runs.status` mantiene el contrato
`running|ok|partial|failed|cancelled`. Una consulta remota válida sin archivos
es una ejecución correcta y se persiste como `ok`; cambiarla a un nuevo estado
rompería la semántica de catch-up, `since_last_run`, métricas y consumidores
existentes.

Para la presentación se deriva `result_status` después de evaluar el estado
canónico: `ok` con `files_found=0` produce `no_files` y la etiqueta
**Sin archivos encontrados**; `ok` con archivos encontrados pero ninguno
descargado produce `no_changes` y **Sin archivos nuevos**; `ok` con al menos
una descarga produce `completed` y **Descarga completada**. `partial` solo se
usa como **Completada con incidencias** cuando hubo archivos fallidos, rutas
aisladas u otra causa accionable. Las degradaciones compatibles de listado o
precisión se guardan como observaciones y no alteran el resultado. Los
`partial` históricos sin fallos, categoría, mensaje ni advertencias accionables
conservan su valor de auditoría, pero se presentan según su resultado real.
`running` y `cancelled` se explican
como **En ejecución** y **Cancelada por el usuario**.

`no_files` expresa cero archivos dentro del alcance recorrido, no inexistencia
absoluta en el servidor. Sin recursión se inspecciona solo el nivel inicial de
cada raíz; con recursión se incluyen subcarpetas hasta `max_depth`. La
comparación completa constituye un alcance distinto y recorre todo el árbol
remoto configurado.

La precedencia evita que un fallo de autenticación, red o ruta con cero
archivos se confunda con un listado vacío. Una corrida `failed` conserva ese
estado y obtiene una etiqueta accionable desde `error_type`, con una causa
genérica solo para códigos desconocidos. API y exports preservan `status` y
pueden añadir `result_status` y `status_label`, sin reescribir el historial.

## D-050 · Árbol remoto, reconciliación unidireccional y cola acotada

El destino predeterminado usa `{remote_tree}` y conserva todos los componentes
de la ruta remota bajo `dest_root`. En rutas POSIX se elimina únicamente el
separador inicial; en UNC se valida el host configurado y el árbol empieza en
el recurso compartido. Cada asignación se reserva de forma persistente por
conexión, alcance de plantilla y ruta remota. Si el saneamiento de Windows
hiciera coincidir dos rutas diferentes, se añade un sufijo hash estable en vez
de permitir que una publicación reemplace a la otra.

Cada conexión puede activar `full_local_reconciliation`. En ese modo se
recorre todo el árbol de sus raíces, se omiten ventana e historial exitoso y
se compara cada archivo remoto con su destino. Se conserva quiet period,
globs, tamaños y la prohibición de seguir symlinks. Un archivo regular local
se considera equivalente por tamaño —si el remoto lo conoce— y por `mtime`
con tolerancia de dos segundos —si está disponible—. Los ausentes o diferentes
se descargan, los equivalentes se omiten y los archivos locales extra no se
borran ni modifican. Es deliberadamente una reconciliación
**remoto → local**, no una sincronización bidireccional.

El historial exitoso deja de ser una restricción única global: sigue sirviendo
para deduplicar la ventana normal, pero no puede impedir reparar un destino
eliminado o alterado. La unicidad se aplica dentro de cada corrida y una tabla
de reservas mantiene estable el destino entre corridas.

Los transportes producen metadatos incrementalmente y sus directorios
pendientes usan una cola temporal respaldada por disco. El orquestador
clasifica en lotes de hasta 500; `run_files` conserva toda la cola accionable
y una muestra máxima de 500 decisiones no accionables, mientras `runs`
mantiene sus totales exactos. Así una reconciliación diaria sin cambios no
duplica millones de filas de auditoría. Los trabajadores reclaman lotes no
mayores de 64 ni de dos veces el paralelismo configurado, reutilizan una
sesión remota por worker y el progreso conserva solo archivos activos más
contadores agregados. Dry-run, resultados y detalle exponen como máximo 500
elementos de muestra, junto con totales reales e indicadores de truncamiento.
Por ello el uso de memoria no crece linealmente con listados de millones de
documentos.

Una secuencia suficientemente larga de fallos sistémicos equivalentes
—autenticación, DNS, permisos, protocolo, conexión, timeout, TLS o transferencia
parcial— abre un cortacircuitos. El lote ya ejecutado se persiste y el resto de
la cola se terminaliza con la misma causa mediante una sola actualización; un
éxito o un error distinto reinicia la secuencia. Así no se crean millones de
sesiones ni parciales cuando el origen completo requiere intervención.

La durabilidad de la cola conserva planificación y evidencia, pero no se
interpreta como continuación exacta de la misma corrida tras un crash. Al
arrancar, esa corrida se cierra como `failed/interrupted`; una ejecución nueva
redescubre el remoto y puede reutilizar el `.part` determinista.

Todo contenido se trata como bytes opacos desde el transporte hasta
`os.replace`. No se decodifica texto, no se normalizan saltos de línea y no se
recodifica ningún documento. Tamaño y, cuando se solicita, SHA-256 verifican
la secuencia binaria publicada.

## D-051 · Retención conservadora y acotada del staging

El arranque limpia `.staging` una vez por raíz de destino distinta y no sigue
symlinks. Un `.part` vacío no aporta reanudación y puede eliminarse de
inmediato; uno con contenido solo es huérfano eliminable cuando su `mtime` es
anterior a `max(7 días, catchup.max_days + 1 día)`. Así una suspensión larga o
una ventana todavía recuperable no pierde bytes ya transferidos. Los
parciales activos o recientes y los archivos ajenos a `.part` se conservan,
y los shards que quedan vacíos se retiran.

El recorrido no construye una lista de rutas: devuelve únicamente contadores
de parciales examinados/eliminados, bytes liberados, shards retirados y
errores. Una raíz inaccesible o un fallo individual genera una advertencia y
no bloquea el resto de destinos ni el inicio del servicio.

## D-052 · Reserva conservadora, metadatos y cancelación cooperativa

Un archivo con tamaño remoto conocido reserva únicamente los bytes pendientes
después de descontar un parcial válido, tanto en staging sharded como legado.
El parcial solo se considera válido cuando tamaño, `mtime` y confiabilidad del
timestamp identifican la versión remota; tamaño por sí solo no permite mezclar
un prefijo viejo con un objeto nuevo.

Si el tamaño es desconocido se reservan 64 MiB por worker que puede estar
activo, no por cada elemento del inventario, y el parcial anterior se descarta.
Durante el stream se comprueba el espacio antes de escribir ventanas acotadas,
incluyendo el margen de los workers simultáneos. Esto mantiene acotado el
pre-flight para millones de documentos sin permitir que un objeto mayor a
64 MiB agote el volumen.

Una transferencia menor al tamaño anunciado se clasifica como
`partial_transfer` y puede reintentarse; una mayor es `integrity` y no se
publica. El `mtime` remoto se aplica al `.part` después de `fsync` y antes de
`os.replace`, de modo que un fallo de metadatos conserva el parcial y nunca
deja un archivo definitivo que aparenta estar completo.

La cancelación interrumpe descubrimiento, adquisición de cupos, espera entre
solicitudes, token bucket y backoff mediante el mismo evento cooperativo. Una
lectura de red ya iniciada depende del timeout del transporte, pero no se
extrae otro lote ni se inicia otro intento después de recibir la cancelación.
