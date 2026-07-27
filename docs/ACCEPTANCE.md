# Criterios de aceptación

- [ ] En Windows sin internet ni Python, `install.ps1` activa el dashboard en menos de 60 segundos.
- [ ] La aplicación vuelve a estar activa tras reiniciar Windows en modo usuario y en modo servicio.
- [ ] La agenda descarga exclusivamente los archivos de la ventana configurada.
- [ ] El catch-up ejecuta una corrida perdida tras seis horas con el equipo apagado.
- [ ] Un corte a mitad de descarga no deja archivos definitivos incompletos y permite reanudar.
- [ ] Repetir una corrida marca todos los archivos ya exitosos como `duplicate`.
- [ ] El quiet period omite un archivo en escritura y permite descargarlo después.
- [ ] El dashboard actualiza bytes, porcentaje, velocidad y ETA al menos cada segundo.
- [ ] El bundle de soporte contiene logs, CSV y configuración sin credenciales.
- [ ] La importación de StabilityMonitor crea conexiones de archivos e informa las de base de datos omitidas.
- [ ] El pre-flight aborta con `disk_space` antes de escribir cuando falta espacio.
- [ ] Una ruta remota maliciosa produce `path_invalid` y no escapa de `dest_root`.
- [ ] Ninguna credencial aparece en logs, exports ni respuestas de la API.
- [ ] `pytest` pasa y `build.ps1` genera el paquete sin acceso a internet.
