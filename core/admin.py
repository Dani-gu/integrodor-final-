from django.contrib import admin

from .models import (
    AuditLog,
    Client,
    CustomerOrder,
    CustomerOrderItem,
    HistoricalSale,
    InventoryMovement,
    Machine,
    Payment,
    Product,
    ProductionLot,
    ProductionConsumption,
    RecipeIngredient,
    PurchaseExpense,
    Sale,
    SaleItem,
    SalesForecast,
    SalesHistoryImport,
)


class ReadOnlyModelAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Product)
class ProductAdmin(ReadOnlyModelAdmin):
    list_display = ("code", "name", "kind", "unit", "sale_price", "current_stock", "minimum_stock", "active")
    list_filter = ("kind", "active")
    search_fields = ("code", "name")


@admin.register(Client)
class ClientAdmin(ReadOnlyModelAdmin):
    list_display = ("name", "tax_id", "phone", "email", "active")
    search_fields = ("name", "tax_id", "phone", "email")
    list_filter = ("active",)


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        opts = self.model._meta
        return request.user.has_perm(f"{opts.app_label}.view_{opts.model_name}")


class SaleItemInline(ReadOnlyInline):
    model = SaleItem
    readonly_fields = ("product", "quantity", "unit_price")


class PaymentInline(ReadOnlyInline):
    model = Payment
    readonly_fields = ("amount", "method", "paid_at", "reference", "recorded_by")


class CustomerOrderItemInline(ReadOnlyInline):
    model = CustomerOrderItem
    readonly_fields = ("product", "quantity", "unit_price")


@admin.register(CustomerOrder)
class CustomerOrderAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "client", "created_at", "status", "created_by", "sale")
    list_filter = ("status", "created_at")
    search_fields = ("client__name", "client__tax_id")
    inlines = (CustomerOrderItemInline,)
    readonly_fields = ("client", "created_by", "created_at", "status", "notes", "sale")


@admin.register(Sale)
class SaleAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "client", "sold_at", "status", "created_by")
    list_filter = ("status", "sold_at")
    search_fields = ("client__name", "client__tax_id")
    inlines = (SaleItemInline, PaymentInline)
    readonly_fields = ("client", "created_by", "sold_at", "status", "notes")


@admin.register(InventoryMovement)
class InventoryMovementAdmin(ReadOnlyModelAdmin):
    list_display = ("product", "kind", "quantity", "occurred_at", "recorded_by")
    list_filter = ("kind", "occurred_at")
    search_fields = ("product__name", "product__code", "note")
    readonly_fields = ("product", "kind", "quantity", "occurred_at", "sale", "note", "recorded_by")


@admin.register(ProductionLot)
class ProductionLotAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "product", "machine", "planned_quantity", "produced_quantity", "operator", "started_at", "finished_at")
    list_filter = ("started_at", "finished_at")
    search_fields = ("product__name", "operator__username")
    readonly_fields = ("product", "machine", "planned_quantity", "produced_quantity", "operator", "started_at", "finished_at", "notes", "created_at")


@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "notes")


@admin.register(ProductionConsumption)
class ProductionConsumptionAdmin(ReadOnlyModelAdmin):
    list_display = ("lot", "material", "quantity")
    list_filter = ("material__kind",)
    search_fields = ("material__name", "material__code")
    readonly_fields = ("lot", "material", "quantity")


@admin.register(RecipeIngredient)
class RecipeIngredientAdmin(ReadOnlyModelAdmin):
    list_display = ("finished_product", "material", "quantity_per_unit")
    search_fields = ("finished_product__name", "material__name", "material__code")
    readonly_fields = ("finished_product", "material", "quantity_per_unit")


@admin.register(PurchaseExpense)
class PurchaseExpenseAdmin(ReadOnlyModelAdmin):
    list_display = ("description", "kind", "amount", "product", "occurred_at", "recorded_by")
    list_filter = ("kind", "occurred_at")
    search_fields = ("description", "product__name")


@admin.register(SalesForecast)
class SalesForecastAdmin(ReadOnlyModelAdmin):
    list_display = ("product", "period", "predicted_quantity", "model_name", "generated_at")
    list_filter = ("period", "model_name")
    search_fields = ("product__name", "model_name")


@admin.register(SalesHistoryImport)
class SalesHistoryImportAdmin(ReadOnlyModelAdmin):
    list_display = ("filename", "row_count", "first_date", "last_date", "imported_at", "imported_by")
    search_fields = ("filename", "imported_by__username")
    list_filter = ("imported_at",)


@admin.register(HistoricalSale)
class HistoricalSaleAdmin(ReadOnlyModelAdmin):
    list_display = ("sold_on", "product", "client", "quantity", "unit_price", "batch")
    list_filter = ("sold_on", "product")
    search_fields = ("product__code", "product__name", "client__name", "client__tax_id")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "entity", "entity_id", "ip_address")
    list_filter = ("action", "entity", "created_at")
    search_fields = ("actor__username", "entity", "entity_id", "action")
    readonly_fields = ("actor", "action", "entity", "entity_id", "details", "ip_address", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
