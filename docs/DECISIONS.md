# Registro de decisiones

## D-001 · Estado portable bajo una única raíz

Toda ruta mutable se deriva de `HARVESTER_DATA_DIR`, del directorio del ejecutable congelado o de la raíz del repositorio en desarrollo. Así el paquete no depende del directorio de trabajo y puede moverse como una unidad.

## D-002 · Configuración de proceso por variables de entorno validadas

El puerto, la exposición a LAN, las credenciales del dashboard y el modo de ejecución se validan al arrancar. Una configuración incompleta falla con un mensaje accionable antes de iniciar trabajos.

## D-003 · Taxonomía de errores estable

Las excepciones de librerías y del sistema se convierten a identificadores persistentes definidos en `ErrorType`. La lógica de reintento consume esos identificadores y no textos variables de excepciones.

## D-004 · Redacción en el límite del sistema de logging

El filtro se instala en el handler rotativo y procesa el mensaje ya interpolado. De este modo cubre llamadas con argumentos y evita que credenciales nombradas, URLs autenticadas o tokens cifrados lleguen al archivo.

## D-005 · Fases verificadas por pruebas

Cada fase termina con pruebas verdes. El pipeline de Windows ejecuta `pytest` y el autodiagnóstico antes de considerar válido el andamiaje.
