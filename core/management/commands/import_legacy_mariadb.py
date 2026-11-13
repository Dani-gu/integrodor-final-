"""Safely extract selected business records from an old MariaDB phpMyAdmin dump.

This command parses INSERT statements for an explicit allowlist. It never runs
the SQL file or imports users, roles, passwords, machines, diagnostics, or invoices.
"""

import hashlib
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import (
    AuditLog,
    Client,
    CustomerOrder,
    CustomerOrderItem,
    HistoricalSale,
    InventoryMovement,
    Product,
    SalesHistoryImport,
)


ALLOWED_TABLES = {
    "productos_terminados",
    "materia_prima",
    "clientes",
    "pedidos",
    "detalles_pedido",
    "maquinas",
}
DELIVERED_STATUSES = {"entregado", "delivered", "completado", "completed", "finalizado", "paid", "pagado"}
PENDING_STATUSES = {"pendiente", "pending"}


def _unescape_mysql_string(value):
    value = value[1:-1]
    replacements = {"0": "\0", "b": "\b", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a", "\\": "\\", "'": "'", '"': '"', "%": "%", "_": "_"}
    result = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            index += 1
            result.append(replacements.get(value[index], value[index]))
        elif value[index] == "'" and index + 1 < len(value) and value[index + 1] == "'":
            result.append("'")
            index += 1
        else:
            result.append(value[index])
        index += 1
    return "".join(result)


def _literal(raw):
    raw = raw.strip()
    if raw.upper() == "NULL":
        return None
    if raw.startswith("'") and raw.endswith("'"):
        return _unescape_mysql_string(raw)
    try:
        return Decimal(raw)
    except InvalidOperation:
        return raw


def _parse_value_rows(values_text):
    rows = []
    index = 0
    while index < len(values_text):
        while index < len(values_text) and (values_text[index].isspace() or values_text[index] == ","):
            index += 1
        if index >= len(values_text):
            break
        if values_text[index] != "(":
            raise ValueError("Formato de VALUES no reconocido")
        index += 1
        fields = []
        field_start = index
        quoted = False
        escaped = False
        while index < len(values_text):
            char = values_text[index]
            if escaped:
                escaped = False
            elif char == "\\" and quoted:
                escaped = True
            elif char == "'":
                if quoted and index + 1 < len(values_text) and values_text[index + 1] == "'":
                    index += 1
                else:
                    quoted = not quoted
            elif not quoted and char == ",":
                fields.append(_literal(values_text[field_start:index]))
                field_start = index + 1
            elif not quoted and char == ")":
                fields.append(_literal(values_text[field_start:index]))
                index += 1
                break
            index += 1
        if quoted:
            raise ValueError("Cadena sin cerrar en VALUES")
        rows.append(fields)
    return rows


def _insert_statements(sql_text):
    header = re.compile(
        r"INSERT INTO\s+`(?P<table>[^`]+)`\s*\((?P<columns>.*?)\)\s*VALUES\s*",
        re.IGNORECASE | re.DOTALL,
    )
    for match in header.finditer(sql_text):
        table = match.group("table")
        if table not in ALLOWED_TABLES:
            continue
        columns = re.findall(r"`([^`]+)`", match.group("columns"))
        index = match.end()
        quoted = False
        escaped = False
        while index < len(sql_text):
            char = sql_text[index]
            if escaped:
                escaped = False
            elif char == "\\" and quoted:
                escaped = True
            elif char == "'":
                if quoted and index + 1 < len(sql_text) and sql_text[index + 1] == "'":
                    index += 1
                else:
                    quoted = not quoted
            elif char == ";" and not quoted:
                break
            index += 1
        if index >= len(sql_text):
            raise ValueError(f"INSERT sin cierre para la tabla {table}")
        parsed = _parse_value_rows(sql_text[match.end():index])
        yield table, [dict(zip(columns, row)) for row in parsed]


def _integer(value, field_name):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CommandError(f"Valor inválido en el campo {field_name}.") from exc


def _decimal(value, field_name, default="0"):
    if value is None or value == "":
        value = default
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise CommandError(f"Valor decimal inválido en el campo {field_name}.") from exc
    if not result.is_finite():
        raise CommandError(f"Valor decimal no finito en el campo {field_name}.")
    return result


def _legacy_date(value):
    if not value:
        raise CommandError("Un pedido entregado no tiene fecha.")
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise CommandError(f"Fecha de pedido inválida: {value}.") from exc


class Command(BaseCommand):
    help = "Importa productos, materias primas, clientes y pedidos históricos desde un SQL de MariaDB."

    def add_arguments(self, parser):
        parser.add_argument("sql_file", help="Ruta al archivo SQL exportado desde MariaDB/phpMyAdmin.")

    @transaction.atomic
    def handle(self, *args, **options):
        path = Path(options["sql_file"])
        if not path.is_file():
            raise CommandError("No se encontró el archivo SQL indicado.")
        if path.stat().st_size > 100 * 1024 * 1024:
            raise CommandError("El archivo supera el límite de 100 MB.")
        raw = path.read_bytes()
        file_hash = hashlib.sha256(raw).hexdigest()
        if SalesHistoryImport.objects.filter(file_sha256=file_hash).exists():
            raise CommandError("Este archivo ya fue importado anteriormente.")
        try:
            sql_text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CommandError("El archivo debe estar codificado como UTF-8.") from exc

        source = {table: [] for table in ALLOWED_TABLES}
        try:
            for table, rows in _insert_statements(sql_text):
                source[table].extend(rows)
        except ValueError as exc:
            raise CommandError(f"No se pudo interpretar el volcado: {exc}.") from exc
        for table in ("productos_terminados", "materia_prima", "clientes", "pedidos", "detalles_pedido"):
            if not source[table]:
                raise CommandError(f"No se encontraron registros en la tabla {table}.")

        actor = get_user_model().objects.filter(is_active=True).order_by("-is_superuser", "id").first()
        if actor is None:
            raise CommandError("Primero crea un usuario activo en el sistema nuevo.")
        if AuditLog.objects.filter(entity="LegacyMatriplastImport", entity_id=file_hash[:32]).exists():
            raise CommandError("La importación de esta base ya fue registrada.")

        finished_products = {}
        raw_materials = {}
        client_map = {}
        new_product_count = 0
        new_material_count = 0
        new_client_count = 0

        for row in source["productos_terminados"]:
            old_id = _integer(row.get("id"), "productos_terminados.id")
            name = str(row.get("nombre") or "").strip()
            if not name or len(name) > 160:
                raise CommandError(f"Nombre inválido para el producto anterior #{old_id}.")
            code = f"OLD-PT-{old_id:04d}"
            if Product.objects.filter(code=code).exists():
                raise CommandError(f"El código {code} ya existe; se canceló la importación sin cambios.")
            stock = _decimal(row.get("stock_actual"), "productos_terminados.stock_actual")
            price = _decimal(row.get("precio_unitario"), "productos_terminados.precio_unitario")
            product = Product.objects.create(
                code=code,
                name=name,
                kind=Product.Kind.FINISHED,
                unit="unidad",
                sale_price=price,
                current_stock=stock,
                minimum_stock=Decimal("0"),
            )
            finished_products[old_id] = product
            new_product_count += 1
            if stock > 0:
                InventoryMovement.objects.create(
                    product=product,
                    kind=InventoryMovement.Kind.ADJUSTMENT,
                    quantity=stock,
                    stock_before=Decimal("0"),
                    stock_after=stock,
                    note="Saldo inicial importado desde la base anterior",
                    recorded_by=actor,
                )
            AuditLog.objects.create(actor=actor, action="IMPORT", entity="Product", entity_id=product.pk, details={"legacy_id": old_id, "code": code})

        for row in source["materia_prima"]:
            old_id = _integer(row.get("id"), "materia_prima.id")
            name = str(row.get("nombre") or "").strip()
            if not name or len(name) > 160:
                raise CommandError(f"Nombre inválido para la materia prima anterior #{old_id}.")
            code = f"OLD-MP-{old_id:04d}"
            if Product.objects.filter(code=code).exists():
                raise CommandError(f"El código {code} ya existe; se canceló la importación sin cambios.")
            stock = _decimal(row.get("stock_actual"), "materia_prima.stock_actual")
            minimum = _decimal(row.get("stock_minimo"), "materia_prima.stock_minimo")
            product = Product.objects.create(
                code=code,
                name=name,
                kind=Product.Kind.RAW_MATERIAL,
                unit=str(row.get("unidad_medida") or "kg")[:30],
                current_stock=stock,
                minimum_stock=minimum,
            )
            raw_materials[old_id] = product
            new_material_count += 1
            if stock > 0:
                InventoryMovement.objects.create(
                    product=product,
                    kind=InventoryMovement.Kind.ADJUSTMENT,
                    quantity=stock,
                    stock_before=Decimal("0"),
                    stock_after=stock,
                    note="Saldo inicial importado desde la base anterior",
                    recorded_by=actor,
                )
            AuditLog.objects.create(actor=actor, action="IMPORT", entity="Product", entity_id=product.pk, details={"legacy_id": old_id, "code": code})

        for row in source["clientes"]:
            old_id = _integer(row.get("id"), "clientes.id")
            name = str(row.get("nombre_razon_social") or "").strip()
            if not name or len(name) > 180:
                raise CommandError(f"Nombre inválido para el cliente anterior #{old_id}.")
            tax_id = str(row.get("nit_ci") or "").strip()
            client = None
            if tax_id:
                client = Client.objects.filter(tax_id__iexact=tax_id).first()
            if client is None:
                client = Client.objects.filter(name__iexact=name).first()
            if client is None:
                client = Client.objects.create(
                    name=name,
                    tax_id=tax_id[:30],
                    phone=str(row.get("telefono") or "")[:40],
                    email=str(row.get("email") or "")[:254],
                )
                new_client_count += 1
                AuditLog.objects.create(actor=actor, action="IMPORT", entity="Client", entity_id=client.pk, details={"legacy_id": old_id})
            client_map[old_id] = client

        orders = {_integer(row.get("id"), "pedidos.id"): row for row in source["pedidos"]}
        detail_rows = source["detalles_pedido"]
        delivered_rows = []
        pending_lines = {}
        skipped_order_count = 0
        for row in detail_rows:
            order_id = _integer(row.get("id_pedido"), "detalles_pedido.id_pedido")
            order = orders.get(order_id)
            if order is None:
                raise CommandError(f"El detalle referencia un pedido inexistente #{order_id}.")
            status = str(order.get("estado") or "").strip().casefold()
            old_product_id = _integer(row.get("id_producto_terminado"), "detalles_pedido.id_producto_terminado")
            product = finished_products.get(old_product_id)
            if product is None:
                raise CommandError(f"El detalle referencia un producto inexistente #{old_product_id}.")
            quantity = _decimal(row.get("cantidad"), "detalles_pedido.cantidad")
            price = _decimal(row.get("precio_unitario"), "detalles_pedido.precio_unitario")
            if quantity <= 0 or price < 0:
                raise CommandError(f"Cantidad o precio inválido en el detalle del pedido #{order_id}.")
            if status in DELIVERED_STATUSES:
                delivered_rows.append((row, order, product, quantity, price))
            elif status in PENDING_STATUSES:
                pending_lines.setdefault(order_id, []).append((product, quantity, price))
            else:
                skipped_order_count += 1

        if not delivered_rows:
            raise CommandError("No se encontraron detalles de pedidos entregados para el historial.")
        first_date = min(_legacy_date(order.get("fecha_pedido")) for _, order, _, _, _ in delivered_rows)
        last_date = max(_legacy_date(order.get("fecha_pedido")) for _, order, _, _, _ in delivered_rows)
        if SalesHistoryImport.objects.filter(is_simulated=False, first_date__lte=last_date, last_date__gte=first_date).exists():
            raise CommandError("El período de pedidos se superpone con otro historial importado.")

        batch = SalesHistoryImport.objects.create(
            filename=path.name[:255],
            file_sha256=file_hash,
            row_count=len(delivered_rows),
            first_date=first_date,
            last_date=last_date,
            imported_by=actor,
        )
        HistoricalSale.objects.bulk_create([
            HistoricalSale(
                batch=batch,
                row_number=_integer(detail.get("id"), "detalles_pedido.id"),
                product=product,
                client=client_map.get(_integer(order.get("id_cliente"), "pedidos.id_cliente")),
                sold_on=_legacy_date(order.get("fecha_pedido")),
                quantity=quantity,
                unit_price=price,
            )
            for detail, order, product, quantity, price in delivered_rows
        ])

        pending_order_count = 0
        for order_id, lines in pending_lines.items():
            row = orders[order_id]
            client_id = _integer(row.get("id_cliente"), "pedidos.id_cliente")
            client = client_map.get(client_id)
            if client is None:
                continue
            order = CustomerOrder.objects.create(
                client=client,
                created_by=actor,
                status=CustomerOrder.Status.PENDING,
                notes=f"Pedido anterior #{order_id}",
            )
            CustomerOrderItem.objects.bulk_create([
                CustomerOrderItem(order=order, product=product, quantity=quantity, unit_price=price)
                for product, quantity, price in lines
            ])
            pending_order_count += 1
            AuditLog.objects.create(actor=actor, action="IMPORT", entity="CustomerOrder", entity_id=order.pk, details={"legacy_id": order_id})

        AuditLog.objects.create(
            actor=actor,
            action="IMPORT",
            entity="LegacyMatriplastImport",
            entity_id=file_hash[:32],
            details={
                "finished_products": new_product_count,
                "raw_materials": new_material_count,
                "clients_created": new_client_count,
                "delivered_order_lines": len(delivered_rows),
                "pending_orders": pending_order_count,
                "skipped_orders": skipped_order_count,
                "from": first_date.isoformat(),
                "to": last_date.isoformat(),
            },
        )
        self.stdout.write(self.style.SUCCESS(
            f"Importación completada: {new_product_count} productos terminados, {new_material_count} materias primas, "
            f"{new_client_count} clientes nuevos, {len(delivered_rows)} líneas históricas y {pending_order_count} pedido(s) pendiente(s)."
        ))
        self.stdout.write(self.style.WARNING(
            "No se importaron usuarios, contraseñas, máquinas, diagnósticos ni facturas. Los pedidos entregados se guardaron solo como historial, sin descontar stock."
        ))
