import csv
import io
import zipfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from xml.etree import ElementTree as ET

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import CharField, Count, DecimalField, ExpressionWrapper, F, Max, Prefetch, Q, Sum
from django.db.models.functions import Cast, TruncMonth
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date
from django.utils import timezone

from .access import INVENTORY_ROLES, MANAGEMENT_ROLES, PRODUCTION_ROLES, REPORT_ROLES, SALES_ROLES, roles_required
from .forms import (
    ClientForm,
    ClientUpdateForm,
    CancelSaleForm,
    CustomerOrderHeaderForm,
    FinishLotForm,
    MachineForm,
    InternalUserCreateForm,
    InternalUserUpdateForm,
    InventoryAdjustmentForm,
    OtherExpenseForm,
    PaymentForm,
    ProductForm,
    ProductUpdateForm,
    ProductionLotForm,
    ProductionConsumptionFormSet,
    RecipeIngredientFormSet,
    PurchaseForm,
    SaleHeaderForm,
    SaleLineFormSet,
    SalesHistoryUploadForm,
)
from .forecasting import FORECAST_HORIZON, MIN_HISTORY_MONTHS, actual_sales_by_month, generate_monthly_forecasts
from .history_imports import import_sales_history
from .models import AuditLog, Client, CustomerOrder, InventoryMovement, Machine, Payment, Product, ProductionLot, PurchaseExpense, RecipeIngredient, Sale, SaleItem, SalesForecast, SalesHistoryImport
from .services import (
    create_sale,
    cancel_unpaid_sale,
    cancel_customer_order,
    convert_customer_order_to_sale,
    create_customer_order,
    adjust_inventory,
    finish_production_lot,
    record_payment,
    register_other_expense,
    register_purchase,
    start_production_lot,
)


@roles_required(*("Administrador", "Gerente", "Vendedor", "Almacenero", "Supervisor", "Operario", "Contador"))
def dashboard(request):
    roles = set(request.user.groups.values_list("name", flat=True))
    if request.user.is_superuser:
        roles.update(("Administrador", "Gerente", "Vendedor", "Almacenero", "Supervisor", "Operario", "Contador"))
    can_view_sales = bool(roles & {*SALES_ROLES, "Contador"})
    can_view_inventory = bool(roles & set(INVENTORY_ROLES))
    can_view_production = bool(roles & set(PRODUCTION_ROLES))
    pending_orders = CustomerOrder.objects.none()
    recent_sales = Sale.objects.none()
    if can_view_sales:
        pending_orders = CustomerOrder.objects.filter(status=CustomerOrder.Status.PENDING).select_related("client").prefetch_related("items__product")[:6]
        recent_sales = Sale.objects.exclude(status=Sale.Status.CANCELLED).select_related("client").prefetch_related("items").order_by("-sold_at")[:6]
    low_stock_products = Product.objects.none()
    if can_view_inventory:
        low_stock_products = list(Product.objects.filter(active=True, current_stock__lte=F("minimum_stock")).order_by("name")[:6])
        for product in low_stock_products:
            product.reorder_quantity = max(product.minimum_stock - product.current_stock, Decimal("0.000"))
    active_lots = ProductionLot.objects.none()
    if can_view_production:
        active_lots = ProductionLot.objects.filter(started_at__isnull=False, finished_at__isnull=True).select_related("product", "operator").order_by("started_at")[:6]
    pending_orders_count = CustomerOrder.objects.filter(status=CustomerOrder.Status.PENDING).count() if can_view_sales else None
    open_sales_count = Sale.objects.filter(status__in=(Sale.Status.OPEN, Sale.Status.PARTIAL)).count() if can_view_sales else None
    low_stock_count = Product.objects.filter(active=True, current_stock__lte=F("minimum_stock")).count() if can_view_inventory else None
    active_lots_count = ProductionLot.objects.filter(started_at__isnull=False, finished_at__isnull=True).count() if can_view_production else None

    attention_items = []
    if can_view_inventory and low_stock_count:
        attention_items.append({"label": "Productos con stock bajo", "count": low_stock_count, "url": "inventory_list", "query": "?stock=low", "tone": "warning"})
    if can_view_sales and pending_orders_count:
        attention_items.append({"label": "Pedidos pendientes de revisar", "count": pending_orders_count, "url": "order_list", "query": "", "tone": "warning"})
    if can_view_sales and open_sales_count:
        attention_items.append({"label": "Ventas con saldo pendiente", "count": open_sales_count, "url": "sale_list", "query": "", "tone": "info"})
    if can_view_production and active_lots_count:
        attention_items.append({"label": "Lotes que siguen en proceso", "count": active_lots_count, "url": "production_list", "query": "?estado=in_progress", "tone": "info"})

    chart_months = _last_months(timezone.localdate().replace(day=1), 6)
    chart_start = chart_months[0]
    chart_end = (timezone.localdate().replace(day=28) + timedelta(days=4)).replace(day=1)
    line_amount = ExpressionWrapper(F("quantity") * F("unit_price"), output_field=DecimalField(max_digits=20, decimal_places=2))
    monthly_sales = {
        _month_key(row["month"]): float(row["total"] or 0)
        for row in SaleItem.objects.filter(sale__sold_at__date__gte=chart_start, sale__sold_at__date__lt=chart_end)
        .exclude(sale__status=Sale.Status.CANCELLED)
        .annotate(month=TruncMonth("sale__sold_at")).values("month").annotate(total=Sum(line_amount))
    } if can_view_sales else {}
    monthly_payments = {
        _month_key(row["month"]): float(row["total"] or 0)
        for row in Payment.objects.filter(paid_at__date__gte=chart_start, paid_at__date__lt=chart_end)
        .annotate(month=TruncMonth("paid_at")).values("month").annotate(total=Sum("amount"))
    } if can_view_sales else {}
    dashboard_chart = {
        "labels": [month.strftime("%m/%y") for month in chart_months],
        "unit": "Bs",
        "series": [
            {"label": "Ventas registradas", "values": [monthly_sales.get(_month_key(month), 0) for month in chart_months], "color": "#75b82a"},
            {"label": "Pagos recibidos", "values": [monthly_payments.get(_month_key(month), 0) for month in chart_months], "color": "#159ab0"},
        ] if can_view_sales else [],
    }
    context = {
        "product_count": Product.objects.filter(active=True).count(),
        "client_count": Client.objects.filter(active=True).count() if can_view_sales else None,
        "open_sales_count": open_sales_count,
        "pending_orders_count": pending_orders_count,
        "low_stock_count": low_stock_count,
        "recent_sales": recent_sales,
        "pending_orders": pending_orders,
        "low_stock_products": low_stock_products,
        "active_lots_count": active_lots_count,
        "active_lots": active_lots,
        "attention_items": attention_items,
        "attention_count": sum(item["count"] for item in attention_items),
        "dashboard_chart": dashboard_chart,
        "can_view_sales": can_view_sales,
        "can_view_inventory": can_view_inventory,
        "can_view_production": can_view_production,
        "today": timezone.localdate(),
    }
    return render(request, "core/dashboard.html", context)


@roles_required(*("Administrador", "Gerente", "Vendedor", "Almacenero", "Supervisor", "Operario", "Contador"))
def system_guide(request):
    return render(request, "core/system_guide.html")


@roles_required(*INVENTORY_ROLES)
def product_list(request):
    products = Product.objects.annotate(recipe_ingredient_count=Count("recipe_ingredients"))
    query = request.GET.get("q", "").strip()
    kind = request.GET.get("tipo", "").strip()
    status = request.GET.get("estado", "").strip()
    stock_status = request.GET.get("stock", "").strip()
    if query:
        products = products.filter(Q(name__icontains=query) | Q(code__icontains=query))
    if kind in Product.Kind.values:
        products = products.filter(kind=kind)
    if status == "active":
        products = products.filter(active=True)
    elif status == "inactive":
        products = products.filter(active=False)
    if stock_status == "low":
        products = products.filter(current_stock__lte=F("minimum_stock"))
        if status != "inactive":
            products = products.filter(active=True)
    metric_products = Product.objects.all()
    if kind in Product.Kind.values:
        metric_products = metric_products.filter(kind=kind)
    page_obj = Paginator(products.order_by("kind", "name", "pk"), 25).get_page(request.GET.get("page"))
    for product in page_obj.object_list:
        product.reorder_quantity = max(product.minimum_stock - product.current_stock, Decimal("0.000"))
    raw_low_stock_products = Product.objects.filter(
        active=True,
        kind=Product.Kind.RAW_MATERIAL,
        current_stock__lte=F("minimum_stock"),
    ).order_by("name")
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(request, "core/product_list.html", {
        "products": page_obj.object_list,
        "page_obj": page_obj,
        "query_string": query_params.urlencode(),
        "query": query,
        "kind": kind,
        "status": status,
        "stock_status": stock_status,
        "active_count": metric_products.filter(active=True).count(),
        "inactive_count": metric_products.filter(active=False).count(),
        "low_stock_count": metric_products.filter(active=True, current_stock__lte=F("minimum_stock")).count(),
        "raw_low_stock_count": raw_low_stock_products.count(),
        "raw_low_stock_products": raw_low_stock_products[:8],
    })


@roles_required(*INVENTORY_ROLES)
@transaction.atomic
def product_create(request):
    form = ProductForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        product = form.save()
        if product.current_stock > 0:
            InventoryMovement.objects.create(
                product=product,
                kind=InventoryMovement.Kind.ADJUSTMENT,
                quantity=product.current_stock,
                stock_before=Decimal("0"),
                stock_after=product.current_stock,
                note="Saldo inicial al crear el producto",
                recorded_by=request.user,
            )
        AuditLog.objects.create(
            actor=request.user,
            action="CREATE",
            entity="Product",
            entity_id=product.pk,
            details={"code": product.code, "initial_stock": str(product.current_stock)},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, "Producto o insumo registrado.")
        return redirect("product_list")
    return render(request, "core/form_page.html", {"form": form, "title": "Nuevo producto o insumo", "back_url": "product_list"})


@roles_required(*INVENTORY_ROLES)
def product_update(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    before = {"code": product.code, "name": product.name, "kind": product.kind, "unit": product.unit, "sale_price": str(product.sale_price), "minimum_stock": str(product.minimum_stock), "active": product.active}
    form = ProductUpdateForm(request.POST or None, instance=product)
    if request.method == "POST" and form.is_valid():
        updated_product = form.save()
        AuditLog.objects.create(
            actor=request.user,
            action="UPDATE",
            entity="Product",
            entity_id=updated_product.pk,
            details={"before": before, "after": {"code": updated_product.code, "name": updated_product.name, "kind": updated_product.kind, "unit": updated_product.unit, "sale_price": str(updated_product.sale_price), "minimum_stock": str(updated_product.minimum_stock), "active": updated_product.active}},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, "Producto o insumo actualizado.")
        return redirect("product_list")
    return render(request, "core/form_page.html", {"form": form, "title": f"Editar {product.name}", "back_url": "product_list", "product": product})


@roles_required(*INVENTORY_ROLES)
@transaction.atomic
def product_recipe(request, product_id):
    product = get_object_or_404(Product, pk=product_id, kind=Product.Kind.FINISHED)
    formset = RecipeIngredientFormSet(request.POST or None, instance=product, prefix="recipe")
    material_stock_data = {
        str(material.pk): {"stock": str(material.current_stock), "unit": material.unit}
        for material in Product.objects.filter(kind=Product.Kind.RAW_MATERIAL).only("id", "current_stock", "unit")
    }
    if request.method == "POST" and formset.is_valid():
        formset.save()
        ingredient_count = RecipeIngredient.objects.filter(finished_product=product).count()
        AuditLog.objects.create(
            actor=request.user,
            action="UPDATE",
            entity="ProductRecipe",
            entity_id=product.pk,
            details={"ingredient_count": ingredient_count},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, "Receta del producto guardada.")
        return redirect("product_list")
    return render(request, "core/product_recipe.html", {
        "product": product,
        "formset": formset,
        "material_stock_data": material_stock_data,
    })


@roles_required(*SALES_ROLES)
def client_list(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("estado", "").strip()
    clients = Client.objects.all()
    if query:
        clients = clients.filter(
            Q(name__icontains=query)
            | Q(tax_id__icontains=query)
            | Q(phone__icontains=query)
            | Q(email__icontains=query)
        )
    if status == "active":
        clients = clients.filter(active=True)
    elif status == "inactive":
        clients = clients.filter(active=False)
    page_obj = Paginator(clients.order_by("name", "pk"), 25).get_page(request.GET.get("page"))
    client_page = list(page_obj.object_list)
    client_ids = [client.pk for client in client_page]
    line_total = ExpressionWrapper(
        F("quantity") * F("unit_price"),
        output_field=DecimalField(max_digits=22, decimal_places=2),
    )
    client_sales_totals = {
        row["sale__client_id"]: row["total"] or Decimal("0.00")
        for row in SaleItem.objects.filter(sale__client_id__in=client_ids)
        .exclude(sale__status=Sale.Status.CANCELLED)
        .values("sale__client_id")
        .annotate(total=Sum(line_total))
    }
    client_payment_totals = {
        row["sale__client_id"]: row["total"] or Decimal("0.00")
        for row in Payment.objects.filter(sale__client_id__in=client_ids)
        .exclude(sale__status=Sale.Status.CANCELLED)
        .values("sale__client_id")
        .annotate(total=Sum("amount"))
    }
    for client in client_page:
        client.balance_due = max(
            client_sales_totals.get(client.pk, Decimal("0.00"))
            - client_payment_totals.get(client.pk, Decimal("0.00")),
            Decimal("0.00"),
        )
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(request, "core/client_list.html", {
        "clients": client_page,
        "page_obj": page_obj,
        "query_string": query_params.urlencode(),
        "query": query,
        "status": status,
        "active_count": Client.objects.filter(active=True).count(),
        "inactive_count": Client.objects.filter(active=False).count(),
    })


@roles_required(*SALES_ROLES)
def client_detail(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    sales = Sale.objects.filter(client=client).exclude(status=Sale.Status.CANCELLED)
    line_total = ExpressionWrapper(
        F("quantity") * F("unit_price"),
        output_field=DecimalField(max_digits=22, decimal_places=2),
    )
    sold_total = SaleItem.objects.filter(sale__in=sales).aggregate(total=Sum(line_total))["total"] or Decimal("0.00")
    paid_total = Payment.objects.filter(sale__in=sales).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    sales_count = sales.count()
    pending_count = sales.filter(status__in=(Sale.Status.OPEN, Sale.Status.PARTIAL)).count()
    page_obj = Paginator(
        sales.select_related("created_by").prefetch_related("items", "payments").order_by("-sold_at"),
        15,
    ).get_page(request.GET.get("page"))
    recent_payments = Payment.objects.filter(sale__client=client).exclude(
        sale__status=Sale.Status.CANCELLED
    ).select_related("sale", "recorded_by").order_by("-paid_at", "-pk")[:10]
    recent_orders = CustomerOrder.objects.filter(client=client).select_related("created_by", "sale").prefetch_related("items__product")[:5]
    return render(request, "core/client_detail.html", {
        "client": client,
        "sales": page_obj.object_list,
        "page_obj": page_obj,
        "sales_count": sales_count,
        "sold_total": sold_total,
        "paid_total": paid_total,
        "balance_total": max(sold_total - paid_total, Decimal("0.00")),
        "pending_count": pending_count,
        "recent_payments": recent_payments,
        "recent_orders": recent_orders,
    })


@roles_required(*SALES_ROLES)
def client_create(request):
    form = ClientForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        client = form.save()
        AuditLog.objects.create(actor=request.user, action="CREATE", entity="Client", entity_id=client.pk, ip_address=request.META.get("REMOTE_ADDR"))
        messages.success(request, "Cliente registrado.")
        return redirect("client_list")
    return render(request, "core/form_page.html", {"form": form, "title": "Nuevo cliente", "back_url": "client_list"})


@roles_required(*SALES_ROLES)
def client_update(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    before = {"name": client.name, "tax_id": client.tax_id, "phone": client.phone, "email": client.email, "address": client.address, "active": client.active}
    form = ClientUpdateForm(request.POST or None, instance=client)
    if request.method == "POST" and form.is_valid():
        updated_client = form.save()
        AuditLog.objects.create(
            actor=request.user,
            action="UPDATE",
            entity="Client",
            entity_id=updated_client.pk,
            details={"before": before, "after": {"name": updated_client.name, "tax_id": updated_client.tax_id, "phone": updated_client.phone, "email": updated_client.email, "address": updated_client.address, "active": updated_client.active}},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, "Cliente actualizado.")
        return redirect("client_list")
    return render(request, "core/form_page.html", {"form": form, "title": f"Editar cliente {client.name}", "back_url": "client_list"})


@roles_required(*SALES_ROLES)
def sale_list(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("estado", "").strip()
    date_from = parse_date(request.GET.get("desde", "").strip())
    date_to = parse_date(request.GET.get("hasta", "").strip())
    if date_from and date_to and date_to < date_from:
        messages.error(request, "La fecha final no puede ser anterior a la fecha inicial.")
        date_from = date_to = None
    sales = Sale.objects.select_related("client", "created_by").prefetch_related("items", "payments")
    if query:
        search_filter = Q(client__name__icontains=query) | Q(items__product__name__icontains=query)
        if query.isdigit():
            search_filter |= Q(pk=int(query))
        sales = sales.filter(search_filter).distinct()
    if status in Sale.Status.values:
        sales = sales.filter(status=status)
    if date_from:
        sales = sales.filter(sold_at__date__gte=date_from)
    if date_to:
        sales = sales.filter(sold_at__date__lte=date_to)
    sales_count = sales.count()
    financial_sales = sales.exclude(status=Sale.Status.CANCELLED)
    sale_line_total = ExpressionWrapper(
        F("quantity") * F("unit_price"),
        output_field=DecimalField(max_digits=22, decimal_places=2),
    )
    sold_total = SaleItem.objects.filter(sale__in=financial_sales).aggregate(total=Sum(sale_line_total))["total"] or Decimal("0.00")
    paid_total = Payment.objects.filter(sale__in=financial_sales).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    balance_total = max(sold_total - paid_total, Decimal("0.00"))
    sales = sales.order_by("-sold_at")
    page_obj = Paginator(sales, 25).get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(request, "core/sale_list.html", {
        "sales": page_obj.object_list,
        "page_obj": page_obj,
        "query_string": query_params.urlencode(),
        "query": query,
        "status": status,
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
        "sale_statuses": Sale.Status.choices,
        "sales_count": sales_count,
        "sold_total": sold_total,
        "paid_total": paid_total,
        "balance_total": balance_total,
    })


@roles_required(*SALES_ROLES)
def order_list(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("estado", "").strip()
    orders = CustomerOrder.objects.select_related("client", "created_by", "sale").prefetch_related("items__product")
    if query:
        search_filter = Q(client__name__icontains=query) | Q(notes__icontains=query) | Q(items__product__name__icontains=query)
        if query.isdigit():
            search_filter |= Q(pk=int(query))
        orders = orders.filter(search_filter).distinct()
    if status in CustomerOrder.Status.values:
        orders = orders.filter(status=status)
    orders = orders.order_by("-created_at")
    page_obj = Paginator(orders, 25).get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(request, "core/order_list.html", {
        "orders": page_obj.object_list,
        "page_obj": page_obj,
        "query_string": query_params.urlencode(),
        "query": query,
        "status": status,
        "order_statuses": CustomerOrder.Status.choices,
    })


@roles_required(*SALES_ROLES)
def order_create(request):
    client_id = request.GET.get("cliente", "").strip()
    initial = {"client": client_id} if client_id.isdigit() else None
    form = CustomerOrderHeaderForm(request.POST or None, initial=initial)
    line_formset = SaleLineFormSet(request.POST or None, prefix="lines")
    if request.method == "POST" and form.is_valid() and line_formset.is_valid():
        lines = [
            {"product_id": row.cleaned_data["product"].pk, "quantity": row.cleaned_data["quantity"], "unit_price": row.cleaned_data["unit_price"]}
            for row in line_formset.forms if row.cleaned_data and not row.cleaned_data.get("DELETE")
        ]
        try:
            order = create_customer_order(
                actor=request.user,
                client_id=form.cleaned_data["client"].pk,
                lines=lines,
                notes=form.cleaned_data["notes"],
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, "Pedido registrado. El stock se verificó, pero no se descontó ni reservó.")
            return redirect("order_list")
        except (ValidationError, Client.DoesNotExist, Product.DoesNotExist) as exc:
            form.add_error(None, exc.messages[0] if hasattr(exc, "messages") else str(exc))
    return render(request, "core/order_form.html", {"form": form, "line_formset": line_formset, "product_prices": _sale_price_map()})


@roles_required(*SALES_ROLES)
def order_convert(request, order_id):
    if request.method == "POST":
        try:
            sale = convert_customer_order_to_sale(actor=request.user, order_id=order_id, ip_address=request.META.get("REMOTE_ADDR"))
            messages.success(request, f"Pedido convertido en venta #{sale.pk}; el stock fue descontado.")
            return redirect("sale_detail", sale_id=sale.pk)
        except (ValidationError, CustomerOrder.DoesNotExist, Client.DoesNotExist, Product.DoesNotExist) as exc:
            messages.error(request, exc.messages[0] if hasattr(exc, "messages") else "No se pudo convertir el pedido.")
    return redirect("order_list")


@roles_required(*SALES_ROLES)
def order_cancel(request, order_id):
    if request.method == "POST":
        try:
            cancel_customer_order(actor=request.user, order_id=order_id, ip_address=request.META.get("REMOTE_ADDR"))
            messages.success(request, "Pedido cancelado; no se modificó el inventario.")
        except (ValidationError, CustomerOrder.DoesNotExist) as exc:
            messages.error(request, exc.messages[0] if hasattr(exc, "messages") else "No se encontró el pedido.")
    return redirect("order_list")


@roles_required(*SALES_ROLES)
def sale_create(request):
    client_id = request.GET.get("cliente", "").strip()
    initial = {"client": client_id} if client_id.isdigit() else None
    header = SaleHeaderForm(request.POST or None, initial=initial)
    line_formset = SaleLineFormSet(request.POST or None, prefix="lines")
    if request.method == "POST" and header.is_valid() and line_formset.is_valid():
        lines = [
            {"product_id": f.cleaned_data["product"].pk, "quantity": f.cleaned_data["quantity"], "unit_price": f.cleaned_data["unit_price"]}
            for f in line_formset.forms
            if f.cleaned_data and not f.cleaned_data.get("DELETE")
        ]
        payment_amount = header.cleaned_data["initial_payment"]
        payments = []
        if payment_amount > 0:
            if not header.cleaned_data["payment_method"]:
                header.add_error("payment_method", "Selecciona la forma de pago.")
                return render(request, "core/sale_form.html", {"form": header, "line_formset": line_formset, "product_prices": _sale_price_map()})
            payments.append({
                "amount": payment_amount,
                "method": header.cleaned_data["payment_method"],
                "reference": header.cleaned_data["payment_reference"],
            })
        try:
            sale = create_sale(
                actor=request.user,
                client_id=header.cleaned_data["client"].pk,
                lines=lines,
                payments=payments,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            sale.notes = header.cleaned_data["notes"]
            sale.save(update_fields=["notes"])
            messages.success(request, "Venta registrada y existencias actualizadas.")
            return redirect("sale_detail", sale_id=sale.pk)
        except (ValidationError, Product.DoesNotExist) as exc:
            header.add_error(None, exc.messages[0] if hasattr(exc, "messages") else str(exc))
    return render(request, "core/sale_form.html", {"form": header, "line_formset": line_formset, "product_prices": _sale_price_map()})


def _sale_price_map():
    return {str(product_id): str(price) for product_id, price in Product.objects.filter(kind=Product.Kind.FINISHED, active=True, sale_price__isnull=False).values_list("pk", "sale_price")}


@roles_required(*SALES_ROLES)
def sale_detail(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related("client", "created_by").prefetch_related("items__product", "payments"), pk=sale_id)
    return render(request, "core/sale_detail.html", {"sale": sale})


@roles_required(*MANAGEMENT_ROLES)
def sale_cancel(request, sale_id):
    if request.method != "POST":
        return redirect("sale_detail", sale_id=sale_id)
    sale = get_object_or_404(Sale, pk=sale_id)
    form = CancelSaleForm(request.POST)
    if form.is_valid():
        try:
            cancel_unpaid_sale(actor=request.user, sale_id=sale.pk, reason=form.cleaned_data["reason"], ip_address=request.META.get("REMOTE_ADDR"))
            messages.success(request, "Venta anulada y productos devueltos al inventario.")
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
    else:
        messages.error(request, "Escribe el motivo de anulación.")
    return redirect("sale_detail", sale_id=sale.pk)


@roles_required(*SALES_ROLES)
def sale_receipt(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related("client").prefetch_related("items__product", "payments"), pk=sale_id)
    return render(request, "core/receipt.html", {"sale": sale})


@roles_required(*SALES_ROLES)
def payment_add(request, sale_id):
    sale = get_object_or_404(Sale, pk=sale_id)
    form = PaymentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            record_payment(
                actor=request.user,
                sale_id=sale.pk,
                amount=form.cleaned_data["amount"],
                method=form.cleaned_data["method"],
                reference=form.cleaned_data["reference"],
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, "Pago registrado.")
            return redirect("sale_detail", sale_id=sale.pk)
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
    return render(request, "core/form_page.html", {"form": form, "title": f"Registrar pago de venta {sale.pk}", "back_url": "sale_detail", "back_args": [sale.pk]})


@roles_required(*INVENTORY_ROLES)
def inventory_list(request):
    query = request.GET.get("q", "").strip()
    kind = request.GET.get("tipo", "").strip()
    movement_kind = request.GET.get("movimiento", "").strip()
    stock_status = request.GET.get("stock", "").strip()
    products = Product.objects.filter(active=True)
    movements = InventoryMovement.objects.select_related("product", "recorded_by")
    if query:
        product_filter = Q(name__icontains=query) | Q(code__icontains=query)
        products = products.filter(product_filter)
        movements = movements.filter(Q(product__name__icontains=query) | Q(product__code__icontains=query))
    if kind in Product.Kind.values:
        products = products.filter(kind=kind)
        movements = movements.filter(product__kind=kind)
    if stock_status == "low":
        products = products.filter(current_stock__lte=F("minimum_stock"))
    if movement_kind in InventoryMovement.Kind.values:
        movements = movements.filter(kind=movement_kind)
    product_page_obj = Paginator(products.order_by("kind", "name"), 25).get_page(request.GET.get("producto_pagina"))
    for product in product_page_obj.object_list:
        product.reorder_quantity = max(product.minimum_stock - product.current_stock, Decimal("0.000"))
    movement_page_obj = Paginator(movements.order_by("-occurred_at"), 30).get_page(request.GET.get("movimiento_pagina"))
    product_query_params = request.GET.copy()
    product_query_params.pop("producto_pagina", None)
    product_query_params.pop("movimiento_pagina", None)
    movement_query_params = request.GET.copy()
    movement_query_params.pop("movimiento_pagina", None)
    movement_query_params.pop("producto_pagina", None)
    return render(request, "core/inventory_list.html", {
        "products": product_page_obj.object_list,
        "product_page_obj": product_page_obj,
        "movements": movement_page_obj.object_list,
        "movement_page_obj": movement_page_obj,
        "query": query,
        "kind": kind,
        "movement_kind": movement_kind,
        "stock_status": stock_status,
        "product_kinds": Product.Kind.choices,
        "movement_kinds": InventoryMovement.Kind.choices,
        "product_query_string": product_query_params.urlencode(),
        "movement_query_string": movement_query_params.urlencode(),
    })


@roles_required(*INVENTORY_ROLES)
def purchase_create(request):
    product_id = request.GET.get("producto", "").strip()
    initial = {"product": product_id} if product_id.isdigit() else {}
    suggested_quantity = request.GET.get("cantidad", "").strip()
    if suggested_quantity:
        try:
            quantity = Decimal(suggested_quantity)
            if quantity.is_finite() and quantity > 0:
                initial["quantity"] = quantity
        except InvalidOperation:
            pass
    form = PurchaseForm(request.POST or None, initial=initial)
    if "quantity" in initial:
        form.fields["quantity"].help_text = "Cantidad sugerida para alcanzar el stock mínimo; puedes modificarla según la compra real."
    form.fields["amount"].help_text = "Ingresa el costo total real de esta compra en bolivianos."
    if request.method == "POST" and form.is_valid():
        try:
            register_purchase(
                actor=request.user,
                product_id=form.cleaned_data["product"].pk,
                quantity=form.cleaned_data["quantity"],
                amount=form.cleaned_data["amount"],
                description=form.cleaned_data["description"],
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, "Compra registrada y stock actualizado.")
            return redirect("inventory_list")
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
    return render(request, "core/form_page.html", {"form": form, "title": "Registrar compra de materia prima", "back_url": "inventory_list"})


@roles_required(*INVENTORY_ROLES)
def inventory_adjust(request):
    product_id = request.GET.get("producto", "").strip()
    initial = {"product": product_id} if product_id.isdigit() else None
    form = InventoryAdjustmentForm(request.POST or None, initial=initial)
    product_stock_data = {
        str(product.pk): {"stock": str(product.current_stock), "unit": product.unit}
        for product in Product.objects.filter(active=True).only("id", "current_stock", "unit")
    }
    form.fields["quantity"].help_text = "Ingresa la cantidad del ajuste. Revisa el saldo resultante antes de guardar."
    form.fields["direction"].help_text = "Aumentar suma unidades; reducir las descuenta de la existencia actual."
    if request.method == "POST" and form.is_valid():
        try:
            adjust_inventory(
                actor=request.user,
                product_id=form.cleaned_data["product"].pk,
                direction=form.cleaned_data["direction"],
                quantity=form.cleaned_data["quantity"],
                reason=form.cleaned_data["reason"],
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, "Ajuste aplicado y registrado en los movimientos.")
            return redirect("inventory_list")
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
    return render(request, "core/form_page.html", {
        "form": form,
        "title": "Ajuste de inventario",
        "back_url": "inventory_list",
        "inventory_adjustment": True,
        "product_stock_data": product_stock_data,
    })


@roles_required(*("Administrador", "Gerente", "Contador"))
def expense_create(request):
    form = OtherExpenseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        register_other_expense(actor=request.user, description=form.cleaned_data["description"], amount=form.cleaned_data["amount"], ip_address=request.META.get("REMOTE_ADDR"))
        messages.success(request, "Gasto registrado.")
        return redirect("reports")
    return render(request, "core/form_page.html", {"form": form, "title": "Registrar otro gasto", "back_url": "reports"})


@roles_required(*PRODUCTION_ROLES)
def production_list(request):
    lots = ProductionLot.objects.select_related("product", "operator", "machine").prefetch_related("consumptions__material").order_by("-created_at")
    query = request.GET.get("q", "").strip()
    status = request.GET.get("estado", "").strip()
    if query:
        search = Q(product__name__icontains=query) | Q(machine__name__icontains=query) | Q(operator__username__icontains=query) | Q(operator__first_name__icontains=query) | Q(operator__last_name__icontains=query)
        if query.isdigit():
            search |= Q(pk=int(query))
        lots = lots.filter(search)
    if status == "pending":
        lots = lots.filter(started_at__isnull=True, finished_at__isnull=True)
    elif status == "in_progress":
        lots = lots.filter(started_at__isnull=False, finished_at__isnull=True)
    elif status == "finished":
        lots = lots.filter(finished_at__isnull=False)
    page_obj = Paginator(lots, 20).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "core/production_list.html", {
        "lots": page_obj.object_list,
        "page_obj": page_obj,
        "query": query,
        "status": status,
        "query_string": params.urlencode(),
        "pending_count": ProductionLot.objects.filter(started_at__isnull=True, finished_at__isnull=True).count(),
        "in_progress_count": ProductionLot.objects.filter(started_at__isnull=False, finished_at__isnull=True).count(),
        "finished_count": ProductionLot.objects.filter(finished_at__isnull=False).count(),
    })


@roles_required(*PRODUCTION_ROLES)
def production_lot_detail(request, lot_id):
    lot = get_object_or_404(
        ProductionLot.objects.select_related("product", "operator", "machine").prefetch_related(
            "consumptions__material", "inventory_movements__product", "inventory_movements__recorded_by"
        ),
        pk=lot_id,
    )
    if lot.finished_at:
        status_label, status_class = "Finalizado", "success"
    elif lot.started_at:
        status_label, status_class = "En proceso", "info"
    else:
        status_label, status_class = "Pendiente", "warning"
    return render(request, "core/production_lot_detail.html", {
        "lot": lot,
        "status_label": status_label,
        "status_class": status_class,
        "inventory_movements": lot.inventory_movements.all(),
    })


@roles_required(*PRODUCTION_ROLES)
def machine_list(request):
    machines = Machine.objects.prefetch_related("materials").order_by("name")
    return render(request, "core/machine_list.html", {
        "machines": machines,
        "operational_count": machines.filter(status=Machine.Status.OPERATIONAL).count(),
        "maintenance_count": machines.filter(status=Machine.Status.MAINTENANCE).count(),
        "out_of_service_count": machines.filter(status=Machine.Status.OUT_OF_SERVICE).count(),
    })


@roles_required(*PRODUCTION_ROLES)
def machine_create(request):
    form = MachineForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        machine = form.save()
        AuditLog.objects.create(actor=request.user, action="CREATE", entity="Machine", entity_id=machine.pk, details={"name": machine.name, "status": machine.status, "materials": list(machine.materials.values_list("name", flat=True))}, ip_address=request.META.get("REMOTE_ADDR"))
        messages.success(request, "Máquina registrada.")
        return redirect("machine_list")
    return render(request, "core/form_page.html", {"form": form, "title": "Registrar máquina", "back_url": "machine_list"})


@roles_required(*PRODUCTION_ROLES)
def machine_update(request, machine_id):
    machine = get_object_or_404(Machine, pk=machine_id)
    before = {
        "name": machine.name,
        "status": machine.status,
        "notes": machine.notes,
        "materials": list(machine.materials.order_by("pk").values_list("pk", flat=True)),
    }
    form = MachineForm(request.POST or None, instance=machine)
    if request.method == "POST" and form.is_valid():
        updated_machine = form.save()
        after = {
            "name": updated_machine.name,
            "status": updated_machine.status,
            "notes": updated_machine.notes,
            "materials": list(updated_machine.materials.order_by("pk").values_list("pk", flat=True)),
        }
        AuditLog.objects.create(actor=request.user, action="UPDATE", entity="Machine", entity_id=updated_machine.pk, details={"before": before, "after": after}, ip_address=request.META.get("REMOTE_ADDR"))
        messages.success(request, "Máquina actualizada.")
        return redirect("machine_list")
    return render(request, "core/form_page.html", {"form": form, "title": f"Editar máquina {machine.name}", "back_url": "machine_list"})


@roles_required(*PRODUCTION_ROLES)
def production_create(request):
    product_id = request.GET.get("producto", "").strip()
    initial = {}
    if product_id.isdigit():
        initial["product"] = product_id
    raw_quantity = request.GET.get("cantidad", "").strip()
    if raw_quantity:
        try:
            suggested_quantity = Decimal(raw_quantity)
            if suggested_quantity.is_finite() and suggested_quantity > 0:
                initial["planned_quantity"] = suggested_quantity
        except InvalidOperation:
            pass
    form = ProductionLotForm(request.POST or None, initial=initial or None)
    if request.method == "POST" and form.is_valid():
        lot = form.save()
        AuditLog.objects.create(
            actor=request.user,
            action="CREATE",
            entity="ProductionLot",
            entity_id=lot.pk,
            details={"product_id": lot.product_id, "machine_id": lot.machine_id, "planned_quantity": str(lot.planned_quantity)},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, "Lote de producción creado.")
        return redirect("production_list")
    return render(request, "core/production_lot_form.html", {
        "form": form,
        "title": "Nuevo lote de producción",
        "back_url": "production_list",
        **_production_planning_context(),
    })


def _production_planning_context(include_product_id=None):
    recipe_products = Product.objects.filter(kind=Product.Kind.FINISHED, active=True)
    if include_product_id:
        recipe_products = Product.objects.filter(kind=Product.Kind.FINISHED).filter(Q(active=True) | Q(pk=include_product_id))
    recipe_products = recipe_products.prefetch_related(
        Prefetch(
            "recipe_ingredients",
            queryset=RecipeIngredient.objects.filter(material__active=True).select_related("material").order_by("material__name"),
        )
    )
    recipe_data = {}
    for product in recipe_products:
        recipe_data[str(product.pk)] = {
            "name": product.name,
            "unit": product.unit,
            "materials": [
                {
                    "name": ingredient.material.name,
                    "unit": ingredient.material.unit,
                    "rate": str(ingredient.quantity_per_unit),
                    "stock": str(ingredient.material.current_stock),
                }
                for ingredient in product.recipe_ingredients.all()
            ],
        }
    machine_data = {
        str(machine.pk): {
            "name": machine.name,
            "materials": [material.name for material in machine.materials.all()],
        }
        for machine in Machine.objects.prefetch_related(
            Prefetch("materials", queryset=Product.objects.filter(active=True).order_by("name"))
        )
    }
    return {"recipe_data": recipe_data, "machine_data": machine_data}


@roles_required(*PRODUCTION_ROLES)
@transaction.atomic
def production_update(request, lot_id):
    lot = get_object_or_404(ProductionLot.objects.select_related("product", "operator", "machine"), pk=lot_id)
    if lot.started_at or lot.finished_at:
        messages.error(request, "Solo puedes editar un lote que todavía no se haya iniciado.")
        return redirect("production_lot_detail", lot_id=lot.pk)
    before = {
        "product_id": lot.product_id,
        "planned_quantity": str(lot.planned_quantity),
        "operator_id": lot.operator_id,
        "machine_id": lot.machine_id,
        "notes": lot.notes,
    }
    form = ProductionLotForm(request.POST or None, instance=lot)
    if request.method == "POST" and form.is_valid():
        updated = form.save()
        AuditLog.objects.create(
            actor=request.user,
            action="UPDATE",
            entity="ProductionLot",
            entity_id=updated.pk,
            details={"before": before, "after": {
                "product_id": updated.product_id,
                "planned_quantity": str(updated.planned_quantity),
                "operator_id": updated.operator_id,
                "machine_id": updated.machine_id,
                "notes": updated.notes,
            }},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, "Lote pendiente actualizado.")
        return redirect("production_lot_detail", lot_id=updated.pk)
    return render(request, "core/production_lot_form.html", {
        "form": form,
        "title": f"Editar lote #{lot.pk:06d}",
        "back_url": "production_lot_detail",
        "back_args": [lot.pk],
        **_production_planning_context(include_product_id=lot.product_id),
    })


@roles_required(*PRODUCTION_ROLES)
def production_start(request, lot_id):
    if request.method != "POST":
        return redirect("production_list")
    try:
        start_production_lot(actor=request.user, lot_id=lot_id, ip_address=request.META.get("REMOTE_ADDR"))
        messages.success(request, "Lote iniciado.")
    except (ValidationError, ProductionLot.DoesNotExist) as exc:
        messages.error(request, exc.messages[0] if hasattr(exc, "messages") else "No se encontró el lote.")
    return redirect("production_list")


@roles_required(*PRODUCTION_ROLES)
def production_finish(request, lot_id):
    lot = get_object_or_404(ProductionLot.objects.select_related("product", "machine"), pk=lot_id)
    form = FinishLotForm(request.POST or None, planned_quantity=lot.planned_quantity)
    recipe_rows = list(
        lot.product.recipe_ingredients.filter(material__active=True)
        .select_related("material")
        .order_by("material__name")
    )
    recipe_rates = {str(row.material_id): str(row.quantity_per_unit) for row in recipe_rows}
    initial_material_ids = set()
    if request.method != "POST":
        if lot.machine_id:
            initial_material_ids.update(lot.machine.materials.filter(active=True).values_list("pk", flat=True))
        initial_material_ids.update(row.material_id for row in recipe_rows)
    initial_materials = Product.objects.filter(pk__in=initial_material_ids).order_by("name")
    initial_consumptions = [{"material": material.pk} for material in initial_materials]
    material_stock = {
        str(item.pk): {"name": item.name, "stock": str(item.current_stock), "unit": item.unit}
        for item in Product.objects.filter(kind=Product.Kind.RAW_MATERIAL, active=True).order_by("name")
    }
    consumption_formset = ProductionConsumptionFormSet(
        request.POST or None,
        prefix="consumptions",
        initial=initial_consumptions,
    )
    if request.method == "POST" and form.is_valid() and consumption_formset.is_valid():
        consumptions = [
            {"material_id": row.cleaned_data["material"].pk, "quantity": row.cleaned_data["quantity"]}
            for row in consumption_formset.forms
            if row.cleaned_data and not row.cleaned_data.get("DELETE") and row.cleaned_data.get("material") and row.cleaned_data.get("quantity") is not None
        ]
        try:
            finish_production_lot(
                actor=request.user,
                lot_id=lot.pk,
                produced_quantity=form.cleaned_data["produced_quantity"],
                consumptions=consumptions,
                confirm_overproduction=form.cleaned_data["confirm_overproduction"],
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, "Lote finalizado: producto ingresado y materias primas consumidas descontadas del stock.")
            return redirect("production_list")
        except ValidationError as exc:
            form.add_error(None, exc.messages[0])
    return render(request, "core/production_finish.html", {"form": form, "consumption_formset": consumption_formset, "lot": lot, "recipe_rates": recipe_rates, "material_stock": material_stock})


@roles_required(*REPORT_ROLES)
def reports(request):
    today = timezone.localdate()
    month_start = today.replace(day=1)
    month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    current_sales = list(
        Sale.objects.filter(sold_at__date__gte=month_start, sold_at__date__lt=month_end)
        .exclude(status=Sale.Status.CANCELLED)
        .prefetch_related("items")
    )
    return render(request, "core/reports.html", {
        "active_users": get_user_model().objects.filter(is_active=True).count(),
        "active_lots": ProductionLot.objects.filter(started_at__isnull=False, finished_at__isnull=True).count(),
        "low_stock_count": Product.objects.filter(active=True, current_stock__lte=F("minimum_stock")).count(),
        "monthly_sales": sum((sale.total for sale in current_sales), Decimal("0.00")),
        "production_available": request.user.is_superuser or request.user.groups.filter(name__in=PRODUCTION_ROLES).exists(),
        "inventory_available": request.user.is_superuser or request.user.groups.filter(name__in=INVENTORY_ROLES).exists(),
    })


@roles_required(*REPORT_ROLES)
def product_cost_report(request):
    """Estimate direct raw-material cost and gross margin from recorded purchases."""
    product_choices = Product.objects.filter(kind=Product.Kind.FINISHED, active=True).order_by("name")
    product_id = request.GET.get("producto", "").strip()
    selected_product = product_choices.filter(pk=product_id).first() if product_id.isdigit() else None
    products = product_choices.filter(pk=selected_product.pk) if selected_product else product_choices

    purchase_costs = {}
    purchase_rows = (
        PurchaseExpense.objects.filter(
            kind=PurchaseExpense.Kind.RAW_MATERIAL,
            product__kind=Product.Kind.RAW_MATERIAL,
            quantity__gt=0,
        )
        .values("product_id")
        .annotate(total_quantity=Sum("quantity"), total_amount=Sum("amount"))
    )
    for row in purchase_rows:
        if row["total_quantity"]:
            purchase_costs[row["product_id"]] = row["total_amount"] / row["total_quantity"]

    products = products.prefetch_related(
        Prefetch(
            "recipe_ingredients",
            queryset=RecipeIngredient.objects.select_related("material").order_by("material__name"),
        )
    )
    cost_rows = []
    complete_cost_count = 0
    missing_recipe_count = 0
    missing_cost_count = 0
    for product in products:
        ingredients = list(product.recipe_ingredients.all())
        material_costs = []
        has_unknown_cost = False
        unit_cost = Decimal("0")
        for ingredient in ingredients:
            material_unit_cost = purchase_costs.get(ingredient.material_id)
            line_cost = ingredient.quantity_per_unit * material_unit_cost if material_unit_cost is not None else None
            if line_cost is None:
                has_unknown_cost = True
            else:
                unit_cost += line_cost
            material_costs.append({
                "name": ingredient.material.name,
                "quantity": ingredient.quantity_per_unit,
                "unit": ingredient.material.unit,
                "purchase_unit_cost": material_unit_cost,
                "line_cost": line_cost,
            })

        if not ingredients:
            status = "no_recipe"
            missing_recipe_count += 1
            unit_cost = None
        elif has_unknown_cost:
            status = "missing_cost"
            missing_cost_count += 1
            unit_cost = None
        else:
            status = "complete"
            complete_cost_count += 1
            unit_cost = unit_cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

        gross_profit = None
        margin_percent = None
        if unit_cost is not None and product.sale_price is not None:
            gross_profit = product.sale_price - unit_cost
            if product.sale_price > 0:
                margin_percent = (gross_profit / product.sale_price * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cost_rows.append({
            "product": product,
            "status": status,
            "material_costs": material_costs,
            "unit_cost": unit_cost,
            "gross_profit": gross_profit,
            "margin_percent": margin_percent,
        })

    export = request.GET.get("export")
    if export in {"csv", "xlsx"}:
        export_rows = []
        for item in cost_rows:
            product = item["product"]
            export_rows.append((
                product.code,
                product.name,
                product.unit,
                item["unit_cost"] if item["unit_cost"] is not None else "",
                product.sale_price if product.sale_price is not None else "",
                item["gross_profit"] if item["gross_profit"] is not None else "",
                item["margin_percent"] if item["margin_percent"] is not None else "",
                {"complete": "Completo", "missing_cost": "Falta costo de compra", "no_recipe": "Sin receta"}[item["status"]],
            ))
        headers = ("Código", "Producto", "Unidad", "Costo directo estimado Bs/unidad", "Precio de venta Bs", "Margen bruto estimado Bs/unidad", "Margen bruto %", "Estado de datos")
        if export == "xlsx":
            return _xlsx_download("costos_y_rentabilidad.xlsx", headers, export_rows)
        return _csv_download("costos_y_rentabilidad.csv", headers, export_rows)

    query_params = request.GET.copy()
    query_params.pop("export", None)
    return render(request, "core/product_cost_report.html", {
        "products": product_choices,
        "selected_product": selected_product,
        "cost_rows": cost_rows,
        "product_count": len(cost_rows),
        "complete_cost_count": complete_cost_count,
        "missing_cost_count": missing_cost_count,
        "missing_recipe_count": missing_recipe_count,
        "query_string": query_params.urlencode(),
    })


def _month_key(value):
    if hasattr(value, "date"):
        value = value.date()
    return value.replace(day=1)


def _last_months(end_month, count):
    months = []
    current = end_month
    for _ in range(count):
        months.append(current)
        current = (current - timedelta(days=1)).replace(day=1)
    return list(reversed(months))


def _csv_download(filename, headers, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow(headers)
    writer.writerows(rows)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _xlsx_download(filename, headers, rows):
    """Create a small formatted Excel workbook without an extra package dependency."""
    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    content_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    ET.register_namespace("", main_ns)
    ET.register_namespace("r", office_rel_ns)

    def tag(namespace, name):
        return f"{{{namespace}}}{name}"

    def xml_bytes(root):
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def column_name(index):
        result = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            result = chr(65 + remainder) + result
        return result

    safe_headers = tuple(str(value) for value in headers)
    data_rows = list(rows)
    last_column = column_name(max(1, len(safe_headers)))
    last_row = max(1, len(data_rows) + 1)

    worksheet = ET.Element(tag(main_ns, "worksheet"))
    ET.SubElement(worksheet, tag(main_ns, "dimension"), {"ref": f"A1:{last_column}{last_row}"})
    views = ET.SubElement(worksheet, tag(main_ns, "sheetViews"))
    sheet_view = ET.SubElement(views, tag(main_ns, "sheetView"), {"workbookViewId": "0"})
    ET.SubElement(sheet_view, tag(main_ns, "pane"), {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"})
    ET.SubElement(sheet_view, tag(main_ns, "selection"), {"pane": "bottomLeft", "activeCell": "A2", "sqref": "A2"})
    sheet_format = ET.SubElement(worksheet, tag(main_ns, "sheetFormatPr"), {"defaultRowHeight": "18"})
    cols = ET.SubElement(worksheet, tag(main_ns, "cols"))
    for index, heading in enumerate(safe_headers, start=1):
        width = max(12, min(38, len(heading) + 3))
        ET.SubElement(cols, tag(main_ns, "col"), {"min": str(index), "max": str(index), "width": str(width), "customWidth": "1"})
    sheet_data = ET.SubElement(worksheet, tag(main_ns, "sheetData"))

    def write_row(row_index, values, header=False):
        row = ET.SubElement(sheet_data, tag(main_ns, "row"), {"r": str(row_index)})
        for column_index, value in enumerate(values, start=1):
            if value is None or value == "":
                continue
            cell = ET.SubElement(row, tag(main_ns, "c"), {"r": f"{column_name(column_index)}{row_index}"})
            if header:
                cell.set("s", "1")
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                ET.SubElement(cell, tag(main_ns, "v")).text = str(value)
            elif isinstance(value, bool):
                cell.set("t", "b")
                ET.SubElement(cell, tag(main_ns, "v")).text = "1" if value else "0"
            else:
                cell.set("t", "inlineStr")
                inline = ET.SubElement(cell, tag(main_ns, "is"))
                text_node = ET.SubElement(inline, tag(main_ns, "t"), {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
                text_node.text = str(value)
        return row

    write_row(1, safe_headers, header=True)
    for row_index, values in enumerate(data_rows, start=2):
        write_row(row_index, values)
    ET.SubElement(worksheet, tag(main_ns, "autoFilter"), {"ref": f"A1:{last_column}{last_row}"})
    ET.SubElement(worksheet, tag(main_ns, "pageMargins"), {"left": "0.3", "right": "0.3", "top": "0.5", "bottom": "0.5", "header": "0.2", "footer": "0.2"})

    workbook = ET.Element(tag(main_ns, "workbook"))
    sheets = ET.SubElement(workbook, tag(main_ns, "sheets"))
    ET.SubElement(sheets, tag(main_ns, "sheet"), {"name": "Reporte", "sheetId": "1", tag(office_rel_ns, "id"): "rId1"})
    workbook_rels = ET.Element(tag(package_rel_ns, "Relationships"))
    ET.SubElement(workbook_rels, tag(package_rel_ns, "Relationship"), {"Id": "rId1", "Type": f"{office_rel_ns}/worksheet", "Target": "worksheets/sheet1.xml"})
    ET.SubElement(workbook_rels, tag(package_rel_ns, "Relationship"), {"Id": "rId2", "Type": f"{office_rel_ns}/styles", "Target": "styles.xml"})
    root_rels = ET.Element(tag(package_rel_ns, "Relationships"))
    ET.SubElement(root_rels, tag(package_rel_ns, "Relationship"), {"Id": "rId1", "Type": f"{office_rel_ns}/officeDocument", "Target": "xl/workbook.xml"})

    styles = ET.Element(tag(main_ns, "styleSheet"))
    fonts = ET.SubElement(styles, tag(main_ns, "fonts"), {"count": "2"})
    ET.SubElement(fonts, tag(main_ns, "font"))
    header_font = ET.SubElement(fonts, tag(main_ns, "font"))
    ET.SubElement(header_font, tag(main_ns, "bold"))
    ET.SubElement(header_font, tag(main_ns, "color"), {"rgb": "FFFFFFFF"})
    fills = ET.SubElement(styles, tag(main_ns, "fills"), {"count": "3"})
    ET.SubElement(fills, tag(main_ns, "fill")).append(ET.Element(tag(main_ns, "patternFill"), {"patternType": "none"}))
    ET.SubElement(fills, tag(main_ns, "fill")).append(ET.Element(tag(main_ns, "patternFill"), {"patternType": "gray125"}))
    green_fill = ET.SubElement(fills, tag(main_ns, "fill"))
    pattern = ET.SubElement(green_fill, tag(main_ns, "patternFill"), {"patternType": "solid"})
    ET.SubElement(pattern, tag(main_ns, "fgColor"), {"rgb": "FF75B82A"})
    ET.SubElement(pattern, tag(main_ns, "bgColor"), {"indexed": "64"})
    borders = ET.SubElement(styles, tag(main_ns, "borders"), {"count": "1"})
    border = ET.SubElement(borders, tag(main_ns, "border"))
    for side in ("left", "right", "top", "bottom", "diagonal"):
        ET.SubElement(border, tag(main_ns, side))
    cell_styles = ET.SubElement(styles, tag(main_ns, "cellStyleXfs"), {"count": "1"})
    ET.SubElement(cell_styles, tag(main_ns, "xf"), {"numFmtId": "0", "fontId": "0", "fillId": "0", "borderId": "0"})
    cell_formats = ET.SubElement(styles, tag(main_ns, "cellXfs"), {"count": "2"})
    ET.SubElement(cell_formats, tag(main_ns, "xf"), {"numFmtId": "0", "fontId": "0", "fillId": "0", "borderId": "0", "xfId": "0"})
    header_format = ET.SubElement(cell_formats, tag(main_ns, "xf"), {"numFmtId": "0", "fontId": "1", "fillId": "2", "borderId": "0", "xfId": "0", "applyFont": "1", "applyFill": "1", "applyAlignment": "1"})
    ET.SubElement(header_format, tag(main_ns, "alignment"), {"vertical": "center", "wrapText": "1"})
    cell_styles = ET.SubElement(styles, tag(main_ns, "cellStyles"), {"count": "1"})
    ET.SubElement(cell_styles, tag(main_ns, "cellStyle"), {"name": "Normal", "xfId": "0", "builtinId": "0"})

    content_types = ET.Element(tag(content_ns, "Types"))
    ET.SubElement(content_types, tag(content_ns, "Default"), {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"})
    ET.SubElement(content_types, tag(content_ns, "Default"), {"Extension": "xml", "ContentType": "application/xml"})
    ET.SubElement(content_types, tag(content_ns, "Override"), {"PartName": "/xl/workbook.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"})
    ET.SubElement(content_types, tag(content_ns, "Override"), {"PartName": "/xl/worksheets/sheet1.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"})
    ET.SubElement(content_types, tag(content_ns, "Override"), {"PartName": "/xl/styles.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"})

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", xml_bytes(content_types))
        archive.writestr("_rels/.rels", xml_bytes(root_rels))
        archive.writestr("xl/workbook.xml", xml_bytes(workbook))
        archive.writestr("xl/_rels/workbook.xml.rels", xml_bytes(workbook_rels))
        archive.writestr("xl/styles.xml", xml_bytes(styles))
        archive.writestr("xl/worksheets/sheet1.xml", xml_bytes(worksheet))
    response = HttpResponse(output.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@roles_required(*REPORT_ROLES)
def sales_report(request):
    today = timezone.localdate()
    date_from = parse_date(request.GET.get("desde", "")) or today.replace(day=1)
    date_to = parse_date(request.GET.get("hasta", "")) or today
    if date_to < date_from:
        messages.error(request, "La fecha final no puede ser anterior a la fecha inicial.")
        date_from, date_to = today.replace(day=1), today
    product_choices = Product.objects.filter(kind=Product.Kind.FINISHED, active=True).order_by("name")
    product_id = request.GET.get("producto", "").strip()
    selected_product = product_choices.filter(pk=product_id).first() if product_id.isdigit() else None
    sale_items = SaleItem.objects.select_related("product")
    if selected_product:
        sale_items = sale_items.filter(product=selected_product)
    sales_query = Sale.objects.filter(
        sold_at__date__gte=date_from,
        sold_at__date__lte=date_to,
    ).exclude(status=Sale.Status.CANCELLED).select_related("client").prefetch_related(
        Prefetch("items", queryset=sale_items), "payments"
    )
    if selected_product:
        sales_query = sales_query.filter(items__product=selected_product).distinct()
    sales = list(sales_query.order_by("-sold_at"))
    for sale in sales:
        sale.report_total = sale.total
        sale.report_quantity = sum((item.quantity for item in sale.items.all()), Decimal("0.000"))

    line_amount = ExpressionWrapper(F("quantity") * F("unit_price"), output_field=DecimalField(max_digits=20, decimal_places=2))
    ranking_items = SaleItem.objects.filter(
        sale__sold_at__date__gte=date_from,
        sale__sold_at__date__lte=date_to,
    ).exclude(sale__status=Sale.Status.CANCELLED)
    if selected_product:
        ranking_items = ranking_items.filter(product=selected_product)
    top_products = list(ranking_items.values("product__name", "product__unit")
        .annotate(units=Sum("quantity"), total=Sum(line_amount), sale_count=Count("sale_id", distinct=True))
        .order_by("-total", "product__name")[:5])
    top_clients = list(ranking_items.values("sale__client__name")
        .annotate(total=Sum(line_amount), sale_count=Count("sale_id", distinct=True))
        .order_by("-total", "sale__client__name")[:5])

    paid = None
    expenses = None
    open_balance = None
    net_total = None
    payment_methods = []
    expense_query = PurchaseExpense.objects.none()
    expense_items = []
    expense_record_count = 0
    if not selected_product:
        period_payments = Payment.objects.filter(paid_at__date__gte=date_from, paid_at__date__lte=date_to)
        paid = period_payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        method_labels = dict(Payment.Method.choices)
        payment_methods = list(
            period_payments.values("method")
            .annotate(total=Sum("amount"), payment_count=Count("pk"))
            .order_by("method")
        )
        for row in payment_methods:
            row["method_label"] = method_labels.get(row["method"], row["method"])
        expense_query = PurchaseExpense.objects.filter(occurred_at__date__gte=date_from, occurred_at__date__lte=date_to).select_related("product", "recorded_by").order_by("-occurred_at")
        expense_record_count = expense_query.count()
        expenses = expense_query.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        expense_items = list(expense_query[:100])
        net_total = paid - expenses
        open_balance = sum((sale.balance for sale in sales), Decimal("0.00"))

    movements_query = InventoryMovement.objects.filter(occurred_at__date__gte=date_from, occurred_at__date__lte=date_to)
    if selected_product:
        movements_query = movements_query.filter(product=selected_product)
    movements = list(
        movements_query.values("kind", "product__unit")
        .annotate(movement_count=Count("id"), quantity=Sum("quantity"))
        .order_by("kind", "product__unit")
    )
    movement_labels = dict(InventoryMovement.Kind.choices)
    for movement in movements:
        movement["kind_label"] = movement_labels[movement["kind"]]

    chart_months = _last_months(date_to.replace(day=1), 12)
    chart_start = chart_months[0]
    chart_end = (date_to.replace(day=28) + timedelta(days=4)).replace(day=1)
    chart_sales_query = SaleItem.objects.filter(
        sale__sold_at__date__gte=chart_start,
        sale__sold_at__date__lt=chart_end,
    ).exclude(sale__status=Sale.Status.CANCELLED)
    if selected_product:
        chart_sales_query = chart_sales_query.filter(product=selected_product)
    sales_by_month = {
        _month_key(row["month"]): float(row["total"] or 0)
        for row in chart_sales_query.annotate(month=TruncMonth("sale__sold_at")).values("month").annotate(total=Sum(line_amount))
    }
    sales_series = [{
        "label": f"Ventas de {selected_product.name}" if selected_product else "Ventas",
        "values": [sales_by_month.get(month, 0) for month in chart_months],
        "color": "#75b82a",
    }]
    if not selected_product:
        payments_by_month = {
            _month_key(row["month"]): float(row["total"] or 0)
            for row in Payment.objects.filter(paid_at__date__gte=chart_start, paid_at__date__lt=chart_end)
            .annotate(month=TruncMonth("paid_at")).values("month").annotate(total=Sum("amount"))
        }
        sales_series.append({"label": "Pagos recibidos", "values": [payments_by_month.get(month, 0) for month in chart_months], "color": "#159ab0"})
    sales_chart = {"labels": [month.strftime("%m/%y") for month in chart_months], "unit": "Bs", "series": sales_series}

    if request.GET.get("export") in ("expenses_csv", "expenses_xlsx"):
        rows = [(
            expense.occurred_at.strftime("%Y-%m-%d %H:%M"),
            expense.get_kind_display(),
            expense.description,
            expense.product.name if expense.product else "",
            expense.quantity if expense.quantity is not None else "",
            expense.product.unit if expense.product and expense.quantity is not None else "",
            expense.amount,
            expense.recorded_by.get_full_name() or expense.recorded_by.username,
        ) for expense in expense_query]
        headers = ("Fecha", "Tipo", "Descripción", "Materia prima", "Cantidad", "Unidad", "Monto Bs", "Registrado por")
        if request.GET.get("export") == "expenses_xlsx":
            return _xlsx_download("detalle_compras_gastos.xlsx", headers, rows)
        return _csv_download("detalle_compras_gastos.csv", headers, rows)

    if request.GET.get("export") in ("csv", "xlsx"):
        rows = []
        for sale in sales:
            if selected_product:
                rows.append((sale.pk, sale.sold_at.strftime("%Y-%m-%d %H:%M"), sale.client.name, selected_product.code, selected_product.name, sale.report_quantity, sale.report_total))
            else:
                rows.append((sale.pk, sale.sold_at.strftime("%Y-%m-%d %H:%M"), sale.client.name, sale.total, sale.amount_paid, sale.balance, sale.get_status_display()))
        headers = ("Venta", "Fecha", "Cliente", "Código producto", "Producto", "Cantidad", "Importe Bs") if selected_product else ("Venta", "Fecha", "Cliente", "Total Bs", "Pagado Bs", "Saldo Bs", "Estado")
        if request.GET.get("export") == "xlsx":
            return _xlsx_download("reporte_ventas.xlsx", headers, rows)
        return _csv_download("reporte_ventas.csv", headers, rows)

    if request.GET.get("export") == "rankings_xlsx":
        rows = [("Producto", row["product__name"], row["units"], row["product__unit"], row["total"], row["sale_count"]) for row in top_products]
        rows += [("Cliente", row["sale__client__name"], "", "", row["total"], row["sale_count"]) for row in top_clients]
        return _xlsx_download("analisis_productos_clientes.xlsx", ("Tipo", "Producto o cliente", "Unidades", "Unidad", "Total vendido Bs", "Ventas"), rows)

    query_params = request.GET.copy()
    query_params.pop("export", None)
    return render(request, "core/sales_report.html", {
        "date_from": date_from,
        "date_to": date_to,
        "selected_product": selected_product,
        "products": product_choices,
        "query_string": query_params.urlencode(),
        "sales_count": len(sales),
        "sales_total": sum((sale.report_total for sale in sales), Decimal("0.00")),
        "unit_count": sum((sale.report_quantity for sale in sales), Decimal("0.000")),
        "paid_total": paid,
        "payment_methods": payment_methods,
        "expense_items": expense_items,
        "expense_record_count": expense_record_count,
        "expense_total": expenses,
        "net_total": net_total,
        "show_full_finance": selected_product is None,
        "sales": sales[:100],
        "open_balance": open_balance,
        "movements": movements,
        "sales_chart": sales_chart,
        "top_products": top_products,
        "top_clients": top_clients,
    })


@roles_required(*INVENTORY_ROLES, *REPORT_ROLES)
def inventory_report(request):
    today = timezone.localdate()
    date_from = parse_date(request.GET.get("desde", "")) or today.replace(day=1)
    date_to = parse_date(request.GET.get("hasta", "")) or today
    if date_to < date_from:
        messages.error(request, "La fecha final no puede ser anterior a la fecha inicial.")
        date_from, date_to = today.replace(day=1), today
    product_choices = Product.objects.filter(active=True).order_by("name")
    product_id = request.GET.get("producto", "").strip()
    selected_product = product_choices.filter(pk=product_id).first() if product_id.isdigit() else None
    kind = request.GET.get("tipo", "").strip()
    stock_status = request.GET.get("stock", "").strip()
    products = product_choices
    if selected_product:
        products = products.filter(pk=selected_product.pk)
    if kind in Product.Kind.values:
        products = products.filter(kind=kind)
    if stock_status == "low":
        products = products.filter(current_stock__lte=F("minimum_stock"))
    low_stock = products.filter(current_stock__lte=F("minimum_stock"))
    movements_query = InventoryMovement.objects.select_related("product", "recorded_by").filter(
        occurred_at__date__gte=date_from,
        occurred_at__date__lte=date_to,
    )
    if selected_product:
        movements_query = movements_query.filter(product=selected_product)
    if kind in Product.Kind.values:
        movements_query = movements_query.filter(product__kind=kind)
    movements = movements_query.order_by("-occurred_at")
    inventory_chart = {}
    inventory_units = list(products.order_by().values_list("unit", flat=True).distinct())
    for unit in inventory_units:
        items = list(products.filter(unit=unit).order_by("name")[:12])
        inventory_chart[unit] = {
            "labels": [product.name[:22] for product in items],
            "unit": unit,
            "series": [
                {"label": f"Stock actual ({unit})", "values": [float(product.current_stock) for product in items], "color": "#159ab0"},
                {"label": f"Mínimo ({unit})", "values": [float(product.minimum_stock) for product in items], "color": "#e4a72f"},
            ],
        }
    export = request.GET.get("export")
    if export in ("stock", "stock_xlsx"):
        headers = ("Código", "Producto o insumo", "Tipo", "Unidad", "Existencia", "Stock mínimo", "Estado")
        rows = ((p.code, p.name, p.get_kind_display(), p.unit, p.current_stock, p.minimum_stock, "Stock bajo" if p.current_stock <= p.minimum_stock else "Disponible") for p in products)
        if export == "stock_xlsx":
            return _xlsx_download("reporte_existencias.xlsx", headers, rows)
        return _csv_download("reporte_existencias.csv", headers, rows)
    if export in ("movements", "movements_xlsx"):
        headers = ("Fecha", "Código", "Producto o insumo", "Movimiento", "Cantidad", "Unidad", "Responsable", "Detalle")
        rows = ((m.occurred_at.strftime("%Y-%m-%d %H:%M"), m.product.code, m.product.name, m.get_kind_display(), m.quantity, m.product.unit, m.recorded_by.get_full_name() or m.recorded_by.username, m.note) for m in movements)
        if export == "movements_xlsx":
            return _xlsx_download("reporte_movimientos.xlsx", headers, rows)
        return _csv_download("reporte_movimientos.csv", headers, rows)
    query_params = request.GET.copy()
    query_params.pop("export", None)
    return render(request, "core/inventory_report.html", {
        "products": products.order_by("kind", "name"),
        "low_stock": low_stock.order_by("kind", "name"),
        "product_count": products.count(),
        "low_stock_count": low_stock.count(),
        "movements": movements[:100],
        "product_choices": product_choices,
        "selected_product": selected_product,
        "kind": kind,
        "stock_status": stock_status,
        "date_from": date_from,
        "date_to": date_to,
        "query_string": query_params.urlencode(),
        "inventory_chart": inventory_chart,
        "inventory_units": inventory_units,
    })


@roles_required(*PRODUCTION_ROLES, *REPORT_ROLES)
def production_report(request):
    today = timezone.localdate()
    default_start = today.replace(day=1)
    month_start = parse_date(request.GET.get("desde", "")) or default_start
    date_to = parse_date(request.GET.get("hasta", "")) or today
    if date_to < month_start:
        messages.error(request, "La fecha final no puede ser anterior a la fecha inicial.")
        month_start, date_to = default_start, today
    product_id = request.GET.get("producto", "").strip()
    products = Product.objects.filter(kind=Product.Kind.FINISHED, active=True).order_by("name")
    selected_product = products.filter(pk=product_id).first() if product_id.isdigit() else None
    month_lots = ProductionLot.objects.filter(created_at__date__gte=month_start, created_at__date__lte=date_to)
    if selected_product:
        month_lots = month_lots.filter(product=selected_product)
    finished = ProductionLot.objects.filter(finished_at__date__gte=month_start, finished_at__date__lte=date_to)
    if selected_product:
        finished = finished.filter(product=selected_product)
    month_lots = month_lots.select_related("product", "operator", "machine").prefetch_related("consumptions__material").order_by("-created_at")
    report_lots = ProductionLot.objects.filter(
        Q(created_at__date__gte=month_start, created_at__date__lte=date_to)
        | Q(finished_at__date__gte=month_start, finished_at__date__lte=date_to)
    )
    if selected_product:
        report_lots = report_lots.filter(product=selected_product)
    report_lots = report_lots.select_related("product", "operator", "machine").prefetch_related("consumptions__material").order_by("-created_at")
    finished_totals = finished.aggregate(planned=Sum("planned_quantity"), produced=Sum("produced_quantity"))
    finished_planned_total = finished_totals["planned"] or Decimal("0.000")
    finished_produced_total = finished_totals["produced"] or Decimal("0.000")
    plan_attainment = (
        finished_produced_total / finished_planned_total * Decimal("100")
        if finished_planned_total
        else None
    )
    production_chart = {}
    production_units = list(month_lots.order_by().values_list("product__unit", flat=True).distinct())
    for unit in production_units:
        rows = list(
            month_lots.filter(product__unit=unit)
            .values("product__name")
            .annotate(planned=Sum("planned_quantity"), produced=Sum("produced_quantity"))
            .order_by("product__name")[:12]
        )
        production_chart[unit] = {
            "labels": [row["product__name"][:22] for row in rows],
            "unit": unit,
            "series": [
                {"label": f"Planificado ({unit})", "values": [float(row["planned"] or 0) for row in rows], "color": "#75b82a"},
                {"label": f"Producido ({unit})", "values": [float(row["produced"] or 0) for row in rows], "color": "#159ab0"},
            ],
        }
    if request.GET.get("export") in ("csv", "xlsx"):
        rows = []
        for lot in report_lots:
            materials = ", ".join(f"{item.material.name}: {item.quantity} {item.material.unit}" for item in lot.consumptions.all())
            rows.append((
                lot.pk,
                lot.created_at.strftime("%Y-%m-%d %H:%M"),
                lot.product.code,
                lot.product.name,
                lot.machine.name if lot.machine else "",
                lot.planned_quantity,
                lot.produced_quantity if lot.produced_quantity is not None else "",
                lot.production_variance if lot.production_variance is not None else "",
                lot.plan_attainment_percentage if lot.plan_attainment_percentage is not None else "",
                lot.product.unit,
                materials,
                lot.operator.get_full_name() or lot.operator.username,
                lot.finished_at.strftime("%Y-%m-%d %H:%M") if lot.finished_at else "",
            ))
        headers = ("Lote", "Creado", "Código producto", "Producto", "Máquina", "Planificado", "Producido", "Variación", "Cumplimiento (%)", "Unidad", "Materias primas consumidas", "Operario", "Finalizado")
        if request.GET.get("export") == "xlsx":
            return _xlsx_download("reporte_produccion.xlsx", headers, rows)
        return _csv_download("reporte_produccion.csv", headers, rows)
    query_params = request.GET.copy()
    query_params.pop("export", None)
    return render(request, "core/production_report.html", {
        "month_start": month_start,
        "date_to": date_to,
        "selected_product": selected_product,
        "products": products,
        "query_string": query_params.urlencode(),
        "lots": report_lots,
        "lot_count": month_lots.count(),
        "finished_count": finished.count(),
        "produced_total": finished_produced_total,
        "plan_attainment": plan_attainment,
        "active_lots": month_lots.filter(started_at__isnull=False, finished_at__isnull=True).count(),
        "production_chart": production_chart,
        "production_units": production_units,
    })


def _forecast_reference_range(forecast):
    margin = forecast.error_metrics.get("MAE")
    if margin is None:
        return None, None
    margin = Decimal(str(margin))
    lower = max(Decimal("0"), forecast.predicted_quantity - margin)
    upper = forecast.predicted_quantity + margin
    if forecast.product.unit.strip().casefold() in {"unidad", "unidades", "pieza", "piezas", "ud", "uds"}:
        lower = lower.to_integral_value(rounding=ROUND_FLOOR)
        upper = upper.to_integral_value(rounding=ROUND_CEILING)
    return lower, upper


def _forecast_display_quantity(forecast):
    if forecast.product.unit.strip().casefold() in {"unidad", "unidades", "pieza", "piezas", "ud", "uds"}:
        return forecast.predicted_quantity.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return forecast.predicted_quantity


@roles_required(*REPORT_ROLES)
def forecasts(request):
    current_month = timezone.localdate().replace(day=1)
    today = timezone.localdate()
    next_month = (current_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_is_closing_today = today == next_month - timedelta(days=1)
    history_end_month = current_month if month_is_closing_today else (current_month - timedelta(days=1)).replace(day=1)
    history_cutoff = next_month if month_is_closing_today else current_month
    can_generate = request.user.is_superuser or request.user.groups.filter(name__in=MANAGEMENT_ROLES).exists()
    latest_run = SalesForecast.objects.filter(period__gt=current_month).aggregate(latest=Max("generated_at"))["latest"]
    latest_sale = Sale.objects.aggregate(latest=Max("recorded_at"))["latest"]
    latest_sale_change = AuditLog.objects.filter(
        entity="Sale", action__in=("CREATE", "CANCEL")
    ).aggregate(latest=Max("created_at"))["latest"]
    latest_history_import = SalesHistoryImport.objects.aggregate(latest=Max("imported_at"))["latest"]
    latest_data_change = max(
        (value for value in (latest_sale, latest_sale_change, latest_history_import) if value),
        default=None,
    )
    source_signature = latest_data_change.isoformat() if latest_data_change else ""
    daily_refresh_due = request.session.get("forecast_checked_on") != today.isoformat()
    source_changed_in_session = request.session.get("forecast_source_checked") != source_signature
    data_changed_after_forecast = latest_data_change and (not latest_run or latest_data_change > latest_run)
    if can_generate and (daily_refresh_due or source_changed_in_session):
        if not latest_run or timezone.localtime(latest_run).date() < today or data_changed_after_forecast:
            result = generate_monthly_forecasts()
            if result["created"]:
                messages.info(request, f"Pronósticos actualizados automáticamente desde las ventas registradas: {result['created']} períodos.")
            if result["skipped"]:
                messages.warning(request, "Algunos productos aún necesitan 12 meses completos de historial para poder proyectarse.")
        request.session["forecast_checked_on"] = today.isoformat()
        request.session["forecast_source_checked"] = source_signature
    forecast_horizon_end = next_month
    for _ in range(FORECAST_HORIZON - 1):
        forecast_horizon_end = (forecast_horizon_end.replace(day=28) + timedelta(days=4)).replace(day=1)
    chart_months = _last_months(history_end_month, 12)
    forecast_rows = list(SalesForecast.objects.filter(
        period__gte=chart_months[0],
        period__lte=forecast_horizon_end,
    ).select_related("product").order_by("-period", "product__name"))
    future_forecasts = [row for row in forecast_rows if row.period >= next_month]
    actuals = actual_sales_by_month({row.product_id for row in forecast_rows}, history_cutoff)
    for row in forecast_rows:
        row.display_quantity = _forecast_display_quantity(row)
        row.reference_min, row.reference_max = _forecast_reference_range(row)
        row.actual_quantity = None
        row.absolute_error = None
        row.actual_status = "Futuro" if row.period >= current_month else "Sin ventas registradas"
        actual = actuals.get((row.product_id, row.period), Decimal("0"))
        if row.period < current_month:
            row.actual_quantity = actual
            row.absolute_error = abs(actual - row.predicted_quantity)
            row.actual_status = "Calculado"
    evaluation_by_product = {}
    for row in forecast_rows:
        if row.absolute_error is None:
            continue
        evaluation = evaluation_by_product.setdefault(row.product_id, {
            "name": row.product.name, "unit": row.product.unit, "comparisons": 0, "error_total": Decimal("0")
        })
        evaluation["comparisons"] += 1
        evaluation["error_total"] += row.absolute_error
    forecast_evaluation = [
        {**item, "average_absolute_error": item["error_total"] / item["comparisons"]}
        for item in sorted(evaluation_by_product.values(), key=lambda value: value["name"])
    ]
    forecast_products = {}
    for row in future_forecasts:
        forecast_products.setdefault(row.product_id, {"name": row.product.name, "forecasts": []})["forecasts"].append(row)
    forecast_chart_data = {}
    for product_id, item in forecast_products.items():
        future = sorted(
            (forecast for forecast in item["forecasts"] if forecast.period >= next_month),
            key=lambda forecast: forecast.period,
        )
        monthly_history = [actuals.get((product_id, month), Decimal("0")) for month in chart_months]
        months_with_sales = sum(quantity > 0 for quantity in monthly_history)
        last_month_with_sales = next(
            (month for month, quantity in reversed(list(zip(chart_months, monthly_history))) if quantity > 0),
            None,
        )
        item["history_summary"] = {
            "months_with_sales": months_with_sales,
            "months_without_sales": len(monthly_history) - months_with_sales,
            "average_monthly_sales": round(float(sum(monthly_history) / len(monthly_history)), 2) if monthly_history else 0,
            "last_month_with_sales": last_month_with_sales.strftime("%m/%Y") if last_month_with_sales else "Sin ventas",
        }
        labels = [month.strftime("%m/%y") for month in chart_months] + [forecast.period.strftime("%m/%y") for forecast in future]
        actual_values = [float(actuals.get((product_id, month), 0)) for month in chart_months] + [None] * len(future)
        latest_actual = actual_values[len(chart_months) - 1]
        forecast_values = [None] * (len(chart_months) - 1) + [latest_actual] + [float(forecast.predicted_quantity) for forecast in future]
        forecast_chart_data[str(product_id)] = {
            "labels": labels,
            "unit": future[0].product.unit if future else "unidades",
            "history_summary": item["history_summary"],
            "series": [
                {"label": "Ventas reales", "values": actual_values, "color": "#159ab0"},
                {"label": "Proyección", "values": forecast_values, "color": "#75b82a"},
            ],
        }
    future_rows = list(SalesForecast.objects.filter(
        period__gte=next_month,
        period__lte=forecast_horizon_end,
        product__kind=Product.Kind.FINISHED,
        product__active=True,
    ).select_related("product").prefetch_related("product__recipe_ingredients__material").order_by("period"))
    demand_by_product = {}
    for row in future_rows:
        entry = demand_by_product.setdefault(row.product_id, {"product": row.product, "quantity": Decimal("0"), "periods": 0})
        entry["quantity"] += row.predicted_quantity
        entry["periods"] += 1
    stock_coverage = []
    for entry in demand_by_product.values():
        if entry["periods"] < FORECAST_HORIZON:
            continue
        product = entry["product"]
        projected = entry["quantity"]
        production_suggestion = max(Decimal("0"), projected - product.current_stock)
        if production_suggestion <= 0:
            continue
        material_checks = []
        ingredients = list(product.recipe_ingredients.all())
        for ingredient in ingredients:
            required = ingredient.quantity_per_unit * production_suggestion
            if required > ingredient.material.current_stock:
                material_checks.append({
                    "name": ingredient.material.name,
                    "required": required,
                    "available": ingredient.material.current_stock,
                    "unit": ingredient.material.unit,
                })
        stock_coverage.append({
            "product": product,
            "projected": projected,
            "stock": product.current_stock,
            "production_suggestion": production_suggestion,
            "material_shortages": material_checks,
            "has_recipe": bool(ingredients),
        })
    stock_coverage.sort(key=lambda item: item["production_suggestion"], reverse=True)
    return render(request, "core/forecast_list.html", {
        "forecasts": forecast_rows,
        "forecast_products": forecast_products,
        "forecast_chart_data": forecast_chart_data,
        "forecast_product_count": len(forecast_products),
        "forecast_period_count": len(future_forecasts),
        "last_forecast_at": SalesForecast.objects.aggregate(latest=Max("generated_at"))["latest"],
        "history_months_required": MIN_HISTORY_MONTHS,
        "forecast_evaluation": forecast_evaluation,
        "stock_coverage": stock_coverage,
    })


@roles_required(*REPORT_ROLES)
def forecast_consultation(request):
    """Show an existing ML forecast for a selected product and future month."""
    current_month = timezone.localdate().replace(day=1)
    available_forecasts = list(
        SalesForecast.objects.filter(
            period__gt=current_month,
            product__kind=Product.Kind.FINISHED,
            product__active=True,
        )
        .select_related("product")
        .order_by("period", "product__name")
    )
    product_ids = {row.product_id for row in available_forecasts}
    products = Product.objects.filter(pk__in=product_ids).order_by("name")
    periods = sorted({row.period for row in available_forecasts})
    selected_product_id = request.GET.get("producto", "")
    selected_period = request.GET.get("mes", "")
    selected_forecast = next(
        (
            row for row in available_forecasts
            if str(row.product_id) == selected_product_id and row.period.isoformat() == selected_period
        ),
        None,
    )
    estimated_revenue = None
    reference_min = reference_max = None
    potential_stock_gap = stock_surplus = None
    display_quantity = None
    demand_history = None
    if selected_forecast:
        display_quantity = _forecast_display_quantity(selected_forecast)
        reference_min, reference_max = _forecast_reference_range(selected_forecast)
        stock_delta = selected_forecast.product.current_stock - display_quantity
        potential_stock_gap = max(Decimal("0"), -stock_delta)
        stock_surplus = max(Decimal("0"), stock_delta)
        today = timezone.localdate()
        next_month = (current_month.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_is_closing_today = today == next_month - timedelta(days=1)
        history_end_month = current_month if month_is_closing_today else (current_month - timedelta(days=1)).replace(day=1)
        history_cutoff = next_month if month_is_closing_today else current_month
        months = _last_months(history_end_month, 12)
        history_by_month = actual_sales_by_month({selected_forecast.product_id}, history_cutoff)
        quantities = [history_by_month.get((selected_forecast.product_id, month), Decimal("0")) for month in months]
        nonzero_months = sum(quantity > 0 for quantity in quantities)
        last_sale_month = next((month for month, quantity in reversed(list(zip(months, quantities))) if quantity > 0), None)
        demand_history = {
            "months_with_sales": nonzero_months,
            "months_without_sales": len(months) - nonzero_months,
            "average_monthly_sales": sum(quantities) / len(quantities) if quantities else Decimal("0"),
            "last_sale_month": last_sale_month,
        }
    if selected_forecast and selected_forecast.product.sale_price is not None:
        estimated_revenue = display_quantity * selected_forecast.product.sale_price

    return render(request, "core/forecast_consultation.html", {
        "products": products,
        "periods": periods,
        "selected_product_id": selected_product_id,
        "selected_period": selected_period,
        "selected_forecast": selected_forecast,
        "display_quantity": display_quantity,
        "quantity_is_discrete": bool(selected_forecast and selected_forecast.product.unit.strip().casefold() in {"unidad", "unidades", "pieza", "piezas", "ud", "uds"}),
        "history_months": selected_forecast.error_metrics.get("history_months") if selected_forecast else None,
        "demand_history": demand_history,
        "validation_predictions": selected_forecast.error_metrics.get("validation_predictions") if selected_forecast else None,
        "reference_min": reference_min,
        "reference_max": reference_max,
        "potential_stock_gap": potential_stock_gap,
        "stock_surplus": stock_surplus,
        "estimated_revenue": estimated_revenue,
        "forecast_count": len(available_forecasts),
    })


@roles_required(*MANAGEMENT_ROLES)
def sales_history_import(request):
    form = SalesHistoryUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            batch = import_sales_history(
                uploaded_file=form.cleaned_data["file"],
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            request.session.pop("forecast_checked_on", None)
            messages.success(request, f"Se importaron {batch.row_count} filas del historial de ventas.")
            return redirect("sales_history_import")
        except ValidationError as exc:
            form.add_error(None, exc.messages)
    batches = SalesHistoryImport.objects.select_related("imported_by").all()[:30]
    current_month = timezone.localdate().replace(day=1)
    products = Product.objects.filter(kind=Product.Kind.FINISHED, active=True).order_by("name")
    active_product_ids = set(products.values_list("id", flat=True))
    projected_ids = set(
        SalesForecast.objects.filter(period__gt=current_month, product_id__in=active_product_ids)
        .values_list("product_id", flat=True)
        .distinct()
    )
    product_history_status = [
        {"product": product, "ready": product.pk in projected_ids}
        for product in products
    ]
    return render(request, "core/history_import.html", {
        "form": form,
        "batches": batches,
        "product_history_status": product_history_status,
        "forecast_ready_count": len(projected_ids),
        "finished_product_count": len(product_history_status),
    })


@roles_required(*MANAGEMENT_ROLES)
def sales_history_csv_template(request):
    response = HttpResponse(
        "\ufeffcodigo_producto;fecha;cantidad\r\n",
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = 'attachment; filename="plantilla_historial_ventas.csv"'
    return response


@roles_required(*MANAGEMENT_ROLES)
def forecast_generate(request):
    if request.method != "POST":
        return redirect("forecasts")
    result = generate_monthly_forecasts()
    if result["created"]:
        messages.success(request, f"Se generaron {result['created']} proyecciones mensuales.")
    if result["skipped"]:
        names = ", ".join(result["skipped"][:5])
        suffix = " y otros" if len(result["skipped"]) > 5 else ""
        messages.warning(request, f"Sin al menos 12 meses completos de historial: {names}{suffix}.")
    if not result["created"] and not result["skipped"]:
        messages.info(request, "No hay productos terminados activos para proyectar.")
    return redirect("forecasts")


@roles_required(*MANAGEMENT_ROLES)
def audit_logs(request):
    logs = AuditLog.objects.select_related("actor").all()
    search = request.GET.get("q", "").strip()
    action = request.GET.get("action", "").strip()
    entity = request.GET.get("entity", "").strip()
    date_from = parse_date(request.GET.get("desde", "").strip())
    date_to = parse_date(request.GET.get("hasta", "").strip())
    if search:
        matching_product_ids = list(
            Product.objects.filter(Q(name__icontains=search) | Q(code__icontains=search)).values_list("pk", flat=True)
        )
        matching_sale_ids = list(
            Sale.objects.filter(
                Q(client__name__icontains=search)
                | Q(client__tax_id__icontains=search)
                | Q(items__product_id__in=matching_product_ids)
            ).values_list("pk", flat=True).distinct()
        )
        matching_movement_ids = list(
            InventoryMovement.objects.filter(product_id__in=matching_product_ids).values_list("pk", flat=True)
        )
        logs = logs.annotate(details_text=Cast("details", output_field=CharField())).filter(
            Q(details_text__icontains=search)
            | Q(actor__username__icontains=search)
            | Q(actor__first_name__icontains=search)
            | Q(actor__last_name__icontains=search)
            | Q(entity__icontains=search)
            | Q(action__icontains=search)
            | Q(entity_id__icontains=search)
            | Q(entity="Sale", entity_id__in=[str(pk) for pk in matching_sale_ids])
            | Q(entity="InventoryMovement", entity_id__in=[str(pk) for pk in matching_movement_ids])
        )
    if action:
        logs = logs.filter(action=action)
    if entity:
        logs = logs.filter(entity=entity)
    if date_from:
        logs = logs.filter(created_at__date__gte=date_from)
    if date_to:
        logs = logs.filter(created_at__date__lte=date_to)
    paginator = Paginator(logs.order_by("-created_at"), 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    action_labels = {
        "CREATE": "Creación",
        "UPDATE": "Actualización",
        "DELETE": "Eliminación",
        "PAYMENT": "Pago registrado",
        "CONVERT": "Conversión a venta",
        "CANCEL": "Anulación o cancelación",
        "PURCHASE": "Compra registrada",
        "START": "Inicio de producción",
        "FINISH": "Producción finalizada",
        "EXPENSE": "Gasto registrado",
        "ADJUST": "Ajuste de inventario",
        "STOCK_ADJUSTMENT": "Ajuste de inventario",
        "IMPORT": "Importación",
        "LOGIN": "Inicio de sesión",
        "LOGOUT": "Cierre de sesión",
        "LOGIN_FAILED": "Intento de acceso fallido",
    }
    entity_labels = {
        "Sale": "Venta",
        "Product": "Producto o insumo",
        "InventoryMovement": "Movimiento de inventario",
        "ProductRecipe": "Receta de producto",
        "Client": "Cliente",
        "CustomerOrder": "Pedido",
        "ProductionLot": "Lote de producción",
        "Machine": "Máquina",
        "ProductionConsumption": "Consumo de producción",
        "PurchaseExpense": "Compra o gasto",
        "SalesHistoryImport": "Historial de ventas",
        "LegacyMatriplastImport": "Base anterior",
        "User": "Usuario",
        "Authentication": "Acceso al sistema",
    }
    detail_labels = {
        "username": "Usuario",
        "role": "Rol",
        "active": "Activo",
        "client_id": "Cliente ID",
        "lines": "Líneas",
        "line_count": "Líneas de productos",
        "status": "Estado",
        "materials": "Materias primas habituales",
        "sale_id": "Venta ID",
        "amount": "Monto",
        "method": "Método de pago",
        "payment_id": "Pago ID",
        "reason": "Motivo",
        "restocked_lines": "Líneas devueltas al stock",
        "product_id": "Producto ID",
        "quantity": "Cantidad",
        "initial_stock": "Existencia inicial",
        "produced_quantity": "Cantidad producida",
        "planned_quantity": "Cantidad planificada",
        "over_plan_confirmed": "Exceso confirmado",
        "description": "Detalle",
        "filename": "Archivo",
        "rows": "Registros",
        "from": "Desde",
        "to": "Hasta",
        "finished_products": "Productos terminados",
        "raw_materials": "Materias primas",
        "clients_created": "Clientes nuevos",
        "delivered_order_lines": "Líneas históricas",
        "pending_orders": "Pedidos pendientes",
        "skipped_orders": "Pedidos omitidos",
        "legacy_id": "ID anterior",
        "code": "Código",
        "unit": "Unidad",
    }

    def format_detail_value(key, value):
        if key == "method":
            return {"CASH": "Efectivo", "TRANSFER": "Transferencia", "QR": "QR"}.get(value, value)
        if key == "status":
            return {
                "OPEN": "Pendiente de pago", "PARTIAL": "Pago parcial", "PAID": "Pagada",
                "CANCELLED": "Anulada", "PENDING": "Pendiente", "CONVERTED": "Convertido a venta",
                "OPERATIONAL": "Operativa", "MAINTENANCE": "En mantenimiento", "OUT_OF_SERVICE": "Fuera de servicio",
            }.get(value, value)
        if key == "amount":
            return f"Bs {value}"
        if isinstance(value, bool):
            return "Sí" if value else "No"
        if isinstance(value, (dict, list)):
            return ""
        return str(value)

    def format_stock_quantity(value):
        try:
            return format(Decimal(str(value)).normalize(), "f")
        except (InvalidOperation, TypeError, ValueError):
            return str(value)

    def stock_change_label(change):
        product_name = change.get("product") or f"Producto #{change.get('product_id', '?')}"
        direction = "entrada" if change.get("direction") == "IN" else "salida"
        quantity = format_stock_quantity(change.get("quantity", "0"))
        unit = change.get("unit", "")
        before = format_stock_quantity(change.get("stock_before", "0"))
        after = format_stock_quantity(change.get("stock_after", "0"))
        unit_label = f" {unit}" if unit else ""
        return f"{product_name}: {direction} de {quantity}{unit_label} · stock {before} → {after}{unit_label}"

    def sale_context_by_log(entries):
        sale_ids = set()
        for entry in entries:
            if entry.entity == "Sale":
                try:
                    sale_ids.add(int(entry.entity_id))
                except (TypeError, ValueError):
                    continue
        sales = Sale.objects.filter(pk__in=sale_ids).select_related("client").prefetch_related("items__product")
        context = {}
        for sale in sales:
            products = ", ".join(
                f"{item.product.name} × {format_stock_quantity(item.quantity)}"
                for item in sale.items.all()
            ) or "sin productos disponibles"
            total = sum((item.quantity * item.unit_price for item in sale.items.all()), Decimal("0.00"))
            context[str(sale.pk)] = f"Cliente: {sale.client.name} · Productos: {products} · Total: Bs {total:.2f}"
        return context

    def prepare_log(log):
        entity_label = entity_labels.get(log.entity, log.entity)
        action_label = action_labels.get(log.action, log.action.replace("_", " ").capitalize())
        if log.action == "CREATE":
            action_label = f"Registró {entity_label.lower()}"
        elif log.action == "UPDATE":
            action_label = f"Actualizó {entity_label.lower()}"
        elif log.action == "DELETE":
            action_label = f"Eliminó {entity_label.lower()}"
        elif log.action == "CANCEL":
            action_label = f"Canceló {entity_label.lower()}"
        details = log.details if isinstance(log.details, dict) else {}
        sale_context = sale_contexts.get(str(log.entity_id)) if log.entity == "Sale" else None
        if "before" in details and "after" in details:
            before, after = details["before"], details["after"]
            changed = [detail_labels.get(key, key.replace("_", " ").capitalize()) for key in after if before.get(key) != after.get(key)]
            detail_text = "Campos modificados: " + ", ".join(changed) if changed else "No se detectaron cambios en los campos registrados."
        else:
            parts = []
            stock_changes = details.get("stock_changes")
            if isinstance(stock_changes, list):
                parts.extend(stock_change_label(change) for change in stock_changes if isinstance(change, dict))
            elif all(key in details for key in ("product_id", "quantity", "stock_before", "stock_after")):
                parts.append(stock_change_label(details))
            for key, value in details.items():
                if sale_context and key in {"client_id", "line_count"}:
                    continue
                if key in {"stock_changes", "stock_before", "stock_after", "quantity", "unit", "product", "product_id", "direction"} and (
                    isinstance(stock_changes, list) or "stock_before" in details
                ):
                    continue
                label = detail_labels.get(key, key.replace("_", " ").capitalize())
                display_value = format_detail_value(key, value)
                if display_value:
                    parts.append(f"{label}: {display_value}")
            if sale_context:
                parts.append(sale_context)
            detail_text = " · ".join(parts) if parts else "—"
        log.action_label = action_label
        log.entity_label = entity_label
        log.details_label = detail_text

    if request.GET.get("export") == "csv":
        rows = []
        export_logs = list(logs.order_by("-created_at", "-pk"))
        sale_contexts = sale_context_by_log(export_logs)
        for log in export_logs:
            prepare_log(log)
            if log.actor:
                actor_name = log.actor.get_full_name() or log.actor.username
            elif log.action == "LOGIN_FAILED":
                actor_name = "Acceso no autenticado"
            else:
                actor_name = "Usuario no disponible"
            rows.append((
                log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                actor_name,
                log.action_label,
                log.entity_label,
                log.entity_id,
                log.details_label,
            ))
        return _csv_download(
            "logs_sistema.csv",
            ("Fecha y hora", "Usuario", "Acción", "Módulo", "ID registro", "Detalle"),
            rows,
        )

    sale_contexts = sale_context_by_log(page_obj.object_list)
    for log in page_obj.object_list:
        prepare_log(log)

    actions = AuditLog.objects.order_by().values_list("action", flat=True).distinct()
    entities = AuditLog.objects.order_by().values_list("entity", flat=True).distinct()
    return render(request, "core/audit_logs.html", {
        "logs": page_obj.object_list,
        "page_obj": page_obj,
        "search": search,
        "selected_action": action,
        "selected_entity": entity,
        "actions": [(value, action_labels.get(value, value.replace("_", " ").capitalize())) for value in actions],
        "entities": [(value, entity_labels.get(value, value)) for value in entities],
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
        "query_string": query_params.urlencode(),
    })


@roles_required("Administrador")
def user_list(request):
    users = get_user_model().objects.prefetch_related("groups").order_by("username")
    return render(request, "core/user_list.html", {"users": users})


@roles_required("Administrador")
def user_create(request):
    form = InternalUserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        AuditLog.objects.create(
            actor=request.user,
            action="CREATE",
            entity="User",
            entity_id=user.pk,
            details={"username": user.username, "role": form.cleaned_data["role"].name},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, f"Usuario {user.username} creado.")
        return redirect("user_list")
    return render(request, "core/form_page.html", {"form": form, "title": "Crear usuario", "back_url": "user_list"})


@roles_required("Administrador")
def user_update(request, user_id):
    user = get_object_or_404(get_user_model(), pk=user_id)
    form = InternalUserUpdateForm(request.POST or None, instance=user)
    if request.method == "POST" and form.is_valid():
        if user.pk == request.user.pk and not form.cleaned_data["is_active"]:
            form.add_error("is_active", "No puedes desactivar tu propia cuenta mientras estás conectado.")
        else:
            updated_user = form.save()
            AuditLog.objects.create(
                actor=request.user,
                action="UPDATE",
                entity="User",
                entity_id=updated_user.pk,
                details={"username": updated_user.username, "role": form.cleaned_data["role"].name, "active": updated_user.is_active},
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(request, f"Usuario {updated_user.username} actualizado.")
            return redirect("user_list")
    return render(request, "core/form_page.html", {"form": form, "title": f"Editar usuario {user.username}", "back_url": "user_list"})
