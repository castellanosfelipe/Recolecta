# Operaciones

Este runbook se completará en las fases de scheduler, alertas y empaquetado.

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
