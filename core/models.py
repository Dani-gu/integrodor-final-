from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from decimal import Decimal


class Product(models.Model):
    class Kind(models.TextChoices):
        FINISHED = "FINISHED", "Producto terminado"
        RAW_MATERIAL = "RAW", "Materia prima"

    code = models.CharField("Código", max_length=40, unique=True)
    name = models.CharField("Nombre", max_length=160)
    kind = models.CharField("Tipo", max_length=10, choices=Kind.choices)
    unit = models.CharField("Unidad de medida", max_length=30)
    sale_price = models.DecimalField("Precio de venta (Bs)", max_digits=14, decimal_places=2, null=True, blank=True, default=None)
    current_stock = models.DecimalField("Existencia actual", max_digits=14, decimal_places=3, default=0)
    minimum_stock = models.DecimalField("Stock mínimo", max_digits=14, decimal_places=3, default=0)
    active = models.BooleanField("Activo", default=True)
    created_at = models.DateTimeField("Creado", auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "producto o insumo"
        verbose_name_plural = "productos e insumos"

    def __str__(self):
        return f"{self.code} - {self.name}"


class Client(models.Model):
    name = models.CharField("Nombre o razón social", max_length=180)
    tax_id = models.CharField("NIT o documento", max_length=30, blank=True)
    phone = models.CharField("Teléfono", max_length=40, blank=True)
    email = models.EmailField("Correo electrónico", blank=True)
    address = models.CharField("Dirección", max_length=240, blank=True)
    active = models.BooleanField("Activo", default=True)
    created_at = models.DateTimeField("Creado", auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "cliente"
        verbose_name_plural = "clientes"

    def __str__(self):
        return self.name


class Sale(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Pendiente de pago"
        PARTIAL = "PARTIAL", "Pago parcial"
        PAID = "PAID", "Pagada"
        CANCELLED = "CANCELLED", "Anulada"

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="sales", verbose_name="Cliente")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sales_created", verbose_name="Registrada por")
    sold_at = models.DateTimeField("Fecha de venta", auto_now_add=True)
    recorded_at = models.DateTimeField("Registrada en el sistema", default=timezone.now, editable=False)
    status = models.CharField("Estado", max_length=12, choices=Status.choices, default=Status.OPEN)
    notes = models.TextField("Observaciones", blank=True)

    class Meta:
        ordering = ["-sold_at"]
        verbose_name = "venta"
        verbose_name_plural = "ventas"

    @property
    def total(self):
        return sum((line.quantity * line.unit_price for line in self.items.all()), start=0)

    @property
    def amount_paid(self):
        return sum((payment.amount for payment in self.payments.all()), start=0)

    @property
    def balance(self):
        if self.status == self.Status.CANCELLED:
            return Decimal("0.00")
        return self.total - self.amount_paid

    def __str__(self):
        return f"Venta {self.pk} - {self.client}"


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items", verbose_name="Venta")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sale_items", verbose_name="Producto")
    quantity = models.DecimalField("Cantidad", max_digits=14, decimal_places=3, validators=[MinValueValidator(0.001)])
    unit_price = models.DecimalField("Precio unitario (Bs)", max_digits=14, decimal_places=2, validators=[MinValueValidator(0)])

    class Meta:
        verbose_name = "detalle de venta"
        verbose_name_plural = "detalles de venta"

    def __str__(self):
        return f"{self.product} x {self.quantity}"

    @property
    def line_total(self):
        return self.quantity * self.unit_price


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "CASH", "Efectivo"
        TRANSFER = "TRANSFER", "Transferencia bancaria"
        QR = "QR", "QR"

    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="payments", verbose_name="Venta")
    amount = models.DecimalField("Monto (Bs)", max_digits=14, decimal_places=2, validators=[MinValueValidator(0.01)])
    method = models.CharField("Forma de pago", max_length=10, choices=Method.choices)
    paid_at = models.DateTimeField("Fecha y hora", auto_now_add=True)
    reference = models.CharField("Referencia", max_length=100, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments_recorded", verbose_name="Registrado por")

    class Meta:
        ordering = ["-paid_at"]
        verbose_name = "pago"
        verbose_name_plural = "pagos"


class InventoryMovement(models.Model):
    class Kind(models.TextChoices):
        PURCHASE = "PURCHASE", "Ingreso por compra"
        PRODUCTION = "PRODUCTION", "Ingreso por producción"
        SALE = "SALE", "Salida por venta"
        SALE_VOID = "SALE_VOID", "Reingreso por anulación"
        CONSUMPTION = "CONSUMPTION", "Consumo en producción"
        ADJUSTMENT = "ADJUSTMENT", "Ajuste"
        CORRECTION_IN = "CORR_IN", "Ajuste de entrada"
        CORRECTION_OUT = "CORR_OUT", "Ajuste de salida"

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="movements", verbose_name="Producto o insumo")
    kind = models.CharField("Tipo de movimiento", max_length=12, choices=Kind.choices)
    quantity = models.DecimalField("Cantidad", max_digits=14, decimal_places=3, validators=[MinValueValidator(0.001)])
    stock_before = models.DecimalField("Existencia anterior", max_digits=14, decimal_places=3, null=True, blank=True)
    stock_after = models.DecimalField("Existencia resultante", max_digits=14, decimal_places=3, null=True, blank=True)
    occurred_at = models.DateTimeField("Fecha y hora", auto_now_add=True)
    sale = models.ForeignKey(Sale, null=True, blank=True, on_delete=models.PROTECT, related_name="inventory_movements", verbose_name="Venta relacionada")
    production_lot = models.ForeignKey("ProductionLot", null=True, blank=True, on_delete=models.PROTECT, related_name="inventory_movements", verbose_name="Lote relacionado")
    note = models.CharField("Detalle", max_length=240, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="inventory_movements_recorded", verbose_name="Registrado por")

    class Meta:
        ordering = ["-occurred_at"]
        verbose_name = "movimiento de inventario"
        verbose_name_plural = "movimientos de inventario"


class Machine(models.Model):
    class Status(models.TextChoices):
        OPERATIONAL = "OPERATIONAL", "Operativa"
        MAINTENANCE = "MAINTENANCE", "En mantenimiento"
        OUT_OF_SERVICE = "OUT_OF_SERVICE", "Fuera de servicio"

    name = models.CharField("Nombre de máquina", max_length=140, unique=True)
    status = models.CharField("Estado", max_length=20, choices=Status.choices, default=Status.OPERATIONAL)
    materials = models.ManyToManyField(Product, blank=True, related_name="machines", verbose_name="Materias primas habituales", limit_choices_to={"kind": "RAW"})
    notes = models.CharField("Observaciones", max_length=240, blank=True)
    created_at = models.DateTimeField("Registrada", auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "máquina"
        verbose_name_plural = "máquinas"

    def __str__(self):
        return self.name


class ProductionLot(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="production_lots", verbose_name="Producto terminado")
    machine = models.ForeignKey(Machine, null=True, blank=True, on_delete=models.PROTECT, related_name="production_lots", verbose_name="Máquina utilizada")
    planned_quantity = models.DecimalField("Cantidad planificada", max_digits=14, decimal_places=3, validators=[MinValueValidator(0.001)])
    produced_quantity = models.DecimalField("Cantidad producida", max_digits=14, decimal_places=3, null=True, blank=True, validators=[MinValueValidator(0)])
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="production_lots", verbose_name="Operario responsable")
    started_at = models.DateTimeField("Inicio", null=True, blank=True)
    finished_at = models.DateTimeField("Fin", null=True, blank=True)
    notes = models.TextField("Observaciones", blank=True)
    created_at = models.DateTimeField("Registrado", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "lote de producción"
        verbose_name_plural = "lotes de producción"

    def __str__(self):
        return f"Lote {self.pk} - {self.product}"

    @property
    def production_variance(self):
        if self.produced_quantity is None:
            return None
        return self.produced_quantity - self.planned_quantity

    @property
    def plan_attainment_percentage(self):
        if self.produced_quantity is None or not self.planned_quantity:
            return None
        return self.produced_quantity / self.planned_quantity * Decimal("100")

    @property
    def production_shortfall(self):
        if self.production_variance is not None and self.production_variance < 0:
            return abs(self.production_variance)
        return None

    @property
    def production_excess(self):
        if self.production_variance is not None and self.production_variance > 0:
            return self.production_variance
        return None


class ProductionConsumption(models.Model):
    lot = models.ForeignKey(ProductionLot, on_delete=models.CASCADE, related_name="consumptions", verbose_name="Lote de producción")
    material = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="production_consumptions", verbose_name="Materia prima")
    quantity = models.DecimalField("Cantidad consumida", max_digits=14, decimal_places=3, validators=[MinValueValidator(0.001)])

    class Meta:
        ordering = ["material__name"]
        constraints = [models.UniqueConstraint(fields=("lot", "material"), name="uniq_lot_consumed_material")]
        verbose_name = "consumo de materia prima"
        verbose_name_plural = "consumos de materias primas"


class RecipeIngredient(models.Model):
    finished_product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="recipe_ingredients", verbose_name="Producto terminado")
    material = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="recipe_uses", verbose_name="Materia prima")
    quantity_per_unit = models.DecimalField(
        "Cantidad por unidad producida",
        max_digits=16,
        decimal_places=6,
        validators=[MinValueValidator(Decimal("0.000001"))],
    )

    class Meta:
        ordering = ["material__name"]
        constraints = [models.UniqueConstraint(fields=("finished_product", "material"), name="uniq_recipe_product_material")]
        verbose_name = "ingrediente de receta"
        verbose_name_plural = "ingredientes de receta"

    def __str__(self):
        return f"{self.material} — {self.quantity_per_unit} por unidad de {self.finished_product}"


class PurchaseExpense(models.Model):
    class Kind(models.TextChoices):
        RAW_MATERIAL = "RAW", "Compra de materia prima"
        OTHER = "OTHER", "Otro gasto"

    kind = models.CharField("Tipo", max_length=8, choices=Kind.choices)
    description = models.CharField("Descripción", max_length=200)
    amount = models.DecimalField("Monto (Bs)", max_digits=14, decimal_places=2, validators=[MinValueValidator(0.01)])
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.PROTECT, related_name="purchases", verbose_name="Materia prima")
    quantity = models.DecimalField("Cantidad", max_digits=14, decimal_places=3, null=True, blank=True, validators=[MinValueValidator(0.001)])
    occurred_at = models.DateTimeField("Fecha", auto_now_add=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="expenses_recorded", verbose_name="Registrado por")

    class Meta:
        ordering = ["-occurred_at"]
        verbose_name = "compra o gasto"
        verbose_name_plural = "compras y gastos"


class SalesForecast(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="forecasts", verbose_name="Producto")
    period = models.DateField("Mes pronosticado")
    predicted_quantity = models.DecimalField("Cantidad proyectada", max_digits=14, decimal_places=3)
    model_name = models.CharField("Modelo", max_length=100)
    error_metrics = models.JSONField("Métricas de error", default=dict, blank=True)
    generated_at = models.DateTimeField("Generado", auto_now_add=True)

    class Meta:
        ordering = ["-period", "product__name"]
        verbose_name = "pronóstico de venta"
        verbose_name_plural = "pronósticos de ventas"


class SalesHistoryImport(models.Model):
    filename = models.CharField("Archivo", max_length=255)
    is_simulated = models.BooleanField("Datos simulados", default=False)
    file_sha256 = models.CharField("Huella SHA-256", max_length=64, unique=True)
    row_count = models.PositiveIntegerField("Filas importadas")
    first_date = models.DateField("Fecha inicial")
    last_date = models.DateField("Fecha final")
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sales_history_imports", verbose_name="Importado por")
    imported_at = models.DateTimeField("Fecha de importación", auto_now_add=True)

    class Meta:
        ordering = ["-imported_at"]
        verbose_name = "importación de historial de ventas"
        verbose_name_plural = "importaciones de historial de ventas"


class HistoricalSale(models.Model):
    batch = models.ForeignKey(SalesHistoryImport, on_delete=models.CASCADE, related_name="rows", verbose_name="Importación")
    row_number = models.PositiveIntegerField("Fila del archivo")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="historical_sales", verbose_name="Producto")
    client = models.ForeignKey(Client, null=True, blank=True, on_delete=models.PROTECT, related_name="historical_sales", verbose_name="Cliente")
    sold_on = models.DateField("Fecha de venta")
    quantity = models.DecimalField("Cantidad", max_digits=14, decimal_places=3, validators=[MinValueValidator(0.001)])
    unit_price = models.DecimalField("Precio unitario (Bs)", max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])

    class Meta:
        ordering = ["sold_on", "product__name"]
        constraints = [models.UniqueConstraint(fields=("batch", "row_number"), name="uniq_history_batch_row")]
        verbose_name = "venta histórica importada"
        verbose_name_plural = "ventas históricas importadas"


class CustomerOrder(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pendiente"
        CONVERTED = "CONVERTED", "Convertido a venta"
        CANCELLED = "CANCELLED", "Cancelado"

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="orders", verbose_name="Cliente")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="customer_orders", verbose_name="Registrado por")
    created_at = models.DateTimeField("Fecha de registro", auto_now_add=True)
    status = models.CharField("Estado", max_length=12, choices=Status.choices, default=Status.PENDING)
    notes = models.TextField("Observaciones", blank=True)
    sale = models.OneToOneField(Sale, null=True, blank=True, on_delete=models.PROTECT, related_name="source_order", verbose_name="Venta generada")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "pedido de cliente"
        verbose_name_plural = "pedidos de clientes"

    @property
    def total(self):
        return sum((line.quantity * line.unit_price for line in self.items.all()), start=Decimal("0.00"))

    def __str__(self):
        return f"Pedido {self.pk} - {self.client}"


class CustomerOrderItem(models.Model):
    order = models.ForeignKey(CustomerOrder, on_delete=models.CASCADE, related_name="items", verbose_name="Pedido")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="customer_order_items", verbose_name="Producto")
    quantity = models.DecimalField("Cantidad", max_digits=14, decimal_places=3, validators=[MinValueValidator(0.001)])
    unit_price = models.DecimalField("Precio unitario (Bs)", max_digits=14, decimal_places=2, validators=[MinValueValidator(0)])

    class Meta:
        verbose_name = "detalle de pedido"
        verbose_name_plural = "detalles de pedido"

    @property
    def line_total(self):
        return self.quantity * self.unit_price


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_logs", verbose_name="Usuario")
    action = models.CharField("Acción", max_length=40)
    entity = models.CharField("Registro afectado", max_length=100)
    entity_id = models.CharField("ID del registro", max_length=64, blank=True)
    details = models.JSONField("Detalles", default=dict, blank=True)
    ip_address = models.GenericIPAddressField("Dirección IP", null=True, blank=True)
    created_at = models.DateTimeField("Fecha y hora", auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "log del sistema"
        verbose_name_plural = "logs del sistema"

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} - {self.action} - {self.entity}"
