"""Transactional business operations for sales, payments and stock changes."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import (
    AuditLog,
    Client,
    CustomerOrder,
    CustomerOrderItem,
    InventoryMovement,
    Machine,
    Payment,
    Product,
    ProductionConsumption,
    ProductionLot,
    PurchaseExpense,
    Sale,
    SaleItem,
)


def _audit(actor, action, entity, entity_id="", details=None, ip_address=None):
    AuditLog.objects.create(
        actor=actor,
        action=action,
        entity=entity,
        entity_id=str(entity_id),
        details=details or {},
        ip_address=ip_address,
    )


def _refresh_sale_status(sale):
    total = sum(
        (item.quantity * item.unit_price for item in sale.items.all()),
        start=Decimal("0.00"),
    )
    paid = sum((payment.amount for payment in sale.payments.all()), start=Decimal("0.00"))
    if paid > total:
        raise ValidationError("El monto pagado no puede superar el total de la venta.")
    sale.status = Sale.Status.PAID if paid == total else Sale.Status.PARTIAL if paid > 0 else Sale.Status.OPEN
    sale.save(update_fields=["status"])


@transaction.atomic
def create_sale(*, actor, client_id, lines, payments=(), ip_address=None):
    """Create a sale, reserve stock, create movements and optionally record payments.

    ``lines`` is an iterable of dictionaries with product_id, quantity and unit_price.
    ``payments`` is an iterable of dictionaries with amount, method and optional reference.
    """
    if not lines:
        raise ValidationError("La venta debe incluir al menos un producto.")

    client = Client.objects.get(pk=client_id, active=True)
    normalized = []
    product_ids = set()
    for line in lines:
        product_id = int(line["product_id"])
        quantity = Decimal(str(line["quantity"]))
        unit_price = Decimal(str(line["unit_price"]))
        if quantity <= 0 or unit_price < 0:
            raise ValidationError("La cantidad debe ser positiva y el precio no puede ser negativo.")
        normalized.append((product_id, quantity, unit_price))
        product_ids.add(product_id)

    products = {
        product.pk: product
        for product in Product.objects.select_for_update().filter(pk__in=product_ids, active=True).order_by("pk")
    }
    if len(products) != len(product_ids):
        raise ValidationError("Uno o más productos no existen o están inactivos.")

    requested = {}
    for product_id, quantity, _ in normalized:
        product = products[product_id]
        if product.kind != Product.Kind.FINISHED:
            raise ValidationError(f"{product.name} no es un producto terminado vendible.")
        requested[product_id] = requested.get(product_id, Decimal("0")) + quantity
    for product_id, quantity in requested.items():
        product = products[product_id]
        if product.current_stock < quantity:
            raise ValidationError(
                f"Stock insuficiente para {product.name}: disponible {product.current_stock} {product.unit}."
            )

    sale = Sale.objects.create(client=client, created_by=actor)
    running_stock = {product_id: products[product_id].current_stock for product_id in requested}
    for product_id, quantity, unit_price in normalized:
        product = products[product_id]
        stock_before = running_stock[product_id]
        stock_after = stock_before - quantity
        SaleItem.objects.create(sale=sale, product=product, quantity=quantity, unit_price=unit_price)
        Product.objects.filter(pk=product_id).update(current_stock=F("current_stock") - quantity)
        InventoryMovement.objects.create(
            product=product,
            kind=InventoryMovement.Kind.SALE,
            quantity=quantity,
            stock_before=stock_before,
            stock_after=stock_after,
            sale=sale,
            note=f"Salida por venta {sale.pk}",
            recorded_by=actor,
        )
        running_stock[product_id] = stock_after

    for payment_data in payments:
        payment_amount = Decimal(str(payment_data["amount"]))
        if payment_amount <= 0:
            raise ValidationError("El monto de cada pago debe ser mayor que cero.")
        if payment_data["method"] not in Payment.Method.values:
            raise ValidationError("La forma de pago no es válida.")
        Payment.objects.create(
            sale=sale,
            amount=payment_amount,
            method=payment_data["method"],
            reference=payment_data.get("reference", ""),
            recorded_by=actor,
        )
    _refresh_sale_status(sale)
    stock_changes = [
        {
            "product_id": product_id,
            "product": products[product_id].name,
            "unit": products[product_id].unit,
            "direction": "OUT",
            "quantity": str(quantity),
            "stock_before": str(products[product_id].current_stock),
            "stock_after": str(products[product_id].current_stock - quantity),
        }
        for product_id, quantity in requested.items()
    ]
    _audit(
        actor,
        "CREATE",
        "Sale",
        sale.pk,
        {"client_id": client.pk, "line_count": len(normalized), "status": sale.status, "stock_changes": stock_changes},
        ip_address,
    )
    return sale


@transaction.atomic
def create_customer_order(*, actor, client_id, lines, notes="", ip_address=None):
    if not lines:
        raise ValidationError("El pedido debe incluir al menos un producto.")
    client = Client.objects.get(pk=client_id, active=True)
    requested = {}
    normalized = []
    for line in lines:
        product_id = int(line["product_id"])
        quantity = Decimal(str(line["quantity"]))
        unit_price = Decimal(str(line["unit_price"]))
        if quantity <= 0 or unit_price < 0:
            raise ValidationError("La cantidad debe ser positiva y el precio no puede ser negativo.")
        requested[product_id] = requested.get(product_id, Decimal("0")) + quantity
        normalized.append((product_id, quantity, unit_price))
    products = {
        product.pk: product
        for product in Product.objects.select_for_update().filter(pk__in=requested, active=True).order_by("pk")
    }
    if len(products) != len(requested):
        raise ValidationError("Uno o más productos no existen o están inactivos.")
    for product_id, quantity in requested.items():
        product = products[product_id]
        if product.kind != Product.Kind.FINISHED:
            raise ValidationError(f"{product.name} no es un producto terminado vendible.")
        if product.current_stock < quantity:
            raise ValidationError(f"Stock insuficiente para {product.name}: disponible {product.current_stock} {product.unit}.")

    order = CustomerOrder.objects.create(client=client, created_by=actor, notes=notes)
    CustomerOrderItem.objects.bulk_create([
        CustomerOrderItem(order=order, product=products[product_id], quantity=quantity, unit_price=unit_price)
        for product_id, quantity, unit_price in normalized
    ])
    _audit(actor, "CREATE", "CustomerOrder", order.pk, {"client_id": client.pk, "lines": len(normalized)}, ip_address)
    return order


@transaction.atomic
def convert_customer_order_to_sale(*, actor, order_id, ip_address=None):
    order = CustomerOrder.objects.select_for_update().prefetch_related("items").get(pk=order_id)
    if order.status != CustomerOrder.Status.PENDING:
        raise ValidationError("Solo se pueden convertir pedidos pendientes.")
    lines = [{"product_id": item.product_id, "quantity": item.quantity, "unit_price": item.unit_price} for item in order.items.all()]
    sale = create_sale(actor=actor, client_id=order.client_id, lines=lines, ip_address=ip_address)
    order.status = CustomerOrder.Status.CONVERTED
    order.sale = sale
    order.save(update_fields=["status", "sale"])
    _audit(actor, "CONVERT", "CustomerOrder", order.pk, {"sale_id": sale.pk}, ip_address)
    return sale


@transaction.atomic
def cancel_customer_order(*, actor, order_id, ip_address=None):
    order = CustomerOrder.objects.select_for_update().get(pk=order_id)
    if order.status != CustomerOrder.Status.PENDING:
        raise ValidationError("Solo se pueden cancelar pedidos pendientes.")
    order.status = CustomerOrder.Status.CANCELLED
    order.save(update_fields=["status"])
    _audit(actor, "CANCEL", "CustomerOrder", order.pk, {}, ip_address)
    return order


@transaction.atomic
def record_payment(*, actor, sale_id, amount, method, reference="", ip_address=None):
    sale = Sale.objects.select_for_update().get(pk=sale_id)
    if sale.status == Sale.Status.CANCELLED:
        raise ValidationError("No se pueden registrar pagos en una venta anulada.")
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValidationError("El monto del pago debe ser mayor que cero.")
    if method not in Payment.Method.values:
        raise ValidationError("La forma de pago no es válida.")
    payment = Payment.objects.create(
        sale=sale,
        amount=amount,
        method=method,
        reference=reference,
        recorded_by=actor,
    )
    try:
        _refresh_sale_status(sale)
    except ValidationError:
        payment.delete()
        raise
    _audit(
        actor,
        "PAYMENT",
        "Sale",
        sale.pk,
        {"payment_id": payment.pk, "amount": str(payment.amount), "method": payment.method},
        ip_address,
    )
    return payment


@transaction.atomic
def cancel_unpaid_sale(*, actor, sale_id, reason, ip_address=None):
    sale = Sale.objects.select_for_update().get(pk=sale_id)
    if sale.status == Sale.Status.CANCELLED:
        raise ValidationError("La venta ya está anulada.")
    if sale.amount_paid > 0:
        raise ValidationError("La venta tiene pagos registrados. Registra primero la devolución del dinero.")

    items = list(sale.items.select_related("product").order_by("product_id"))
    products = {
        product.pk: product
        for product in Product.objects.select_for_update().filter(pk__in={item.product_id for item in items}).order_by("pk")
    }
    running_stock = {product_id: product.current_stock for product_id, product in products.items()}
    for item in items:
        product = products[item.product_id]
        stock_before = running_stock[item.product_id]
        stock_after = stock_before + item.quantity
        Product.objects.filter(pk=product.pk).update(current_stock=F("current_stock") + item.quantity)
        InventoryMovement.objects.create(
            product=product,
            kind=InventoryMovement.Kind.SALE_VOID,
            quantity=item.quantity,
            stock_before=stock_before,
            stock_after=stock_after,
            sale=sale,
            note=f"Anulación de venta {sale.pk}: {reason}",
            recorded_by=actor,
        )
        running_stock[item.product_id] = stock_after
    sale.status = Sale.Status.CANCELLED
    sale.save(update_fields=["status"])
    restored_by_product = {}
    for item in items:
        restored_by_product[item.product_id] = restored_by_product.get(item.product_id, Decimal("0")) + item.quantity
    stock_changes = [
        {
            "product_id": product_id,
            "product": products[product_id].name,
            "unit": products[product_id].unit,
            "direction": "IN",
            "quantity": str(quantity),
            "stock_before": str(products[product_id].current_stock),
            "stock_after": str(products[product_id].current_stock + quantity),
        }
        for product_id, quantity in restored_by_product.items()
    ]
    _audit(actor, "CANCEL", "Sale", sale.pk, {"reason": reason, "restocked_lines": len(items), "stock_changes": stock_changes}, ip_address)
    return sale


@transaction.atomic
def register_purchase(*, actor, product_id, quantity, amount, description="Compra de materia prima", ip_address=None):
    product = Product.objects.select_for_update().get(pk=product_id, active=True)
    if product.kind != Product.Kind.RAW_MATERIAL:
        raise ValidationError("Las compras de insumos deben asociarse a una materia prima.")
    quantity = Decimal(str(quantity))
    amount = Decimal(str(amount))
    if quantity <= 0 or amount <= 0:
        raise ValidationError("La cantidad y el monto deben ser mayores que cero.")
    stock_before = product.current_stock
    stock_after = stock_before + quantity

    expense = PurchaseExpense.objects.create(
        kind=PurchaseExpense.Kind.RAW_MATERIAL,
        description=description,
        amount=amount,
        product=product,
        quantity=quantity,
        recorded_by=actor,
    )
    Product.objects.filter(pk=product.pk).update(current_stock=F("current_stock") + quantity)
    InventoryMovement.objects.create(
        product=product,
        kind=InventoryMovement.Kind.PURCHASE,
        quantity=quantity,
        stock_before=stock_before,
        stock_after=stock_after,
        note=description,
        recorded_by=actor,
    )
    _audit(
        actor,
        "PURCHASE",
        "PurchaseExpense",
        expense.pk,
        {
            "product_id": product.pk,
            "product": product.name,
            "unit": product.unit,
            "quantity": str(quantity),
            "amount": str(amount),
            "stock_before": str(stock_before),
            "stock_after": str(stock_after),
        },
        ip_address,
    )
    return expense


@transaction.atomic
def start_production_lot(*, actor, lot_id, ip_address=None):
    # The machine FK is nullable, so select_related() creates an outer join.
    # Lock only the lot row to avoid PostgreSQL's FOR UPDATE/nullable-join error.
    lot = ProductionLot.objects.select_for_update(of=("self",)).select_related("machine").get(pk=lot_id)
    if lot.started_at:
        raise ValidationError("El lote ya fue iniciado.")
    if lot.finished_at:
        raise ValidationError("El lote ya está finalizado.")
    if lot.machine_id and lot.machine.status != Machine.Status.OPERATIONAL:
        raise ValidationError("La máquina asignada ya no está operativa. Edita el lote pendiente y selecciona una máquina disponible.")
    lot.started_at = timezone.now()
    lot.operator = actor
    lot.save(update_fields=["started_at", "operator"])
    _audit(actor, "START", "ProductionLot", lot.pk, {}, ip_address)
    return lot


@transaction.atomic
def finish_production_lot(*, actor, lot_id, produced_quantity, consumptions=(), confirm_overproduction=False, ip_address=None):
    lot = ProductionLot.objects.select_for_update(of=("self",)).select_related("product").get(pk=lot_id)
    if not lot.started_at:
        raise ValidationError("Primero se debe iniciar el lote.")
    if lot.finished_at:
        raise ValidationError("El lote ya fue finalizado.")
    produced_quantity = Decimal(str(produced_quantity))
    if produced_quantity < 0:
        raise ValidationError("La cantidad producida no puede ser negativa.")
    if produced_quantity > lot.planned_quantity and not confirm_overproduction:
        raise ValidationError("La cantidad producida supera lo planificado. Confirma que el valor ingresado es correcto.")
    if lot.product.kind != Product.Kind.FINISHED:
        raise ValidationError("El lote debe corresponder a un producto terminado.")

    requested = {}
    for item in consumptions:
        material_id = int(item["material_id"])
        quantity = Decimal(str(item["quantity"]))
        if quantity <= 0:
            raise ValidationError("La cantidad de materia prima consumida debe ser mayor que cero.")
        requested[material_id] = requested.get(material_id, Decimal("0")) + quantity
    if produced_quantity > 0 and not requested:
        raise ValidationError("Registra al menos una materia prima consumida para finalizar un lote con producción.")
    product_ids_to_lock = set(requested) | {lot.product_id}
    locked_products = {
        product.pk: product
        for product in Product.objects.select_for_update().filter(pk__in=product_ids_to_lock).order_by("pk")
    }
    if len(locked_products) != len(product_ids_to_lock):
        raise ValidationError("Una materia prima seleccionada ya no está disponible.")
    finished_product = locked_products[lot.product_id]
    materials = {material_id: locked_products[material_id] for material_id in requested}
    for material_id, quantity in requested.items():
        material = materials[material_id]
        if material.kind != Product.Kind.RAW_MATERIAL:
            raise ValidationError(f"{material.name} no está registrado como materia prima.")
        if not material.active:
            raise ValidationError(f"{material.name} está inactiva y no se puede consumir.")
        if material.current_stock < quantity:
            raise ValidationError(
                f"Stock insuficiente de {material.name}: disponible {material.current_stock} {material.unit}, solicitado {quantity} {material.unit}."
            )

    lot.produced_quantity = produced_quantity
    lot.finished_at = timezone.now()
    lot.operator = actor
    lot.save(update_fields=["produced_quantity", "finished_at", "operator"])
    stock_changes = []
    if produced_quantity:
        stock_changes.append({
            "product_id": finished_product.pk,
            "product": finished_product.name,
            "unit": finished_product.unit,
            "direction": "IN",
            "quantity": str(produced_quantity),
            "stock_before": str(finished_product.current_stock),
            "stock_after": str(finished_product.current_stock + produced_quantity),
        })
        Product.objects.filter(pk=finished_product.pk).update(current_stock=F("current_stock") + produced_quantity)
        InventoryMovement.objects.create(
            product=finished_product,
            kind=InventoryMovement.Kind.PRODUCTION,
            quantity=produced_quantity,
            stock_before=finished_product.current_stock,
            stock_after=finished_product.current_stock + produced_quantity,
            production_lot=lot,
            note=f"Ingreso del lote {lot.pk}",
            recorded_by=actor,
        )
    for material_id, quantity in requested.items():
        material = materials[material_id]
        stock_changes.append({
            "product_id": material.pk,
            "product": material.name,
            "unit": material.unit,
            "direction": "OUT",
            "quantity": str(quantity),
            "stock_before": str(material.current_stock),
            "stock_after": str(material.current_stock - quantity),
        })
        Product.objects.filter(pk=material.pk).update(current_stock=F("current_stock") - quantity)
        ProductionConsumption.objects.create(lot=lot, material=material, quantity=quantity)
        InventoryMovement.objects.create(
            product=material,
            kind=InventoryMovement.Kind.CONSUMPTION,
            quantity=quantity,
            stock_before=material.current_stock,
            stock_after=material.current_stock - quantity,
            production_lot=lot,
            note=f"Consumo del lote {lot.pk}",
            recorded_by=actor,
        )
    _audit(
        actor,
        "FINISH",
        "ProductionLot",
        lot.pk,
        {
            "produced_quantity": str(produced_quantity),
            "planned_quantity": str(lot.planned_quantity),
            "over_plan_confirmed": bool(confirm_overproduction and produced_quantity > lot.planned_quantity),
            "materials_consumed": len(requested),
            "machine_id": lot.machine_id,
            "stock_changes": stock_changes,
        },
        ip_address,
    )
    return lot


@transaction.atomic
def register_other_expense(*, actor, description, amount, ip_address=None):
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValidationError("El monto debe ser mayor que cero.")
    expense = PurchaseExpense.objects.create(
        kind=PurchaseExpense.Kind.OTHER,
        description=description,
        amount=amount,
        recorded_by=actor,
    )
    _audit(
        actor,
        "EXPENSE",
        "PurchaseExpense",
        expense.pk,
        {"amount": str(amount), "description": description},
        ip_address,
    )
    return expense


@transaction.atomic
def adjust_inventory(*, actor, product_id, direction, quantity, reason, ip_address=None):
    product = Product.objects.select_for_update().get(pk=product_id, active=True)
    quantity = Decimal(str(quantity))
    if quantity <= 0:
        raise ValidationError("La cantidad del ajuste debe ser mayor que cero.")
    if direction not in ("IN", "OUT"):
        raise ValidationError("El tipo de ajuste no es válido.")
    if direction == "OUT" and product.current_stock < quantity:
        raise ValidationError(
            f"No se puede reducir {product.name}: disponible {product.current_stock} {product.unit}."
        )

    movement_kind = InventoryMovement.Kind.CORRECTION_IN if direction == "IN" else InventoryMovement.Kind.CORRECTION_OUT
    stock_change = quantity if direction == "IN" else -quantity
    stock_before = product.current_stock
    stock_after = stock_before + stock_change
    Product.objects.filter(pk=product.pk).update(current_stock=F("current_stock") + stock_change)
    movement = InventoryMovement.objects.create(
        product=product,
        kind=movement_kind,
        quantity=quantity,
        stock_before=stock_before,
        stock_after=stock_after,
        note=reason,
        recorded_by=actor,
    )
    _audit(
        actor,
        "STOCK_ADJUSTMENT",
        "InventoryMovement",
        movement.pk,
        {
            "product_id": product.pk,
            "product": product.name,
            "unit": product.unit,
            "direction": direction,
            "quantity": str(quantity),
            "reason": reason,
            "stock_before": str(stock_before),
            "stock_after": str(stock_after),
        },
        ip_address,
    )
    return movement
