# Guía de usuario

## Requisitos y preparación

Recolecta funciona en Windows 10/11 x64 sin internet y sin una instalación
previa de Python.

### Instalador recomendado

1. Copie `Recolecta-Setup.exe` y `SHA256SUMS.txt` al equipo.
2. Compare su SHA-256 con el manifiesto:

```powershell
Get-FileHash .\Recolecta-Setup.exe -Algorithm SHA256
```

3. Ejecute `Recolecta-Setup.exe` y confirme la instalación. El Setup instala
   en `%LOCALAPPDATA%\Recolecta`, registra la tarea del usuario, inicia la
   aplicación y abre el dashboard.

En una actualización se conservan `data\`, logs, exports y archivos
descargados. El modo `SYSTEM` continúa disponible mediante
`install-service.ps1` desde el paquete portable y requiere administrador.

### Paquete portable

1. Copie `Recolecta-win64.zip` al equipo y verifique también su SHA-256.
2. Extraiga el ZIP, por ejemplo en `C:\Recolecta`.
3. Si Windows marcó los archivos como descargados de internet:

```powershell
Get-ChildItem C:\Recolecta -Recurse | Unblock-File
```

## Modo A portable: usuario actual, sin administrador

Este modo inicia Recolecta cuando el usuario abre sesión, muestra el icono
de bandeja y permite notificaciones toast.

```powershell
cd C:\Recolecta\Recolecta
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

El script registra la tarea `Recolecta`, la inicia y espera hasta 20
segundos por `http://127.0.0.1:8091/healthz`. La tarea se reinicia después de
un fallo, no duplica instancias, puede iniciar con batería y no tiene límite de
duración.

## Modo B portable: SYSTEM al arrancar

Use este modo para operar sin una sesión iniciada. Abra PowerShell como
administrador:

```powershell
cd C:\Recolecta\Recolecta
Set-ExecutionPolicy -Scope Process Bypass
.\install-service.ps1
```

La tarea `Recolecta-Service` inicia con Windows como `SYSTEM`, nivel
máximo, `WakeToRun` y recuperación automática. El dashboard sigue disponible,
pero no hay bandeja ni toast. Los secretos usan DPAPI de máquina y el archivo
`data\.entropy` tiene ACL restringida.

No instale ambos modos a la vez sobre la misma carpeta. Si cambia de modo,
ejecute primero `.\uninstall.ps1` y luego el instalador elegido.

## Primera conexión, paso a paso

1. Abra `http://127.0.0.1:8091`.
2. Entre en **Conexiones → Nueva conexión**.
3. Asigne un nombre y, opcionalmente, el cliente o área propietaria.
4. Elija FTP, FTPS, SFTP, WebDAV, WebDAVS o SMB.
5. Escriba servidor, puerto, usuario y secreto. Si omite el puerto se usa el
   estándar del protocolo.
6. Indique una ruta remota por línea. Active recursividad solo si necesita
   subcarpetas y fije una profundidad razonable.
7. Elija la zona IANA, por ejemplo `America/Bogota`, y una ventana:
   día calendario anterior, últimas N horas o desde la última corrida correcta.
   Si esa conexión debe ejecutarse a una hora distinta, complete **Hora
   diaria**; si queda vacía, usa la hora global de **Ajustes**.
8. Seleccione un destino local. En producción prefiera una ruta absoluta en
   un volumen con espacio suficiente.
9. Configure filtros, conflicto (`skip`, `overwrite` o `keep_both`),
   verificación por tamaño o SHA-256, paralelismo y límites de ancho de banda.
10. Pulse **Probar conexión y rutas**. Recolecta autentica, comprueba cada
    ruta remota y realiza una escritura temporal en el destino local.
11. Cuando aparezca **Validación correcta**, pulse **Guardar conexión**.
12. Ejecute primero un **dry-run**; después active la conexión y lance una
    corrida real.

El dry-run lista metadatos, usa la misma ventana y filtros de la descarga, y no
abre archivos para lectura. Una corrida real escribe primero en
`<destino>\.staging`; solo publica el archivo final después de validar
integridad.

## Importar conexiones desde StabilityMonitor

1. Entre en **Conexiones** y pulse **Importar JSON**.
2. Seleccione `monitor-backup.json` y confirme la cantidad detectada.
3. Revise el resumen de conexiones importadas, omitidas y con error.
4. Edite cada conexión importada. Si está marcada **Sin secreto**, ingrese la
   credencial; después pulse **Probar conexión y rutas**, guarde y actívela
   solamente cuando la validación sea correcta.

La operación es idempotente para la misma combinación de nombre, protocolo,
host y puerto. Recolecta consume FTP, FTPS, SFTP, WebDAV, WebDAVS y SMB. Las
entradas SQL Server u Oracle se omiten con un motivo visible porque no son
fuentes de archivos soportadas. Un error en una entrada no cancela las demás.
Todas las entradas importadas nacen en pausa; si el backup contiene un secreto,
se cifra al importarlo y no se expone en la interfaz.

## Dashboard, agenda y cancelación

- **Inicio** resume conexiones, fallos y volumen reciente.
- **En vivo** muestra porcentaje, bytes, velocidad y ETA.
- **Historial** filtra corridas y abre el detalle.
- **Archivos** busca por nombre o ruta y exporta CSV.
- **Conexiones** crea, importa, prueba, duplica, pausa o elimina orígenes.
- **Ajustes** controla hora diaria, zona, jitter, catch-up, concurrencia,
  cortesía, reserva de disco, retención y alertas.

### Cómo leer los estados

Una ejecución que logra consultar el origen, pero no encuentra archivos, no es
un fallo. Recolecta conserva el resultado técnico correcto y muestra una
explicación más útil:

| Estado visible | Qué significa |
|---|---|
| **Sin ejecuciones** | La conexión todavía no tiene historial. |
| **En ejecución** | El trabajo continúa activo. |
| **Archivos no existentes** | La consulta terminó correctamente y las rutas configuradas no contenían archivos. |
| **Sin archivos nuevos** | Se encontraron archivos, pero ninguno requería descarga por ventana, filtros o deduplicación. |
| **Descarga completada** | Se descargó al menos un archivo y la corrida terminó sin incidencias. |
| **Completada con incidencias** | La corrida avanzó, pero tuvo advertencias o fallos en parte de los archivos. |
| **Cancelada por el usuario** | Un operador solicitó detener la corrida. |

**Archivos no existentes** describe el resultado de un listado válido. Si
Recolecta no pudo autenticar, conectar o abrir una ruta, muestra la causa real:
**Credencial rechazada**, **Servidor no encontrado**, **Servidor no
disponible**, **Tiempo de conexión agotado**, **Seguridad TLS/SSH no validada**,
**Acceso denegado**, **Ruta remota no existente**, **Espacio local
insuficiente**, **No se pudo escribir en el destino**, **Validación del archivo
fallida**, **Transferencia incompleta**, **Ruta no permitida**, **Ejecución
interrumpida** o **Error de protocolo**. Use el detalle de la corrida para ver
el mensaje técnico saneado.

Si una conexión que normalmente recibe archivos muestra **Archivos no
existentes**, Recolecta puede emitir además una alerta de silencio sospechoso.
La alerta invita a revisar el origen, pero no convierte la corrida válida en
fallida.

**Cancelar corrida** solicita una parada cooperativa. El `.part` queda
disponible para reanudar. APScheduler conserva un job y una hora por conexión;
el catch-up usa esa misma hora y recupera ventanas perdidas después de un
apagado o suspensión.

## Logs, exports y soporte

- `logs\app.log`: ciclo de vida, scheduler y errores generales.
- `logs\runs\<fecha>_<conexion>_<id>.jsonl`: evidencia por corrida, con
  progreso cada 10 %.
- `exports\`: CSV, HTML y bundles ZIP generados.
- `data\recolecta.db`: configuración, agenda e historial.

En **Ajustes → Descargar bundle** se genera un ZIP con logs, CSV, reporte HTML
y configuración pública sin secretos. La retención predeterminada es de 180
días y jamás borra los archivos descargados.

## Acceso desde la LAN

Por defecto solo escucha en `127.0.0.1`. Para permitir acceso remoto defina
antes de registrar la tarea:

```powershell
[Environment]::SetEnvironmentVariable("RECOLECTA_BIND_LAN", "1", "User")
[Environment]::SetEnvironmentVariable("RECOLECTA_DASH_USER", "operador", "User")
[Environment]::SetEnvironmentVariable("RECOLECTA_DASH_PASS", "una-clave-larga", "User")
```

Vuelva a instalar la tarea. Basic Auth protege dashboard y API; `/healthz`
queda público. En modo `SYSTEM`, use variables de entorno de máquina y aplique
la política de firewall de su organización.

## Desinstalación

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\uninstall.ps1
```

El script desregistra las tareas y detiene únicamente el proceso cuyo
ejecutable pertenece a esa carpeta. Conserva el bundle, `data\`, `logs\`,
`exports\` y los destinos descargados. Elimínelos manualmente solo después de
respaldarlos y confirmar que ya no se necesitan.

## Diez problemas comunes

1. **PowerShell bloquea el script.** Ejecute
   `Set-ExecutionPolicy -Scope Process Bypass` en esa ventana; no necesita
   cambiar la política del equipo.
2. **`/healthz` no responde.** Revise `logs\app.log`, confirme que el puerto
   8091 no está ocupado y ejecute `Recolecta.exe --self-test`.
3. **La tarea inicia y se detiene.** Compruebe el historial del Programador de
   tareas, permisos de escritura sobre la carpeta y que no haya otra instancia.
4. **Autenticación rechazada.** Edite la conexión, vuelva a ingresar el
   secreto y pulse **Probar**; el bundle de soporte no contiene credenciales.
5. **FTPS falla por certificado.** Verifique cadena, nombre DNS y reloj del
   equipo. No desactive validación TLS como solución permanente.
6. **SFTP informa host desconocido o cambiado.** Confirme la huella con el
   administrador y actualice `data\known_hosts`; nunca acepte el cambio a
   ciegas.
7. **No aparecen archivos.** Revise zona horaria, ventana, quiet period,
   filtros glob, tamaño mínimo/máximo, ruta remota y el resultado del dry-run.
8. **`disk_space`.** Libere el tamaño planificado más la reserva configurada o
   cambie el destino. El motor no crea staging si el pre-flight falla.
9. **Queda un `.part`.** Es normal tras cancelación o caída de red. No lo
   renombre; la siguiente corrida de la misma identidad intentará reanudarlo.
10. **La conexión se movió a otro equipo y el secreto ya no abre.** DPAPI está
    ligado al equipo y, en modo usuario, también a la cuenta. Reingrese todas
    las credenciales en el nuevo host.
