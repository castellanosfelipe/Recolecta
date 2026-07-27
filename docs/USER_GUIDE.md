# Guía de usuario

La guía se ampliará con instalación, alertas y respaldo en las fases 6 a 8.

## Dashboard local

Abra `http://127.0.0.1:8091` mientras FileHarvester está en ejecución. La interfaz no necesita internet:

- **Inicio** resume el estado de cada conexión y permite iniciar una corrida.
- **En vivo** muestra bytes, porcentaje, velocidad media, ETA y cada trabajador; el sondeo es de un segundo mientras hay actividad y de diez segundos en reposo.
- **Historial** filtra corridas y abre el detalle de sus archivos.
- **Archivos** busca por ruta o nombre y exporta el catálogo a CSV.
- **Conexiones** crea, edita, prueba, duplica o elimina orígenes. Una copia nace en pausa y sin credencial.
- **Ajustes** cambia hora, cortesía, concurrencia, retención y catch-up.

El botón **Cancelar corrida** solicita una parada cooperativa. El archivo `.part` se conserva para reanudarlo posteriormente.

Las respuestas del API nunca incluyen la credencial; solo muestran `has_secret`.

## Acceso desde la LAN

Por defecto el servidor solo escucha en `127.0.0.1`. Para permitir acceso desde otros equipos:

```powershell
$env:HARVESTER_BIND_LAN = "1"
$env:HARVESTER_DASH_USER = "operador"
$env:HARVESTER_DASH_PASS = "una-clave-larga"
```

Basic Auth protege dashboard, archivos estáticos y API. `/healthz` queda sin autenticación. FileHarvester escribe una advertencia si se expone a la LAN sin credenciales.

## Modos de instalación previstos

- Modo A: tarea programada por usuario, sin privilegios de administrador, activa después de iniciar sesión.
- Modo B: tarea programada como `SYSTEM`, requiere administrador y permanece activa sin sesión interactiva.

En modo `SYSTEM` no habrá bandeja ni notificaciones de escritorio. El dashboard local seguirá disponible y los secretos deberán cifrarse con DPAPI de máquina.

## Portabilidad de credenciales

- `dpapi:` solo puede descifrarse con la misma cuenta de Windows en el mismo equipo.
- `dpapi-machine:` solo puede descifrarse en el mismo equipo y requiere conservar `data/.entropy`.
- `fernet:` requiere la misma `HARVESTER_SECRET_KEY` o el mismo `data/.secret.key`.

Un respaldo de configuración no exportará estos tokens. Después de mover la instalación a otro equipo, vuelva a ingresar las credenciales.

## Ejecución desatendida y energía

El modo usuario recupera tareas después del login; el modo `SYSTEM` puede ejecutarlas desde el arranque sin sesión. Configure el equipo de descarga para permanecer disponible en corriente alterna:

```powershell
powercfg /change standby-timeout-ac 0
```

Si la política operativa lo permite, un administrador también puede desactivar la hibernación:

```powershell
powercfg /hibernate off
```

APScheduler y el catch-up recuperan ventanas perdidas, pero no pueden descargar mientras Windows está apagado o suspendido.
