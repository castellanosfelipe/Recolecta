# Criterios de aceptación

## Verificación automatizada

- [x] La agenda descarga exclusivamente los archivos de la ventana configurada.
- [x] El catch-up encuentra y ejecuta ventanas perdidas sin duplicar una ventana correcta.
- [x] Un corte no deja archivos definitivos incompletos y permite reanudar el `.part`.
- [x] Repetir una corrida marca identidades exitosas como `duplicate`.
- [x] El quiet period omite un archivo en escritura y permite descargarlo después.
- [x] El progreso expone bytes, porcentaje, velocidad y ETA con polling activo de un segundo.
- [x] El bundle de soporte contiene logs, CSV y configuración sin credenciales.
- [x] La importación de StabilityMonitor crea conexiones de archivos e informa fuentes omitidas.
- [x] El pre-flight aborta con `disk_space` antes de escribir.
- [x] Una ruta remota maliciosa produce `path_invalid` y no escapa de `dest_root`.
- [x] Las credenciales no aparecen en logs, exports ni respuestas de API.
- [x] Los scripts PowerShell son sintácticamente válidos y sus contratos de tarea/desinstalación están comprobados.
- [x] `build.ps1` instala desde `wheelhouse` con `--no-index`, ejecuta la suite y genera el paquete.
- [x] La suite impone cobertura de `app/` ≥85 %; una regresión bloquea el build.
- [x] El `.exe` congelado pasa `--self-test` e incluye recursos web y scripts de instalación.
- [x] El ZIP y el inventario offline tienen manifiestos SHA-256 verificables.
- [x] `acceptance_smoke.ps1` extrae el ZIP, arranca solo el ejecutable congelado y exige `/healthz`, dashboard y JS en menos de 5 s.
- [x] Código, interfaz, tareas, exportaciones y artefactos usan exclusivamente la marca `Recolecta`.
- [x] El dashboard enlaza al repositorio de GitHub y al perfil de LinkedIn con iconos locales, etiquetas accesibles y apertura segura.

Evidencia de la compilación local actual: CPython.org 3.12.10 x64, 172 pruebas
aprobadas, cobertura total 85,26 %, autodiagnóstico fuente y congelado
aprobados y bundle `onedir` por debajo de 120 MB.

## Pruebas de aceptación en equipo destino

Estas dos comprobaciones requieren reiniciar o cerrar sesión en un Windows
limpio y por eso no forman parte de la suite:

- [ ] En una VM sin internet ni Python, `install.ps1` registra la tarea y activa el dashboard en menos de 60 segundos.
- [ ] Tras reiniciar Windows, el modo usuario se recupera después del logon y el modo `SYSTEM` antes del logon.

Use una VM Windows 10/11 x64 limpia, pruebe cada modo por separado y adjunte
el historial del Programador de tareas junto con `/healthz` y `logs\app.log`.
