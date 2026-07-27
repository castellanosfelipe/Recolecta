# Guía de usuario

La guía completa se desarrollará junto con las funciones operativas de las fases 5 a 7.

## Modos de instalación previstos

- Modo A: tarea programada por usuario, sin privilegios de administrador, activa después de iniciar sesión.
- Modo B: tarea programada como `SYSTEM`, requiere administrador y permanece activa sin sesión interactiva.

En modo `SYSTEM` no habrá bandeja ni notificaciones de escritorio. El dashboard local seguirá disponible y los secretos deberán cifrarse con DPAPI de máquina.
