# FileHarvester

Descargador programado y auditable para FTP, FTPS, SFTP, WebDAV(S) y SMB, diseñado para Windows 10/11 x64 sin acceso a internet.

## Estado

Fases 0 a 6 completadas: configuración y persistencia, transportes, motor atómico reanudable, agenda y catch-up, dashboard local, logs JSONL, exports CSV/HTML/ZIP, retención, alertas y bandeja interactiva.

## Desarrollo

Requiere Python 3.12.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe launcher.py --self-test
```

La configuración portable se resuelve desde `HARVESTER_DATA_DIR`; en desarrollo, si no se define, usa la raíz del repositorio. El dashboard escucha en `http://127.0.0.1:8091`. Incluye Inicio, corridas en vivo, historial, archivos, conexiones y ajustes; no carga recursos desde internet.

En modo `dev`, la clave Fernet se toma de `HARVESTER_SECRET_KEY` o se genera en `data/.secret.key`. En modo `windows` se usa DPAPI de usuario y en modo `service`, DPAPI de máquina con entropía adicional.

El dry-run usa exactamente el mismo cálculo de ventana y filtros que utilizará la descarga. Solo consulta metadatos remotos; nunca abre los archivos para lectura.

Cada transferencia escribe primero en `<dest_root>/.staging/<uuid>.part`. Solo después de validar el tamaño y el hash se publica mediante `os.replace`; una cancelación conserva el parcial para la siguiente ejecución.

```powershell
FileHarvester.exe --run-now
FileHarvester.exe --run-now --connection 3 --dry-run
FileHarvester.exe --run-now --connection 3 --date 2026-07-26
```

Si la instancia residente está activa, el CLI delega la orden a su API local en vez de abrir una segunda corrida.

Para exponer el dashboard a la LAN, defina `HARVESTER_BIND_LAN=1`. Se recomienda configurar a la vez `HARVESTER_DASH_USER` y `HARVESTER_DASH_PASS`; `/healthz` permanece público para monitoreo.

Desde Ajustes se puede descargar un bundle de soporte con `app.log`, logs JSONL, CSV, reporte HTML y configuración sin secretos. La retención predeterminada es de 180 días y nunca elimina los archivos descargados.

## Documentación

- `docs/SPEC.md`: especificación completa y plan de fases.
- `docs/DECISIONS.md`: decisiones de arquitectura.
- `docs/ACCEPTANCE.md`: criterios de aceptación verificables.
- `docs/USER_GUIDE.md`: guía operativa para usuarios.
- `docs/OPERATIONS.md`: runbook para soporte.
