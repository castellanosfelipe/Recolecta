# FileHarvester

Descargador programado y auditable para FTP, FTPS, SFTP, WebDAV(S) y SMB, diseñado para Windows 10/11 x64 sin acceso a internet.

## Estado

Fase 0 completada: estructura base, configuración portable, logging seguro, taxonomía de errores, CI y reglas de contribución. Las fases funcionales se implementan de forma incremental según `docs/SPEC.md`.

## Desarrollo

Requiere Python 3.12.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe launcher.py --self-test
```

La configuración portable se resuelve desde `HARVESTER_DATA_DIR`; en desarrollo, si no se define, usa la raíz del repositorio. El dashboard final escuchará en `http://127.0.0.1:8091`.

## Documentación

- `docs/SPEC.md`: especificación completa y plan de fases.
- `docs/DECISIONS.md`: decisiones de arquitectura.
- `docs/ACCEPTANCE.md`: criterios de aceptación verificables.
- `docs/USER_GUIDE.md`: guía operativa para usuarios.
- `docs/OPERATIONS.md`: runbook para soporte.
