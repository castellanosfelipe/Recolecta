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
- [x] Reimportar el mismo backup no crea duplicados y todas las conexiones importadas nacen en pausa hasta validarse.
- [x] Cada conexión puede tener una hora diaria distinta o heredar la hora global; scheduler y catch-up respetan la elección.
- [x] Todas las vistas son navegables y los diálogos de conexión y detalle cierran mediante sus controles visibles.
- [x] El editor no permite guardar hasta validar credencial, todas las rutas remotas y escritura en el destino local; cualquier cambio exige repetir la prueba.
- [x] Una corrida canónica `ok` con `files_found=0` se presenta como `no_files` —**Archivos no existentes**— y no como fallida.
- [x] Los resultados `ok` distinguen `no_changes` —**Sin archivos nuevos**— cuando no descargan, y `completed` —**Descarga completada**— cuando descargan al menos un archivo.
- [x] Los estados persistidos continúan limitados a `running|ok|partial|failed|cancelled`; una corrida fallida con cero archivos conserva el fallo y muestra la causa específica de `error_type`.
- [x] El pre-flight aborta con `disk_space` antes de escribir.
- [x] Una ruta remota maliciosa produce `path_invalid` y no escapa de `dest_root`.
- [x] Las credenciales no aparecen en logs, exports ni respuestas de API.
- [x] Los scripts PowerShell son sintácticamente válidos y sus contratos de tarea/desinstalación están comprobados.
- [x] `build.ps1` instala desde `wheelhouse` con `--no-index`, ejecuta la suite y genera el paquete.
- [x] La suite impone cobertura de `app/` ≥85 %; una regresión bloquea el build.
- [x] El `.exe` congelado pasa `--self-test` e incluye recursos web y scripts de instalación.
- [x] El ZIP y el inventario offline tienen manifiestos SHA-256 verificables.
- [x] `acceptance_smoke.ps1` extrae el ZIP, arranca solo el ejecutable congelado y exige `/healthz`, dashboard y JS en menos de 5 s.
- [x] `Recolecta-Setup.exe` contiene el bundle offline y se publica como activo de cada GitHub Release.
- [x] `installer_smoke.ps1` verifica hash, extracción y autodiagnóstico sin registrar tareas en CI.
- [x] Código, interfaz, tareas, exportaciones y artefactos usan exclusivamente la marca `Recolecta`.
- [x] El dashboard enlaza al repositorio de GitHub y al perfil de LinkedIn con iconos locales, etiquetas accesibles y apertura segura.

Evidencia de la compilación local actual: CPython.org 3.12.10 x64, 225 pruebas
aprobadas, cobertura total 85,54 %, autodiagnóstico fuente y congelado
aprobados y bundle `onedir` por debajo de 120 MB.

## Pruebas de aceptación en equipo destino

Estas dos comprobaciones requieren reiniciar o cerrar sesión en un Windows
limpio y por eso no forman parte de la suite:

- [ ] En una VM sin internet ni Python, `install.ps1` registra la tarea y activa el dashboard en menos de 60 segundos.
- [ ] Tras reiniciar Windows, el modo usuario se recupera después del logon y el modo `SYSTEM` antes del logon.

Use una VM Windows 10/11 x64 limpia, pruebe cada modo por separado y adjunte
el historial del Programador de tareas junto con `/healthz` y `logs\app.log`.
