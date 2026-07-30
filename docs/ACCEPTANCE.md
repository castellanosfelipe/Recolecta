# Criterios de aceptación

## Verificación automatizada

- [x] La agenda descarga exclusivamente los archivos de la ventana configurada.
- [x] El catch-up encuentra y ejecuta ventanas perdidas sin duplicar una ventana correcta.
- [x] Un corte no deja archivos definitivos incompletos y permite reanudar el `.part`.
- [x] Repetir una corrida marca identidades exitosas como `duplicate`.
- [x] `{remote_tree}` conserva la jerarquía del remoto y dos archivos homónimos en carpetas distintas llegan a destinos diferentes.
- [x] Cada conexión permite activar **Comparación completa con carpeta local** desde sus acciones y el editor; el valor queda persistido.
- [x] La comparación completa ignora ventana e historial, descarga ausentes o diferentes, omite equivalentes y conserva filtros, quiet period y la exclusión de symlinks.
- [x] La comparación completa es unidireccional remoto → local y no borra ni modifica archivos locales extra.
- [x] Descubrimiento, persistencia y descarga usan lotes acotados; una cola grande no crea una lista o un futuro en memoria por cada documento.
- [x] Dry-run, resultado y detalle limitan la muestra visible a 500 elementos e informan los totales y el truncamiento.
- [x] Un archivo con contenido binario no UTF-8 conserva exactamente sus bytes; ninguna etapa lo decodifica o recodifica.
- [x] WebDAV consume bytes HTTP crudos y solo reanuda con un `Content-Range` exacto; una respuesta ambigua no modifica el parcial.
- [x] Un archivo sin tamaño reserva 64 MiB por worker activo y comprueba espacio durante el stream, sin multiplicar la reserva por todo el inventario.
- [x] Un parcial solo se reutiliza con tamaño y timestamp remoto confiables; tamaño por sí solo no mezcla versiones.
- [x] Una transferencia corta se reintenta como `partial_transfer`, una sobredimensionada falla por `integrity` y ninguna se publica incompleta.
- [x] El `mtime` se aplica al staging antes de la publicación atómica; si falla, queda el `.part` y no un archivo final engañoso.
- [x] La cancelación interrumpe descubrimiento, cupos, spacing, token bucket y backoff, sin reclamar un lote adicional.
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
- [x] Una ruta remota inválida falla de forma aislada y no impide descargar las entradas válidas del mismo listado.
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

Evidencia previa a la compilación del release v0.2.0: CPython.org 3.12.10 x64,
308 pruebas aprobadas, una omisión esperada por permisos de symlink y cobertura
total 86,23 %. El autodiagnóstico congelado y el tamaño del bundle se verifican
nuevamente en `build.ps1` antes de publicar.

## Pruebas de aceptación en equipo destino

Estas dos comprobaciones requieren reiniciar o cerrar sesión en un Windows
limpio y por eso no forman parte de la suite:

- [ ] En una VM sin internet ni Python, `install.ps1` registra la tarea y activa el dashboard en menos de 60 segundos.
- [ ] Tras reiniciar Windows, el modo usuario se recupera después del logon y el modo `SYSTEM` antes del logon.

Use una VM Windows 10/11 x64 limpia, pruebe cada modo por separado y adjunte
el historial del Programador de tareas junto con `/healthz` y `logs\app.log`.
