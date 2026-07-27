# Registro de decisiones

## D-001 · Estado portable bajo una única raíz

Toda ruta mutable se deriva de `HARVESTER_DATA_DIR`, del directorio del ejecutable congelado o de la raíz del repositorio en desarrollo. Así el paquete no depende del directorio de trabajo y puede moverse como una unidad.

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

En modo `dev`, `HARVESTER_SECRET_KEY` tiene prioridad. Si no existe, se crea atómicamente `data/.secret.key` con permisos restrictivos. Esto permite reiniciar el proceso sin perder acceso a secretos y conserva builds reproducibles cuando CI inyecta una clave.

## D-010 · DPAPI de máquina con entropía protegida

El modo `service` usa `CRYPTPROTECT_LOCAL_MACHINE` y 32 bytes adicionales en `data/.entropy`. La ACL del archivo admite únicamente `SYSTEM` y `Administrators`; si no puede aplicarse, la configuración falla antes de almacenar credenciales con una protección incompleta.

## D-011 · Ventanas semiabiertas calculadas en hora local y comparadas en UTC

`calendar_day` construye los dos límites en la zona IANA de la conexión y solo después los convierte a UTC. Esto conserva días de 23 o 25 horas durante cambios DST. Todos los modos producen `[inicio, fin)`, evitando que un archivo exactamente en el límite aparezca en dos ventanas contiguas.

## D-012 · Arranque de `since_last_run` sin historial

Si aún no existe una corrida exitosa, `since_last_run` usa `window_hours` hacia atrás desde el inicio actual. Las corridas siguientes parten del último `window_end_utc` exitoso menos el solape configurado.

## D-013 · Un único planner para dry-run y descarga

Ventana, quiet period, globs, tamaños, symlinks y deduplicación se aplican en `orchestrator.plan_listing`. El dry-run solo cambia el consumidor del plan; así no puede prometer archivos distintos de los que procesará el motor.

## D-014 · Jerarquía temporal FTP con degradación explícita

FTP consulta `MDTM` por archivo, usa el dato `modify` de `MLSD` si `MDTM` no está disponible y cae a `LIST` únicamente cuando el servidor no implementa MLSD. Los resultados de `LIST` se conservan para operación, pero producen advertencias y un plan parcial por su precisión limitada.

## D-015 · Adaptadores de metadatos sin lectura de contenido

SFTP usa `listdir_attr` y TOFU en `data/known_hosts`; WebDAV usa `PROPFIND` con `getlastmodified`; SMB usa `stat` sobre UNC y puede establecer credenciales explícitas con `WNetAddConnection2`. Ningún adaptador de Fase 2 ejecuta comandos de descarga.

## D-016 · Staging determinista por identidad remota

El `.part` usa un UUIDv5 derivado de conexión, ruta remota, mtime y tamaño. Sigue teniendo un nombre opaco, pero puede localizarse tras reiniciar sin persistir un nombre aleatorio adicional. Un cambio real del archivo remoto produce otro staging y evita mezclar contenidos.

## D-017 · Publicación atómica después de integridad

El staging vive bajo el mismo `dest_root` que el archivo definitivo. Después de `flush`, `fsync` y validación de tamaño/SHA-256, `os.replace` publica el archivo atómicamente. No se crea ninguna carpeta ni sesión remota si falla el pre-flight de espacio.

## D-018 · Hash de parciales antes de reanudar

Con SHA-256, un proceso reiniciado lee una vez el parcial existente para reconstruir el estado del hash y continúa actualizándolo con los bloques de red. Nunca relee el archivo completo después de terminar. En modo `size`, el parcial no se relee.

## D-019 · Reinicio controlado cuando resume no está disponible

FTP intenta `REST`, SFTP hace `seek` y WebDAV exige `206` para un `Range`. Si el servidor rechaza la operación o responde `200`, el transporte trunca el staging, reinicia los contadores/hash y registra `resume_supported=false`.

## D-020 · Saneamiento y truncación resistente a colisiones

Los componentes Windows se limpian de caracteres inválidos, nombres reservados y terminaciones prohibidas. Las rutas remotas con `..`, drive o UNC se rechazan. Si MAX_PATH obliga a truncar, se preserva la extensión y se incorpora un hash corto del nombre original para reducir colisiones.

## D-021 · Cortesía compartida entre trabajadores

El motor usa un lock breve por host para espaciar aperturas, un semáforo global y un token bucket compartido por conexión. Los reintentos aplican backoff exponencial con jitter; autenticación, permisos e integridad no se reintentan.

## D-022 · Un job cron por conexión y zona IANA

APScheduler mantiene un `CronTrigger` por conexión con su propia zona IANA. Para obtener jitter simétrico, el trigger parte de `hora-N` y aplica un jitter nativo de `2N`, mientras la ventana se asocia a la hora nominal. Los jobs usan `misfire_grace_time=None`, `coalesce=True` y `max_instances=1`; una suspensión no descarta silenciosamente la ejecución y varias pérdidas contiguas no crean una estampida.

## D-023 · Catch-up asociado por ventana, no por fecha de proceso

Una ventana se considera atendida únicamente si existe una corrida `ok` con los mismos límites UTC. El catch-up examina hasta `catchup.max_days`, ordena candidatos del más antiguo al reciente y vuelve a comprobar dentro del coordinador para cerrar carreras con un misfire de APScheduler. La hora nominal calcula la ventana, mientras `runs.started_at` conserva la hora real de ejecución.

## D-024 · Recuperación conservadora de estados interrumpidos

Al arrancar, las corridas `running` pasan a `failed/interrupted` y sus archivos `downloading` vuelven a `pending`. El staging UUIDv5 permanece, por lo que la nueva corrida de catch-up puede reanudar los bytes sin presentar la corrida antigua como exitosa.

## D-025 · Mutex global y delegación HTTP local

Windows usa `Global\FileHarvester.Singleton`; desarrollo usa un lock de archivo. Un CLI que no obtiene el mutex no compite por SQLite ni por el servidor: envía la orden a `127.0.0.1` y la instancia propietaria aplica el lock por conexión.

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
