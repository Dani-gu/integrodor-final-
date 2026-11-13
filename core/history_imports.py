"""Validated, all-or-nothing import of historical sales from CSV."""

import csv
import hashlib
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AuditLog, Client, HistoricalSale, Product, SalesHistoryImport


REQUIRED_COLUMNS = {"codigo_producto", "fecha", "cantidad"}
OPTIONAL_COLUMNS = {"precio_unitario", "documento_cliente", "cliente"}
MAX_ROWS = 20_000


def _parse_date(value):
    value = value.strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise ValueError("usa AAAA-MM-DD o DD/MM/AAAA")


def _parse_decimal(value, decimal_comma=False):
    value = value.strip().replace(" ", "")
    if decimal_comma:
        value = value.replace(".", "").replace(",", ".") if "," in value else value
    return Decimal(value)


def _match_client(document, name, row_number):
    if not document and not name:
        return None
    if document:
        matches = Client.objects.filter(tax_id__iexact=document)
        if matches.count() == 1:
            client = matches.first()
            if name and client.name.casefold() != name.casefold():
                raise ValueError(f"la fila {row_number}: el nombre no coincide con el documento del cliente")
            return client
        raise ValueError(f"la fila {row_number}: no se encontró un cliente único con ese documento")
    matches = Client.objects.filter(name__iexact=name)
    if matches.count() == 1:
        return matches.first()
    raise ValueError(f"la fila {row_number}: el nombre del cliente no existe o no es único; registra su documento")


@transaction.atomic
def import_sales_history(*, uploaded_file, actor, ip_address=None):
    raw = uploaded_file.read()
    if not raw:
        raise ValidationError("El archivo está vacío.")
    file_hash = hashlib.sha256(raw).hexdigest()
    if SalesHistoryImport.objects.filter(file_sha256=file_hash).exists():
        raise ValidationError("Este mismo archivo ya fue importado.")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("El CSV debe estar codificado como UTF-8.") from exc

    try:
        dialect = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    headers = {header.strip().lower() for header in (reader.fieldnames or []) if header}
    missing = REQUIRED_COLUMNS - headers
    if missing:
        raise ValidationError("Faltan columnas obligatorias: " + ", ".join(sorted(missing)))
    if headers - REQUIRED_COLUMNS - OPTIONAL_COLUMNS:
        raise ValidationError("El archivo contiene columnas no reconocidas.")
    if len(headers) != len(reader.fieldnames or []):
        raise ValidationError("Hay encabezados vacíos o repetidos.")

    product_map = {product.code.casefold(): product for product in Product.objects.filter(kind=Product.Kind.FINISHED)}
    parsed_rows = []
    errors = []
    current_month = timezone.localdate().replace(day=1)
    for row_number, row in enumerate(reader, start=2):
        if not row or not any((value or "").strip() for value in row.values() if isinstance(value, str)):
            continue
        if row_number > MAX_ROWS + 1:
            raise ValidationError(f"El archivo supera el límite de {MAX_ROWS} filas.")
        normalized = {(key or "").strip().lower(): (value or "").strip() for key, value in row.items() if key is not None}
        try:
            if None in row:
                raise ValueError(f"la fila {row_number}: tiene más valores que encabezados")
            code = normalized.get("codigo_producto", "").casefold()
            product = product_map.get(code)
            if not product:
                raise ValueError(f"la fila {row_number}: no existe el producto terminado indicado")
            sold_on = _parse_date(normalized.get("fecha", ""))
            if sold_on >= current_month:
                raise ValueError(f"la fila {row_number}: solo se aceptan fechas anteriores al mes actual")
            decimal_comma = dialect.delimiter == ";"
            quantity = _parse_decimal(normalized.get("cantidad", ""), decimal_comma=decimal_comma)
            if not quantity.is_finite() or quantity <= 0:
                raise ValueError(f"la fila {row_number}: la cantidad debe ser mayor que cero")
            raw_price = normalized.get("precio_unitario", "")
            unit_price = _parse_decimal(raw_price, decimal_comma=decimal_comma) if raw_price else None
            if unit_price is not None and (not unit_price.is_finite() or unit_price < 0):
                raise ValueError(f"la fila {row_number}: el precio no puede ser negativo")
            client = _match_client(normalized.get("documento_cliente", ""), normalized.get("cliente", ""), row_number)
            parsed_rows.append({
                "row_number": row_number,
                "product": product,
                "client": client,
                "sold_on": sold_on,
                "quantity": quantity,
                "unit_price": unit_price,
            })
        except (ValueError, InvalidOperation) as exc:
            errors.append(str(exc))
            if len(errors) >= 20:
                break

    if errors:
        raise ValidationError(errors)
    if not parsed_rows:
        raise ValidationError("El archivo no contiene filas de ventas para importar.")

    first_date = min(row["sold_on"] for row in parsed_rows)
    last_date = max(row["sold_on"] for row in parsed_rows)
    overlapping = SalesHistoryImport.objects.filter(
        is_simulated=False, first_date__lte=last_date, last_date__gte=first_date
    ).exists()
    if overlapping:
        raise ValidationError("Las fechas de este archivo se superponen con otro historial importado. Consolida los datos en un solo CSV por período.")

    batch = SalesHistoryImport.objects.create(
        filename=uploaded_file.name[:255],
        file_sha256=file_hash,
        row_count=len(parsed_rows),
        first_date=first_date,
        last_date=last_date,
        imported_by=actor,
    )
    HistoricalSale.objects.bulk_create([
        HistoricalSale(batch=batch, **row) for row in parsed_rows
    ])
    AuditLog.objects.create(
        actor=actor,
        action="IMPORT",
        entity="SalesHistoryImport",
        entity_id=batch.pk,
        details={"filename": batch.filename, "rows": batch.row_count, "from": first_date.isoformat(), "to": last_date.isoformat()},
        ip_address=ip_address,
    )
    return batch
