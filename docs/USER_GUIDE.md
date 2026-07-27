# Guía de usuario

## Requisitos y preparación

Recolecta funciona en Windows 10/11 x64 sin internet y sin una instalación
previa de Python. Elija una carpeta permanente donde la cuenta que ejecutará
la aplicación pueda escribir. No use una carpeta temporal ni mueva archivos
individuales fuera del bundle.

1. Copie `Recolecta-win64.zip` al equipo.
2. Compare su SHA-256 con `SHA256SUMS.txt`:

```powershell
Get-FileHash .\Recolecta-win64.zip -Algorithm SHA256
```

3. Extraiga el ZIP, por ejemplo en `C:\Recolecta`.
4. Si Windows marcó los archivos como descargados de internet:

```powershell
Get-ChildItem C:\Recolecta -Recurse | Unblock-File
```

## Modo A: usuario actual, sin administrador

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

## Modo B: SYSTEM al arrancar

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
8. Seleccione un destino local. En producción prefiera una ruta absoluta en
   un volumen con espacio suficiente.
9. Configure filtros, conflicto (`skip`, `overwrite` o `keep_both`),
   verificación por tamaño o SHA-256, paralelismo y límites de ancho de banda.
10. Guarde, pulse **Probar** y revise cuántos archivos entrarían en el plan.
11. Ejecute primero un **dry-run**; después active la conexión y lance una
    corrida real.

El dry-run lista metadatos, usa la misma ventana y filtros de la descarga, y no
abre archivos para lectura. Una corrida real escribe primero en
`<destino>\.staging`; solo publica el archivo final después de validar
integridad.

## Dashboard, agenda y cancelación

- **Inicio** resume conexiones, fallos y volumen reciente.
- **En vivo** muestra porcentaje, bytes, velocidad y ETA.
- **Historial** filtra corridas y abre el detalle.
- **Archivos** busca por nombre o ruta y exporta CSV.
- **Conexiones** crea, prueba, duplica, pausa o elimina orígenes.
- **Ajustes** controla hora diaria, zona, jitter, catch-up, concurrencia,
  cortesía, reserva de disco, retención y alertas.

**Cancelar corrida** solicita una parada cooperativa. El `.part` queda
disponible para reanudar. APScheduler conserva un job por conexión y el
catch-up recupera ventanas perdidas después de un apagado o suspensión.

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
