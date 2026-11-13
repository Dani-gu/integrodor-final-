import hashlib
import random
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max, Min
from django.db.models.functions import TruncMonth
from django.utils import timezone

from core.forecasting import actual_sales_by_month
from core.forecasting import generate_monthly_forecasts
from core.models import AuditLog, HistoricalSale, Product, SalesHistoryImport


def shift_month(month, offset):
    index = month.year * 12 + month.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


class Command(BaseCommand):
    help = "Crea historial mensual simulado, separado de las ventas operativas, para probar los pronósticos ML."

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, default=24, help="Meses sintéticos anteriores al historial importado (mínimo 12).")
        parser.add_argument("--seed", type=int, default=20261111, help="Semilla para repetir de forma reproducible la simulación.")

    @transaction.atomic
    def handle(self, *args, **options):
        months_count = options["months"]
        seed = options["seed"]
        if months_count < 12 or months_count > 60:
            raise CommandError("--months debe estar entre 12 y 60.")

        existing_batch = SalesHistoryImport.objects.filter(
            is_simulated=True,
            file_sha256=hashlib.sha256(f"matriplast-ml-demo:{months_count}:{seed}".encode()).hexdigest(),
        ).first()
        if existing_batch:
            self.stdout.write(self.style.WARNING(
                f"Esta simulación ya fue creada (importación #{existing_batch.pk}, {existing_batch.row_count} filas). No se duplicó."
            ))
            return

        real_batches = SalesHistoryImport.objects.filter(is_simulated=False)
        first_real_date = real_batches.aggregate(value=Min("first_date"))["value"]
        if not first_real_date:
            raise CommandError("Se necesita al menos un historial real importado para calcular una simulación basada en los productos existentes.")

        last_complete_month = (timezone.localdate().replace(day=1) - timedelta(days=1)).replace(day=1)
        reference_months = [shift_month(last_complete_month, offset) for offset in range(-11, 1)]
        reference_cutoff = shift_month(last_complete_month, 1)
        products = list(Product.objects.filter(kind=Product.Kind.FINISHED, active=True).order_by("name"))
        product_ids = {product.pk for product in products}
        observed_sales = actual_sales_by_month(product_ids, reference_cutoff)
        reference_by_product = {
            product.pk: {month.month: observed_sales.get((product.pk, month), Decimal("0")) for month in reference_months}
            for product in products
        }
        products_with_history = [
            product for product in products
            if any(quantity > 0 for quantity in reference_by_product[product.pk].values())
        ]
        if not products_with_history:
            raise CommandError("No hay ventas reales en los últimos 12 meses completos para usar como referencia.")

        first_synthetic_month = shift_month(first_real_date.replace(day=1), -months_count)
        last_synthetic_month = shift_month(first_real_date.replace(day=1), -1)
        last_day = (shift_month(last_synthetic_month, 1) - timedelta(days=1))
        overlap = real_batches.filter(first_date__lte=last_day, last_date__gte=first_synthetic_month).exists()
        if overlap:
            raise CommandError("El período calculado coincide con un historial real. No se escribió ningún dato para evitar mezclar fuentes.")

        randomizer = random.Random(seed)
        generated_rows = []
        row_number = 1
        discrete_units = {"unidad", "unidades", "pieza", "piezas", "ud", "uds"}
        for product in products_with_history:
            monthly_reference = reference_by_product[product.pk]
            for month in (shift_month(first_synthetic_month, offset) for offset in range(months_count)):
                baseline = monthly_reference.get(month.month, Decimal("0"))
                if baseline <= 0:
                    continue
                variation = Decimal(str(randomizer.uniform(0.82, 1.18)))
                quantity = baseline * variation
                if product.unit.strip().casefold() in discrete_units:
                    quantity = Decimal(max(1, int(quantity.to_integral_value()))).quantize(Decimal("0.001"))
                else:
                    quantity = quantity.quantize(Decimal("0.001"))
                generated_rows.append({
                    "row_number": row_number,
                    "product": product,
                    "sold_on": month.replace(day=15),
                    "quantity": quantity,
                })
                row_number += 1

        if not generated_rows:
            raise CommandError("No se pudieron crear meses con demanda de referencia distinta de cero.")

        actor = get_user_model().objects.filter(is_active=True, is_superuser=True).order_by("pk").first()
        if actor is None:
            raise CommandError("Se necesita un superusuario activo para registrar la importación simulada en la auditoría.")

        source_hash = hashlib.sha256(f"matriplast-ml-demo:{months_count}:{seed}".encode()).hexdigest()
        batch = SalesHistoryImport.objects.create(
            filename=f"SIMULADO_ML_{months_count}_MESES_SEMILLA_{seed}.csv",
            file_sha256=source_hash,
            row_count=len(generated_rows),
            first_date=first_synthetic_month,
            last_date=last_day,
            imported_by=actor,
            is_simulated=True,
        )
        HistoricalSale.objects.bulk_create([
            HistoricalSale(batch=batch, **row) for row in generated_rows
        ])
        forecast_result = generate_monthly_forecasts()
        AuditLog.objects.create(
            actor=actor,
            action="IMPORT",
            entity="SalesHistoryImport",
            entity_id=batch.pk,
            details={
                "simulated": True,
                "rows": len(generated_rows),
                "products": len(products_with_history),
                "from": first_synthetic_month.isoformat(),
                "to": last_day.isoformat(),
                "seed": seed,
            },
        )
        self.stdout.write(self.style.SUCCESS(
            f"Simulación agregada: {len(generated_rows)} filas mensuales en {len(products_with_history)} productos, "
            f"del {first_synthetic_month:%m/%Y} al {last_synthetic_month:%m/%Y}. "
            "Se guardó en el historial de entrenamiento, sin crear ventas, pagos ni movimientos de inventario."
        ))
        self.stdout.write(
            f"Pronósticos regenerados: {forecast_result['created']} períodos; "
            f"productos sin historial suficiente: {len(forecast_result['skipped'])}."
        )
