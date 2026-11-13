# Sistema Matriplast

Sistema web interno para ventas, inventarios, producción y análisis de Matriplast.

## Alcance acordado

- Usuarios internos con permisos por función.
- Catálogo de productos terminados y materias primas, con unidad de medida configurable.
- Ventas con varios productos, cliente, registro de pagos parciales en efectivo, transferencia o QR y saldo pendiente.
- Comprobante interno en PDF; no es factura fiscal ni reemplaza la autorización del SIN.
- Movimientos de inventario, compras y gastos, con ajustes manuales de entrada y salida que validan las existencias y conservan el motivo.
- Lotes con producto, cantidad planificada y producida, operario y horas de inicio y fin.
- Pronóstico mensual de ventas por producto cuando exista historial suficiente.
- Logs de auditoría para consultar usuario, acción, registro afectado y fecha, incluidos inicios de sesión, cierres de sesión e intentos fallidos sin guardar contraseñas.
- Visión artificial aplazada. Máquinas y diagnósticos están pendientes de confirmar.

## Preparación local

1. Instala Python 3.14 o una versión compatible con las dependencias.
2. Crea un entorno virtual: `python -m venv .venv`.
3. Activa el entorno virtual e instala dependencias: `pip install -r requirements.txt`.
4. Copia `.env.example` a `.env` y reemplaza `DB_PASSWORD` por la contraseña local del usuario `postgres`. No compartas ese archivo.
5. Ejecuta `python manage.py makemigrations core` y `python manage.py migrate` para crear las tablas en PostgreSQL.
6. Crea los grupos de acceso con `python manage.py setup_roles`.
7. Crea el usuario administrador con `python manage.py createsuperuser`.
8. Inicia el servidor con `python manage.py runserver` y abre `/`.

## Respaldo de PostgreSQL en Windows

- Con el servidor de PostgreSQL iniciado y `.env` configurado, ejecuta `powershell -ExecutionPolicy Bypass -File .\backup_postgres.ps1` desde la carpeta del proyecto.
- Sin `DRIVE_BACKUP_DIRECTORY`, el respaldo manual se guarda en `backups/`. Para guardarlo en Google Drive, instala e inicia sesión en **Google Drive para escritorio**, crea una carpeta dentro de **Mi unidad** y configura su ruta local en `DRIVE_BACKUP_DIRECTORY` dentro de `.env` (por ejemplo `G:\My Drive\MATRIPLAST Backups`; usa la ruta que realmente aparezca en tu equipo).
- Para programar el respaldo diario a las 20:00, con Drive para escritorio iniciado y la carpeta configurada, ejecuta una vez `powershell -ExecutionPolicy Bypass -File .\configurar_backup_diario.ps1`. La tarea de Windows corre cuando tu usuario ha iniciado sesión y Windows vuelve a ejecutarla si el equipo estaba apagado a la hora programada.
- Cada copia usa formato personalizado de PostgreSQL, comprueba que el archivo no esté vacío y que `pg_restore` pueda leerlo. El archivo `backup.log` en la carpeta de destino registra éxitos y errores. La carpeta `backups/` local está excluida de Git para no subir copias por accidente.
- Para restaurar, crea primero una base de datos vacía con otro nombre en pgAdmin. Haz clic derecho sobre ella, elige **Restore**, selecciona el archivo `.dump` y usa el formato **Custom or tar**. No restaures sobre la base que estás usando; la copia contiene los datos existentes al momento del respaldo.

## Estado

La interfaz incluye administración de usuarios y roles, ventas, clientes, inventario, producción, reportes y consulta de logs. El comprobante interno se puede imprimir o guardar como PDF desde el navegador; no es una factura fiscal. El pronóstico compara Random Forest con una media móvil, evalúa el último trimestre con MAE, RMSE y MAPE, genera tres meses futuros solo cuando el producto tiene 12 meses completos de historial y contrasta proyecciones pasadas con las ventas registradas cuando hay datos.
