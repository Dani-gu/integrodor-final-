from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group
from django.db.models import Q
from django.forms import BaseInlineFormSet, formset_factory, inlineformset_factory

from .models import Client, Machine, Product, ProductionLot, RecipeIngredient


class StyledFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs["class"] = f"{field.widget.attrs.get('class', '')} {css}".strip()


class ProductForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Product
        fields = ("code", "name", "kind", "unit", "sale_price", "current_stock", "minimum_stock", "active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["minimum_stock"].help_text = "Umbral de alerta: el sistema avisará cuando la existencia llegue a este nivel o baje de él. Usa la unidad configurada para el producto."


class ProductUpdateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Product
        fields = ("code", "name", "kind", "unit", "sale_price", "minimum_stock", "active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["minimum_stock"].help_text = "Umbral de alerta: el sistema avisará cuando la existencia llegue a este nivel o baje de él. Usa la unidad configurada para el producto."


class ClientForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Client
        fields = ("name", "tax_id", "phone", "email", "address", "active")


class ClientUpdateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Client
        fields = ("name", "tax_id", "phone", "email", "address", "active")


class SaleHeaderForm(StyledFormMixin, forms.Form):
    client = forms.ModelChoiceField(queryset=Client.objects.filter(active=True), label="Cliente")
    notes = forms.CharField(label="Observaciones", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    initial_payment = forms.DecimalField(label="Pago inicial (Bs)", min_value=0, initial=Decimal("0.00"))
    payment_method = forms.ChoiceField(
        label="Forma de pago",
        choices=(("", "— Selecciona si hay pago —"), ("CASH", "Efectivo"), ("TRANSFER", "Transferencia bancaria"), ("QR", "QR")),
        required=False,
    )
    payment_reference = forms.CharField(label="Referencia de pago", required=False, max_length=100)


class CustomerOrderHeaderForm(StyledFormMixin, forms.Form):
    client = forms.ModelChoiceField(queryset=Client.objects.filter(active=True), label="Cliente")
    notes = forms.CharField(label="Observaciones", required=False, widget=forms.Textarea(attrs={"rows": 2}))


class SalesHistoryUploadForm(StyledFormMixin, forms.Form):
    file = forms.FileField(label="Archivo CSV", help_text="Usa UTF-8 y las columnas indicadas en la pantalla.")

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError("El archivo no puede superar 10 MB.")
        if not uploaded.name.lower().endswith(".csv"):
            raise forms.ValidationError("Selecciona un archivo con extensión .csv.")
        return uploaded


class SaleLineForm(StyledFormMixin, forms.Form):
    product = forms.ModelChoiceField(
        queryset=Product.objects.filter(kind=Product.Kind.FINISHED, active=True), label="Producto"
    )
    quantity = forms.DecimalField(label="Cantidad", min_value=Decimal("0.001"), decimal_places=3, max_digits=14)
    unit_price = forms.DecimalField(label="Precio unitario (Bs)", min_value=0, decimal_places=2, max_digits=14)


SaleLineFormSet = formset_factory(SaleLineForm, extra=1, can_delete=True)


class PaymentForm(StyledFormMixin, forms.Form):
    amount = forms.DecimalField(label="Monto (Bs)", min_value=Decimal("0.01"), decimal_places=2, max_digits=14)
    method = forms.ChoiceField(label="Forma de pago", choices=(("CASH", "Efectivo"), ("TRANSFER", "Transferencia bancaria"), ("QR", "QR")))
    reference = forms.CharField(label="Referencia", required=False, max_length=100)


class CancelSaleForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(label="Motivo de anulación", max_length=240, widget=forms.Textarea(attrs={"rows": 2}))


class PurchaseForm(StyledFormMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(kind=Product.Kind.RAW_MATERIAL, active=True), label="Materia prima")
    quantity = forms.DecimalField(label="Cantidad", min_value=Decimal("0.001"), decimal_places=3, max_digits=14)
    amount = forms.DecimalField(label="Monto total (Bs)", min_value=Decimal("0.01"), decimal_places=2, max_digits=14)
    description = forms.CharField(label="Detalle de compra", max_length=200)


class InventoryAdjustmentForm(StyledFormMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.filter(active=True), label="Producto o insumo")
    direction = forms.ChoiceField(label="Tipo de ajuste", choices=(("IN", "Aumentar existencia"), ("OUT", "Reducir existencia")))
    quantity = forms.DecimalField(label="Cantidad", min_value=Decimal("0.001"), decimal_places=3, max_digits=14)
    reason = forms.CharField(label="Motivo del ajuste", max_length=240, widget=forms.Textarea(attrs={"rows": 3}))


class OtherExpenseForm(StyledFormMixin, forms.Form):
    description = forms.CharField(label="Descripción", max_length=200)
    amount = forms.DecimalField(label="Monto (Bs)", min_value=Decimal("0.01"), decimal_places=2, max_digits=14)


class ProductionLotForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ProductionLot
        fields = ("product", "planned_quantity", "operator", "machine", "notes")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        products = Product.objects.filter(kind=Product.Kind.FINISHED)
        products = products.filter(Q(active=True) | Q(pk=self.instance.product_id)) if self.instance.pk else products.filter(active=True)
        self.fields["product"].queryset = products.order_by("name")
        operators = self.fields["operator"].queryset
        self.fields["operator"].queryset = operators.filter(Q(is_active=True) | Q(pk=self.instance.operator_id)) if self.instance.pk else operators.filter(is_active=True)
        machines = Machine.objects.all()
        if self.instance.pk and self.instance.machine_id:
            machines = machines.filter(Q(status=Machine.Status.OPERATIONAL) | Q(pk=self.instance.machine_id))
        else:
            machines = machines.filter(status=Machine.Status.OPERATIONAL)
        self.fields["machine"].queryset = machines.order_by("name")
        self.fields["machine"].required = False
        self.fields["machine"].empty_label = "Sin máquina asignada"
        self.fields["product"].help_text = "Si tiene receta, abajo verás el consumo estimado y si el stock actual alcanza."
        self.fields["machine"].help_text = "La máquina puede tener materiales habituales; las cantidades se estiman con la receta del producto."
        self.fields["planned_quantity"].help_text = "Cantidad objetivo para este lote. La vista previa se actualiza al cambiarla; el resultado real se registra al finalizarlo."


class FinishLotForm(StyledFormMixin, forms.Form):
    produced_quantity = forms.DecimalField(label="Cantidad producida", min_value=0, decimal_places=3, max_digits=14)
    confirm_overproduction = forms.BooleanField(
        label="Confirmo que la cantidad real supera lo planificado.",
        required=False,
    )

    def __init__(self, *args, planned_quantity=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.planned_quantity = Decimal(str(planned_quantity)) if planned_quantity is not None else None

    def clean(self):
        cleaned = super().clean()
        produced = cleaned.get("produced_quantity")
        if (
            produced is not None
            and self.planned_quantity is not None
            and produced > self.planned_quantity
            and not cleaned.get("confirm_overproduction")
        ):
            self.add_error(
                "confirm_overproduction",
                "La cantidad producida supera el plan. Confirma que el valor ingresado es correcto.",
            )
        return cleaned


class ProductionConsumptionForm(StyledFormMixin, forms.Form):
    material = forms.ModelChoiceField(
        queryset=Product.objects.filter(kind=Product.Kind.RAW_MATERIAL, active=True).order_by("name"),
        label="Materia prima",
        required=False,
    )
    quantity = forms.DecimalField(label="Cantidad consumida", min_value=Decimal("0.001"), decimal_places=3, max_digits=14, required=False)

    def clean(self):
        cleaned = super().clean()
        material = cleaned.get("material")
        quantity = cleaned.get("quantity")
        if material and quantity is None:
            self.add_error("quantity", "Indica cuánto material se consumió.")
        elif quantity is not None and material is None:
            self.add_error("material", "Selecciona la materia prima consumida.")
        return cleaned


ProductionConsumptionFormSet = forms.formset_factory(ProductionConsumptionForm, extra=1, can_delete=True)


class MachineForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Machine
        fields = ("name", "status", "materials", "notes")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        materials = self.fields["materials"]
        materials.queryset = Product.objects.filter(kind=Product.Kind.RAW_MATERIAL, active=True).order_by("name")
        checkbox_widget = forms.CheckboxSelectMultiple()
        checkbox_widget.choices = materials.choices
        materials.widget = checkbox_widget
        materials.widget.attrs["class"] = "material-checkbox-list"
        materials.help_text = "Al finalizar un lote con esta máquina, estos materiales aparecerán preseleccionados para registrar el consumo real."


class RecipeIngredientForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = RecipeIngredient
        fields = ("material", "quantity_per_unit")
        widgets = {
            "quantity_per_unit": forms.NumberInput(attrs={"step": "0.000001", "inputmode": "decimal"}),
        }
        labels = {"quantity_per_unit": "Cantidad por unidad producida"}
        help_texts = {"quantity_per_unit": "Usa la unidad de la materia prima. Se permiten hasta 6 decimales; los ceros finales no son necesarios."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        quantity = self.initial.get("quantity_per_unit")
        if quantity not in (None, ""):
            try:
                normalized = Decimal(str(quantity)).normalize()
                self.initial["quantity_per_unit"] = format(normalized, "f")
            except (ArithmeticError, TypeError, ValueError):
                pass
        materials = Product.objects.filter(kind=Product.Kind.RAW_MATERIAL)
        if self.instance.pk and self.instance.material_id:
            materials = materials.filter(Q(active=True) | Q(pk=self.instance.material_id))
        else:
            materials = materials.filter(active=True)
        self.fields["material"].queryset = materials.order_by("name")


class BaseRecipeIngredientFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        materials = set()
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or form.cleaned_data.get("DELETE"):
                continue
            material = form.cleaned_data.get("material")
            quantity = form.cleaned_data.get("quantity_per_unit")
            if not material and quantity is None:
                continue
            if material and material.pk in materials:
                raise forms.ValidationError("No repitas una materia prima en la receta.")
            if material:
                materials.add(material.pk)


RecipeIngredientFormSet = inlineformset_factory(
    Product,
    RecipeIngredient,
    fk_name="finished_product",
    form=RecipeIngredientForm,
    formset=BaseRecipeIngredientFormSet,
    fields=("material", "quantity_per_unit"),
    extra=1,
    can_delete=True,
)


class InternalUserCreateForm(StyledFormMixin, UserCreationForm):
    role = forms.ModelChoiceField(queryset=Group.objects.filter(name__in=("Gerente", "Vendedor", "Almacenero", "Supervisor", "Operario", "Contador", "Administrador")).order_by("name"), label="Rol")

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "first_name", "last_name", "email")
        labels = {"username": "Nombre de usuario", "first_name": "Nombre", "last_name": "Apellido", "email": "Correo electrónico"}

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            user.groups.set([self.cleaned_data["role"]])
        return user


class InternalUserUpdateForm(StyledFormMixin, forms.ModelForm):
    role = forms.ModelChoiceField(queryset=Group.objects.filter(name__in=("Gerente", "Vendedor", "Almacenero", "Supervisor", "Operario", "Contador", "Administrador")).order_by("name"), label="Rol")

    class Meta:
        model = get_user_model()
        fields = ("username", "first_name", "last_name", "email", "is_active")
        labels = {"username": "Nombre de usuario", "first_name": "Nombre", "last_name": "Apellido", "email": "Correo electrónico", "is_active": "Usuario activo"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].initial = self.instance.groups.first()

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            user.groups.set([self.cleaned_data["role"]])
        return user
