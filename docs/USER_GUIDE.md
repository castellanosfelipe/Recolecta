# Guía de usuario

La guía completa se desarrollará junto con las funciones operativas de las fases 5 a 7.

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
