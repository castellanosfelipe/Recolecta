# Operaciones

Este runbook se completará con los procedimientos de instalación y empaquetado de la Fase 7.

## Diagnóstico inicial

1. Ejecutar `FileHarvester.exe --self-test`.
2. Revisar `logs/app.log`.
3. Confirmar que `http://127.0.0.1:8091/healthz` responde.
4. Verificar espacio libre en el volumen de destino.
5. Validar credenciales desde la acción “Probar conexión”.

Nunca copie una base de datos con secretos DPAPI a otra máquina esperando que puedan descifrarse. Restaure la configuración sin secretos y vuelva a ingresarlos en el equipo destino.

## Advertencia `timestamp_unreliable`

Indica que un FTP antiguo no soportó `MDTM` ni `MLSD` y fue necesario interpretar `LIST`. La corrida puede continuar como parcial, pero la zona y la precisión del timestamp no están garantizadas. Solicite habilitar RFC 3659 en el servidor o use `since_last_run` con solape suficiente.

## Archivos `.part`

Una cancelación o pérdida de red conserva archivos en `<dest_root>/.staging`. No los renombre ni los copie como archivos terminados. La siguiente corrida de la misma identidad los reanuda; la limpieza de huérfanos solo elimina nombres que no estén referenciados por trabajos pendientes recuperados.

Si aparece `disk_space`, libere al menos el tamaño planificado más 10 % de reserva. El motor aborta antes de abrir conexiones o crear staging.

## Recuperación al arrancar

Una corrida que estaba `running` pasa a `failed` con causa `interrupted`. Sus archivos vuelven a `pending` y el catch-up intenta la ventana pendiente después de `startup_delay_s`. Si ya existe una corrida `ok` para esos mismos límites UTC, no se crea otra.

Para diagnóstico manual:

```powershell
FileHarvester.exe --self-test
FileHarvester.exe --run-now --connection 3 --dry-run
FileHarvester.exe --run-now --connection 3
```

El código de salida es distinto de cero cuando la instancia residente no responde o la ejecución directa falla.

## Progreso y cancelación

`GET /api/progress` mantiene el detalle activo en memoria para no castigar SQLite. `bytes_done` se persiste como máximo una vez por segundo por archivo y el resultado terminal siempre se escribe completo. Si el dashboard deja de actualizar:

1. comprobar `/healthz`;
2. recargar la vista sin cerrar el proceso residente;
3. revisar `logs/app.log`;
4. confirmar en Historial si la corrida ya terminó.

La cancelación es cooperativa: puede tardar hasta que el bloque de red actual regrese. No elimine el `.part`.

## Bundle de soporte

Descárguelo desde Ajustes o desde `GET /api/export/bundle.zip?days=7`. Antes de adjuntarlo:

1. confirme que el rango incluye la falla;
2. conserve el ZIP sin modificar para mantener coherencia entre CSV y JSONL;
3. no agregue manualmente archivos de credenciales.

La generación excluye claves de ajustes sensibles y los modelos públicos nunca contienen el token cifrado. Si se detecta un posible secreto en un mensaje, el filtro sustituye su valor por `***`.

## Alertas que no llegan

Revise **Ajustes → Registro de alertas**. El estado `failed` conserva la causa del canal sin reintentar indefinidamente la misma alerta.

- SMTP: valide host, puerto, remitente y destinatarios; usuario/clave provienen de `HARVESTER_SMTP_USER` y `HARVESTER_SMTP_PASSWORD`.
- Webhook: confirme `alerts.webhook.enabled` y `HARVESTER_ALERT_WEBHOOK_URL`.
- Toast: requiere Windows con sesión interactiva.
- Event Log: requiere modo `service` y `pywin32`.

La restricción única `(run_id, cause, channel)` evita duplicados incluso tras reiniciar.

## Retención

La tarea `audit-retention` corre diariamente a las 03:30 UTC. También puede ejecutarse manualmente desde Ajustes. Antes de reducir el periodo, exporte un bundle si necesita conservar evidencia.

La purga sólo toca `runs`, sus filas relacionadas, `logs/runs/*.jsonl` y archivos antiguos de `exports/`. No recorre ni elimina `downloads/`.
