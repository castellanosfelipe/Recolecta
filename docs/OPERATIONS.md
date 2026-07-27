# Operaciones

Este runbook se completará en las fases de scheduler, alertas y empaquetado.

## Diagnóstico inicial

1. Ejecutar `FileHarvester.exe --self-test`.
2. Revisar `logs/app.log`.
3. Confirmar que `http://127.0.0.1:8091/healthz` responde.
4. Verificar espacio libre en el volumen de destino.
5. Validar credenciales desde la acción “Probar conexión”.

Nunca copie una base de datos con secretos DPAPI a otra máquina esperando que puedan descifrarse. Restaure la configuración sin secretos y vuelva a ingresarlos en el equipo destino.
