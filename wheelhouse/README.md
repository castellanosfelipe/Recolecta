# Wheelhouse

Wheels de producción, pruebas y empaquetado para CPython 3.12 sobre Windows x64. `build.ps1` instala exclusivamente desde esta carpeta con `pip --no-index --find-links`.

`SHA256SUMS.txt` permite verificar el inventario antes de compilar. Para regenerarlo se requiere una estación conectada y después debe comprobarse el build desconectado.

El soporte SMB2/SMB3 se conserva como una cadena offline fijada y completa:
`smbprotocol==1.17.0` depende de `pyspnego==0.12.1`, que en Windows usa
`sspilib==0.5.0`. Los tres wheels deben permanecer inventariados en
`SHA256SUMS.txt` y compatibles con CPython 3.12 x64 (`sspilib` usa ABI3).
