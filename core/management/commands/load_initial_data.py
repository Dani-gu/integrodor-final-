from datetime import date, datetime, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import AuditLog, Client, InventoryMovement, Payment, Product, Sale
from core.services import create_customer_order, create_sale, register_purchase


PRODUCTS = (
    ("ENV-500", "Botella plástica 500 ml", Product.Kind.FINISHED, "unidad", "1.80", "1500"),
    ("ENV-250", "Envase plástico 250 ml", Product.Kind.FINISHED, "unidad", "2.50", "1500"),
    ("BID-1L", "Bidón plástico 1 L", Product.Kind.FINISHED, "unidad", "6.00", "1500"),
    ("MAT-RES-01", "Resina plástica", Product.Kind.RAW_MATERIAL, "kg", "", "0"),
    ("MAT-TIN-01", "Tinte plástico", Product.Kind.RAW_MATERIAL, "kg", "", "0"),
    ("MAT-AD-01", "Aditivo plástico", Product.Kind.RAW_MATERIAL, "kg", "", "0"),
)

CLIENTS = (
    ("Envases del Altiplano", "", "72000001"),
    ("Distribuidora Andina", "", "72000002"),
    ("Comercial La Paz", "", "72000003"),
)

MONTHLY_QUANTITIES = (
    (70, 75, 80, 78, 82, 85, 90, 88, 94, 98, 100, 105),
    (45, 40, 52, 55, 58, 56, 62, 65, 60, 68, 72, 75),
    (18, 20, 22, 20, 25, 24, 28, 26, 30, 32, 34, 36),
)


def next_month(value):
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


class Command(BaseCommand):
    help = "Carga clientes, productos e historial de ventas para iniciar y probar el sistema."

    @transaction.atomic
    def handle(self, *args, **options):
        codes = [item[0] for item in PRODUCTS]
        if Product.objects.filter(code__in=codes).exists():
            raise CommandError("La carga inicial ya existe o hay códigos ocupados; no se insertó ningún dato.")
        actor = get_user_model().objects.filter(is_active=True).order_by("-is_superuser", "id").first()
        if actor is None:
            raise CommandError("Primero crea e inicia sesión con un usuario activo.")

        products = []
        for code, name, kind, unit, price, initial_stock in PRODUCTS:
            product = Product.objects.create(
                code=code,
                name=name,
                kind=kind,
                unit=unit,
                sale_price=Decimal(price) if price else None,
                current_stock=Decimal(initial_stock),
                minimum_stock=Decimal("30"),
            )
            products.append(product)
            AuditLog.objects.create(
                actor=actor,
                action="CREATE",
                entity="Product",
                entity_id=product.pk,
                details={"code": code, "initial_stock": str(product.current_stock)},
            )
            if product.current_stock:
                InventoryMovement.objects.create(
                    product=product,
                    kind=InventoryMovement.Kind.ADJUSTMENT,
                    quantity=product.current_stock,
                    stock_before=Decimal("0"),
                    stock_after=product.current_stock,
                    note="Saldo inicial",
                    recorded_by=actor,
                )

        clients = [Client.objects.create(name=name, tax_id=tax_id, phone=phone) for name, tax_id, phone in CLIENTS]
        for client in clients:
            AuditLog.objects.create(actor=actor, action="CREATE", entity="Client", entity_id=client.pk)

        # Register purchases through the same service used by the application so stock and expenses reconcile.
        for product, quantity, amount in (
            (products[3], "500", "8500.00"),
            (products[4], "80", "2400.00"),
            (products[5], "45", "1350.00"),
        ):
            register_purchase(
                actor=actor,
                product_id=product.pk,
                quantity=Decimal(quantity),
                amount=Decimal(amount),
                description=f"Compra de {product.name.lower()}",
            )

        month = date(2025, 10, 1)
        for month_index in range(12):
            sold_at = timezone.make_aware(datetime.combine(month.replace(day=min(12 + month_index, 25)), time(10, 0)))
            for product_index in range(3):
                product = products[product_index]
                quantity = Decimal(MONTHLY_QUANTITIES[product_index][month_index])
                total = quantity * product.sale_price
                sale = create_sale(
                    actor=actor,
                    client_id=clients[(month_index + product_index) % len(clients)].pk,
                    lines=[{"product_id": product.pk, "quantity": quantity, "unit_price": product.sale_price}],
                    payments=[{"amount": total, "method": Payment.Method.CASH}],
                )
                Sale.objects.filter(pk=sale.pk).update(sold_at=sold_at)
                Payment.objects.filter(sale=sale).update(paid_at=sold_at)
            month = next_month(month)

        create_customer_order(
            actor=actor,
            client_id=clients[0].pk,
            lines=[{"product_id": products[0].pk, "quantity": Decimal("12"), "unit_price": products[0].sale_price}],
            notes="Solicitud de productos para entrega posterior.",
        )

        self.stdout.write(self.style.SUCCESS(
            "Carga inicial completada: 6 productos, 3 clientes, 36 ventas históricas, 3 compras y 1 pedido."
        ))
