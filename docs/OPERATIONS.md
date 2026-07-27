# Operaciones

## Comprobación rápida de un fallo

Ejecute en este orden:

1. `Recolecta.exe --self-test`.
2. `Invoke-WebRequest http://127.0.0.1:8091/healthz -UseBasicParsing`.
3. `Get-ScheduledTask Recolecta* | Get-ScheduledTaskInfo`.
4. Revise `logs\app.log` y el JSONL de la corrida en `logs\runs\`.
5. Confirme espacio libre y permisos de escritura del destino.
6. Use **Probar conexión** y después una corrida `--dry-run`.
7. Revise hora, zona IANA, filtros, quiet period y ventana UTC persistida.

El código de salida de `--run-now` es distinto de cero si la instancia
residente no responde o la ejecución directa falla.

## Estado y recuperación

Una corrida que estaba `running` al arrancar pasa a `failed/interrupted`. Sus
archivos `downloading` vuelven a `pending`; el staging UUIDv5 se conserva y el
catch-up reintenta la ventana. Si ya existe una corrida `ok` con los mismos
límites UTC, no se crea otra.

La cancelación es cooperativa y puede esperar al bloque de red actual. No
renombre ni publique manualmente los `.part`. Si falta espacio, el motor
requiere el volumen planificado más la reserva —10 % por defecto— antes de
abrir conexiones.

## Forzar una descarga controlada

Recolecta evita por diseño volver a bajar una identidad que ya terminó
correctamente. Para una excepción auditable:

1. En **Conexiones**, duplique la conexión original.
2. Edite la copia, vuelva a ingresar el secreto y seleccione `keep_both` o
   `overwrite` según la política autorizada.
3. Mantenga la copia pausada mientras ejecuta **Probar** y un dry-run.
4. Active y ejecute solo la copia.
5. Verifique integridad y desactive o elimine la copia.

La copia tiene otro identificador y, por tanto, un historial independiente.
No borre filas directamente de SQLite para saltarse deduplicación.

## Rotar credenciales

1. Genere la nueva credencial en el sistema remoto.
2. Edite la conexión y escriba el nuevo secreto o ruta de clave.
3. Pulse **Probar** antes de revocar la credencial anterior.
4. Ejecute un dry-run y luego una corrida real.
5. Revoque la credencial antigua y descargue un bundle de soporte si necesita
   evidencia del cambio.

El secreto se vuelve a cifrar con el proveedor del modo actual; nunca aparece
en API, logs o exports. Rote por separado las variables de SMTP, webhook y
Basic Auth, reiniciando después la tarea programada.

## Mover una instalación

### Misma máquina y misma cuenta/modo

1. Ejecute `.\uninstall.ps1`.
2. Copie la carpeta completa, incluidos `data\`, `logs\` y `exports\`.
3. Conserve `data\.entropy` si usa modo `SYSTEM`.
4. Registre otra vez con el instalador del mismo modo.
5. Pruebe todas las conexiones.

### Otra cuenta o máquina

DPAPI de usuario requiere la misma cuenta en el mismo equipo.
`dpapi-machine` requiere el mismo equipo y `data\.entropy`. Copiar la base a
otro host no vuelve portables los tokens. En el destino, reingrese cada
credencial y pruebe las conexiones. Para desarrollo con Fernet, conserve
`RECOLECTA_SECRET_KEY` o `data\.secret.key` por un canal seguro.

## Logs y bundle de soporte

Descargue el bundle desde Ajustes o con
`GET /api/export/bundle.zip?days=7`. Incluye `app.log`, JSONL, CSV, reporte
HTML y configuración pública. Antes de enviarlo, confirme que el rango cubre
la falla y no agregue archivos de credenciales.

El progreso activo vive en memoria; SQLite recibe checkpoints como máximo una
vez por segundo y el resultado terminal siempre se persiste. Si la UI se
detiene pero `/healthz` responde, recargue la vista y revise Historial antes
de reiniciar.

## Alertas

En **Ajustes → Registro de alertas**, `failed` conserva el error del canal:

- SMTP usa `RECOLECTA_SMTP_USER` y `RECOLECTA_SMTP_PASSWORD`.
- Webhook usa `RECOLECTA_ALERT_WEBHOOK_URL`.
- Toast requiere sesión interactiva.
- Event Log corresponde al modo `SYSTEM`.

La clave única `(run_id, cause, channel)` evita duplicados después de reinicios.

## Retención y respaldo

`audit-retention` corre a las 03:30 UTC. Purga historial relacionado,
`logs\runs\*.jsonl` y exports por antigüedad. No recorre destinos ni elimina
archivos descargados. Antes de reducir la retención, exporte un bundle.

Respalde juntos `data\`, `logs\` y `exports\`; incluya los destinos según su
política. No copie `data\recolecta.db` mientras el proceso escribe: detenga la
tarea o use una herramienta de backup compatible con SQLite.

## Paquete y actualización

Verifique `Recolecta-win64.zip` contra `SHA256SUMS.txt`. Para actualizar:

1. respalde `data\`, `logs\` y `exports\`;
2. desinstale la tarea;
3. extraiga la nueva versión en una carpeta nueva;
4. copie el estado respaldado;
5. instale en el mismo modo y ejecute `--self-test`;
6. conserve la versión anterior hasta completar una corrida correcta.

`build.ps1` valida los hashes de `wheelhouse\` y `vendor\`, instala con
`--no-index`, exige pruebas verdes, congela y exige el autodiagnóstico del
ejecutable antes de crear el ZIP.
