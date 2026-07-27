# Instrucciones del repositorio

Lee `docs/SPEC.md` antes de cambiar el producto.

- Ejecuta las fases en orden y no avances si las pruebas de la fase actual fallan.
- Los mensajes de UI y errores son en español. Código, identificadores y docstrings son en inglés.
- No agregues dependencias fuera de `requirements*.txt`; toda dependencia de producción debe contar con su wheel compatible en `wheelhouse/` antes del empaquetado offline.
- No uses CDN, fuentes remotas, telemetría ni comprobaciones de actualización.
- Registra toda decisión no trivial en `docs/DECISIONS.md` con encabezado `D-0NN · Título`.
- Nunca registres ni exportes secretos.
- No incluyas trailers `Co-authored-by` en commits. El hook `commit-msg` lo impide.

Activa los hooks al clonar:

```powershell
git config core.hooksPath .githooks
```
