from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.template import Context, Template
from django.urls import reverse

from .models import (
    AuditLog,
    Client,
    CustomerOrder,
    InventoryMovement,
    Machine,
    Payment,
    Product,
    ProductionConsumption,
    ProductionLot,
    PurchaseExpense,
    RecipeIngredient,
    Sale,
    SaleItem,
)
from .services import (
    adjust_inventory,
    cancel_customer_order,
    cancel_unpaid_sale,
    convert_customer_order_to_sale,
    create_customer_order,
    create_sale,
    finish_production_lot,
    record_payment,
    register_purchase,
    start_production_lot,
)
from .forecasting import _fit_and_predict
from datetime import date


User = get_user_model()


class BusinessFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="validation-admin", email="validation@example.test", password="test-password-123"
        )
        cls.client_record = Client.objects.create(name="Cliente de validación")
        cls.raw = Product.objects.create(
            code="TEST-RAW", name="Resina de prueba", kind=Product.Kind.RAW_MATERIAL,
            unit="kg", current_stock=Decimal("100.000"), minimum_stock=Decimal("10.000"),
        )
        cls.finished = Product.objects.create(
            code="TEST-FIN", name="Envase de prueba", kind=Product.Kind.FINISHED,
            unit="unidad", sale_price=Decimal("5.00"), current_stock=Decimal("20.000"),
        )
        cls.machine = Machine.objects.create(name="Máquina de validación")
        cls.machine.materials.add(cls.raw)
        RecipeIngredient.objects.create(
            finished_product=cls.finished, material=cls.raw, quantity_per_unit=Decimal("0.100000")
        )

    def setUp(self):
        self.client.force_login(self.user)

    def line(self, quantity="2", unit_price="5.00"):
        return [{"product_id": self.finished.pk, "quantity": quantity, "unit_price": unit_price}]

    def test_sale_reduces_stock_and_records_movement_and_audit(self):
        sale = create_sale(actor=self.user, client_id=self.client_record.pk, lines=self.line(), ip_address="127.0.0.1")
        self.raw.refresh_from_db()
        self.finished.refresh_from_db()
        self.assertEqual(self.finished.current_stock, Decimal("18.000"))
        self.assertEqual(self.raw.current_stock, Decimal("100.000"))
        self.assertEqual(sale.status, Sale.Status.OPEN)
        self.assertTrue(InventoryMovement.objects.filter(sale=sale, kind=InventoryMovement.Kind.SALE).exists())
        self.assertTrue(AuditLog.objects.filter(entity="Sale", entity_id=str(sale.pk), action="CREATE").exists())

    def test_sale_rejects_insufficient_stock_without_partial_changes(self):
        before = self.finished.current_stock
        with self.assertRaises(ValidationError):
            create_sale(actor=self.user, client_id=self.client_record.pk, lines=self.line(quantity="999"))
        self.finished.refresh_from_db()
        self.assertEqual(self.finished.current_stock, before)
        self.assertFalse(Sale.objects.exists())

    def test_payment_updates_status_and_rejects_overpayment(self):
        sale = create_sale(actor=self.user, client_id=self.client_record.pk, lines=self.line())
        payment = record_payment(actor=self.user, sale_id=sale.pk, amount="5.00", method=Payment.Method.QR)
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.PARTIAL)
        self.assertEqual(sale.balance, Decimal("5.00"))
        with self.assertRaises(ValidationError):
            record_payment(actor=self.user, sale_id=sale.pk, amount="5.01", method=Payment.Method.CASH)
        sale.refresh_from_db()
        self.assertEqual(sale.amount_paid, Decimal("5.00"))
        payment = record_payment(actor=self.user, sale_id=sale.pk, amount="5.00", method=Payment.Method.TRANSFER)
        sale.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.PAID)
        self.assertEqual(sale.balance, Decimal("0.00"))
        self.assertEqual(Payment.objects.filter(sale=sale).count(), 2)

    def test_cancel_unpaid_sale_restores_stock_but_paid_sale_is_protected(self):
        sale = create_sale(actor=self.user, client_id=self.client_record.pk, lines=self.line())
        cancel_unpaid_sale(actor=self.user, sale_id=sale.pk, reason="Prueba")
        sale.refresh_from_db()
        self.finished.refresh_from_db()
        self.assertEqual(sale.status, Sale.Status.CANCELLED)
        self.assertEqual(self.finished.current_stock, Decimal("20.000"))
        paid_sale = create_sale(
            actor=self.user, client_id=self.client_record.pk, lines=self.line(),
            payments=[{"amount": "1", "method": Payment.Method.CASH}],
        )
        with self.assertRaises(ValidationError):
            cancel_unpaid_sale(actor=self.user, sale_id=paid_sale.pk, reason="Prueba")

    def test_order_conversion_creates_one_sale_and_cannot_repeat(self):
        order = create_customer_order(actor=self.user, client_id=self.client_record.pk, lines=self.line())
        sale = convert_customer_order_to_sale(actor=self.user, order_id=order.pk)
        order.refresh_from_db()
        self.finished.refresh_from_db()
        self.assertEqual(order.status, CustomerOrder.Status.CONVERTED)
        self.assertEqual(order.sale_id, sale.pk)
        self.assertEqual(self.finished.current_stock, Decimal("18.000"))
        with self.assertRaises(ValidationError):
            convert_customer_order_to_sale(actor=self.user, order_id=order.pk)
        self.assertEqual(Sale.objects.count(), 1)

    def test_pending_order_can_be_cancelled(self):
        order = create_customer_order(actor=self.user, client_id=self.client_record.pk, lines=self.line())
        cancel_customer_order(actor=self.user, order_id=order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, CustomerOrder.Status.CANCELLED)
        self.assertEqual(self.finished.current_stock, Decimal("20.000"))

    def test_purchase_and_inventory_adjustment_update_stock_and_ledger(self):
        register_purchase(actor=self.user, product_id=self.raw.pk, quantity="12", amount="60", description="Compra de prueba")
        adjust_inventory(actor=self.user, product_id=self.raw.pk, direction="OUT", quantity="2", reason="Conteo de prueba")
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.current_stock, Decimal("110.000"))
        self.assertEqual(PurchaseExpense.objects.filter(product=self.raw).count(), 1)
        self.assertEqual(InventoryMovement.objects.filter(product=self.raw).count(), 2)
        with self.assertRaises(ValidationError):
            adjust_inventory(actor=self.user, product_id=self.raw.pk, direction="OUT", quantity="999", reason="Exceso")

    def test_production_consumes_material_and_adds_finished_stock(self):
        lot = ProductionLot.objects.create(
            product=self.finished, planned_quantity=Decimal("5"), operator=self.user, machine=self.machine,
        )
        start_production_lot(actor=self.user, lot_id=lot.pk)
        finish_production_lot(
            actor=self.user, lot_id=lot.pk, produced_quantity="5",
            consumptions=[{"material_id": self.raw.pk, "quantity": "0.5"}],
        )
        lot.refresh_from_db()
        self.raw.refresh_from_db()
        self.finished.refresh_from_db()
        self.assertIsNotNone(lot.finished_at)
        self.assertEqual(self.raw.current_stock, Decimal("99.500"))
        self.assertEqual(self.finished.current_stock, Decimal("25.000"))
        self.assertEqual(ProductionConsumption.objects.get(lot=lot).quantity, Decimal("0.500"))

    def test_insufficient_production_material_rolls_back_finish(self):
        lot = ProductionLot.objects.create(product=self.finished, planned_quantity=1, operator=self.user)
        start_production_lot(actor=self.user, lot_id=lot.pk)
        with self.assertRaises(ValidationError):
            finish_production_lot(
                actor=self.user, lot_id=lot.pk, produced_quantity="1",
                consumptions=[{"material_id": self.raw.pk, "quantity": "999"}],
            )
        lot.refresh_from_db()
        self.finished.refresh_from_db()
        self.assertIsNone(lot.finished_at)
        self.assertEqual(self.finished.current_stock, Decimal("20.000"))


class ApplicationPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="page-admin", email="page@example.test", password="test-password-123"
        )
        cls.client_record = Client.objects.create(name="Cliente páginas")
        cls.raw = Product.objects.create(code="PAGE-RAW", name="Materia página", kind=Product.Kind.RAW_MATERIAL, unit="kg", current_stock=100)
        cls.finished = Product.objects.create(code="PAGE-FIN", name="Producto página", kind=Product.Kind.FINISHED, unit="unidad", sale_price=5, current_stock=10)
        cls.machine = Machine.objects.create(name="Máquina página")
        cls.lot = ProductionLot.objects.create(product=cls.finished, planned_quantity=1, operator=cls.admin)
        cls.sale = create_sale(actor=cls.admin, client_id=cls.client_record.pk, lines=[{"product_id": cls.finished.pk, "quantity": "1", "unit_price": "5"}])

    def setUp(self):
        self.client.force_login(self.admin)

    def test_main_modules_reports_and_forms_render(self):
        paths = [
            reverse("dashboard"), reverse("product_list"), reverse("client_list"), reverse("sale_list"),
            reverse("order_list"), reverse("inventory_list"), reverse("reports"), reverse("sales_report"),
            reverse("inventory_report"), reverse("production_report"), reverse("production_list"),
            reverse("machine_list"), reverse("forecasts"), reverse("audit_logs"), reverse("user_list"),
            reverse("product_create"), reverse("client_create"), reverse("sale_create"),
            reverse("order_create"), reverse("purchase_create"), reverse("inventory_adjust"),
            reverse("expense_create"), reverse("production_create"), reverse("machine_create"),
            reverse("sales_history_import"),
            reverse("product_update", args=[self.finished.pk]),
            reverse("product_recipe", args=[self.finished.pk]),
            reverse("client_detail", args=[self.client_record.pk]),
            reverse("client_update", args=[self.client_record.pk]),
            reverse("sale_detail", args=[self.sale.pk]),
            reverse("sale_receipt", args=[self.sale.pk]),
            reverse("payment_add", args=[self.sale.pk]),
            reverse("production_lot_detail", args=[self.lot.pk]),
            reverse("production_finish", args=[self.lot.pk]),
            reverse("production_update", args=[self.lot.pk]),
            reverse("machine_update", args=[self.machine.pk]),
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_client_detail_shows_account_and_recent_payment(self):
        record_payment(actor=self.admin, sale_id=self.sale.pk, amount="2", method=Payment.Method.QR, reference="QA-001")
        response = self.client.get(reverse("client_detail", args=[self.client_record.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["balance_total"], Decimal("3.00"))
        self.assertContains(response, "Pagos recientes")
        self.assertContains(response, "QA-001")

    def test_sale_form_shows_initial_payment_and_remaining_balance_estimates(self):
        response = self.client.get(reverse("sale_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="sale-estimated-payment"')
        self.assertContains(response, 'id="sale-estimated-balance"')
        self.assertContains(response, 'id="sale-payment-warning"')

    def test_topbar_exposes_accessible_theme_toggle(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="theme-toggle"')
        self.assertContains(response, 'aria-label="Cambiar a tema oscuro"')
        self.assertContains(response, "/static/core/theme.css?v=2")

    def test_forecast_page_explains_model_and_accuracy_metrics(self):
        response = self.client.get(reverse("forecasts"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Compara dos métodos")
        self.assertContains(response, "Random Forest")
        self.assertContains(response, "MAE")
        self.assertContains(response, "12 meses completos")
        self.assertEqual(response.context["forecast_product_count"], 0)

    def test_audit_csv_exports_all_matching_logs(self):
        AuditLog.objects.create(actor=self.admin, action="PAYMENT", entity="Sale", entity_id="901", details={"amount": "15.00", "method": "QR"})
        response = self.client.get(reverse("audit_logs") + "?action=PAYMENT&export=csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename=\"logs_sistema.csv\"", response["Content-Disposition"])
        body = response.content.decode("utf-8-sig")
        self.assertIn("Pago registrado", body)
        self.assertIn("Bs 15.00", body)
        self.assertIn("901", body)

    def test_role_restriction_blocks_user_without_module_role(self):
        no_role_user = User.objects.create_user(username="sin-rol", password="test-password-123")
        self.client.force_login(no_role_user)
        self.assertEqual(self.client.get(reverse("product_list")).status_code, 403)

    def test_anonymous_user_is_sent_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def sale_form_data(self, **header):
        return {
            "client": self.client_record.pk,
            "notes": "Validación automatizada",
            "initial_payment": "0.00",
            "payment_method": "",
            "payment_reference": "",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.finished.pk,
            "lines-0-quantity": "1.000",
            "lines-0-unit_price": "5.00",
            **header,
        }

    def test_sale_and_initial_payment_http_submission(self):
        response = self.client.post(
            reverse("sale_create"),
            self.sale_form_data(initial_payment="2.00", payment_method=Payment.Method.QR, payment_reference="QR-TEST"),
        )
        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.exclude(pk=self.sale.pk).get()
        self.finished.refresh_from_db()
        self.assertEqual(self.finished.current_stock, Decimal("8.000"))
        self.assertEqual(sale.status, Sale.Status.PARTIAL)
        self.assertEqual(sale.payments.get().reference, "QR-TEST")

    def test_payment_http_submission_updates_sale(self):
        response = self.client.post(reverse("payment_add", args=[self.sale.pk]), {
            "amount": "2.00", "method": Payment.Method.TRANSFER, "reference": "TR-TEST",
        })
        self.assertEqual(response.status_code, 302)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.status, Sale.Status.PARTIAL)
        self.assertEqual(self.sale.amount_paid, Decimal("2.00"))

    def test_order_http_submission_and_conversion(self):
        response = self.client.post(reverse("order_create"), self.sale_form_data())
        self.assertEqual(response.status_code, 302)
        order = CustomerOrder.objects.get()
        self.assertEqual(order.status, CustomerOrder.Status.PENDING)
        self.finished.refresh_from_db()
        self.assertEqual(self.finished.current_stock, Decimal("9.000"))
        response = self.client.post(reverse("order_convert", args=[order.pk]))
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.finished.refresh_from_db()
        self.assertEqual(order.status, CustomerOrder.Status.CONVERTED)
        self.assertEqual(self.finished.current_stock, Decimal("8.000"))

    def test_purchase_http_submission_updates_raw_stock(self):
        response = self.client.post(reverse("purchase_create"), {
            "product": self.raw.pk, "quantity": "3.000", "amount": "12.00", "description": "Compra QA",
        })
        self.assertEqual(response.status_code, 302)
        self.raw.refresh_from_db()
        self.assertEqual(self.raw.current_stock, Decimal("103.000"))

    def test_production_create_start_and_finish_http_submission(self):
        response = self.client.post(reverse("production_create"), {
            "product": self.finished.pk, "planned_quantity": "2.000", "operator": self.admin.pk,
            "machine": self.machine.pk, "notes": "Lote QA",
        })
        self.assertEqual(response.status_code, 302)
        lot = ProductionLot.objects.exclude(pk=self.lot.pk).get()
        response = self.client.post(reverse("production_start", args=[lot.pk]))
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse("production_finish", args=[lot.pk]), {
            "produced_quantity": "2.000",
            "consumptions-TOTAL_FORMS": "2", "consumptions-INITIAL_FORMS": "1",
            "consumptions-MIN_NUM_FORMS": "0", "consumptions-MAX_NUM_FORMS": "1000",
            "consumptions-0-material": self.raw.pk, "consumptions-0-quantity": "0.200",
            "consumptions-1-material": "", "consumptions-1-quantity": "",
        })
        self.assertEqual(response.status_code, 302)
        lot.refresh_from_db()
        self.finished.refresh_from_db()
        self.raw.refresh_from_db()
        self.assertIsNotNone(lot.finished_at)
        self.assertEqual(self.finished.current_stock, Decimal("11.000"))
        self.assertEqual(self.raw.current_stock, Decimal("99.800"))

    def test_report_csv_exports_respond_with_downloads(self):
        paths = [
            reverse("sales_report") + "?export=csv",
            reverse("inventory_report") + "?export=stock",
            reverse("inventory_report") + "?export=movements",
            reverse("production_report") + "?export=csv",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("attachment; filename=", response["Content-Disposition"])


class ForecastModelTests(SimpleTestCase):
    def test_model_skips_history_shorter_than_twelve_months(self):
        history = [(date(2025, month, 1), Decimal(month * 10)) for month in range(1, 10)]
        self.assertIsNone(_fit_and_predict(history))

    def test_model_returns_three_nonnegative_monthly_predictions_with_metrics(self):
        history = []
        year, month = 2024, 1
        for index in range(18):
            history.append((date(year, month, 1), Decimal(100 + (index % 4) * 15 + index * 2)))
            month += 1
            if month == 13:
                month = 1
                year += 1
        result = _fit_and_predict(history)
        self.assertIsNotNone(result)
        model_name, metrics, forecast = result
        self.assertIn(model_name, ("Random Forest", "Media móvil de 3 meses", "Suavizado exponencial de Holt"))
        self.assertIn("MAE", metrics)
        self.assertEqual(len(forecast), 3)
        self.assertTrue(all(quantity >= 0 for _, quantity in forecast))


class NumberFormattingTests(SimpleTestCase):
    def test_inventory_and_currency_use_bolivian_separators(self):
        rendered = Template(
            "{% load number_format %}{{ quantity|bo_quantity }}|{{ stock|bo_quantity }}|{{ amount|bo_money }}"
        ).render(Context({
            "quantity": Decimal("2123.000"),
            "stock": Decimal("900.000"),
            "amount": Decimal("1234567.89"),
        }))
        self.assertEqual(rendered, "2.123,000|900,000|1.234.567,89")
