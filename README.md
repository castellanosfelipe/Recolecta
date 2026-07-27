# FileHarvester

Descargador programado y auditable para FTP, FTPS, SFTP, WebDAV(S) y SMB, diseñado para Windows 10/11 x64 sin acceso a internet.

## Estado

Fases 0 a 3 completadas: configuración portable, SQLite WAL, secretos DPAPI/Fernet, planificación y transportes, más un motor concurrente de descargas atómicas con reanudación, SHA-256, control de ancho de banda, reintentos y pre-flight de disco. La programación desatendida comienza en la Fase 4.

## Desarrollo

Requiere Python 3.12.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe launcher.py --self-test
```

La configuración portable se resuelve desde `HARVESTER_DATA_DIR`; en desarrollo, si no se define, usa la raíz del repositorio. El dashboard final escuchará en `http://127.0.0.1:8091`.

En modo `dev`, la clave Fernet se toma de `HARVESTER_SECRET_KEY` o se genera en `data/.secret.key`. En modo `windows` se usa DPAPI de usuario y en modo `service`, DPAPI de máquina con entropía adicional.

El dry-run usa exactamente el mismo cálculo de ventana y filtros que utilizará la descarga. Solo consulta metadatos remotos; nunca abre los archivos para lectura.

Cada transferencia escribe primero en `<dest_root>/.staging/<uuid>.part`. Solo después de validar el tamaño y el hash se publica mediante `os.replace`; una cancelación conserva el parcial para la siguiente ejecución.

## Documentación

- `docs/SPEC.md`: especificación completa y plan de fases.
- `docs/DECISIONS.md`: decisiones de arquitectura.
- `docs/ACCEPTANCE.md`: criterios de aceptación verificables.
- `docs/USER_GUIDE.md`: guía operativa para usuarios.
- `docs/OPERATIONS.md`: runbook para soporte.
