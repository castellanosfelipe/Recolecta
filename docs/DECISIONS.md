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
