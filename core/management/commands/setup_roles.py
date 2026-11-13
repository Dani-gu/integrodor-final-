from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db.models import Q


ROLE_MODELS = {
    "Administrador": None,
    "Gerente": {"product", "client", "sale", "saleitem", "payment", "customerorder", "customerorderitem", "inventorymovement", "productionlot", "purchaseexpense", "salesforecast", "saleshistoryimport", "historicalsale", "auditlog"},
    "Vendedor": {"product", "client", "sale", "saleitem", "payment", "customerorder", "customerorderitem"},
    "Almacenero": {"product", "inventorymovement", "purchaseexpense"},
    "Supervisor": {"product", "inventorymovement", "productionlot", "salesforecast"},
    "Operario": {"product", "productionlot"},
    "Contador": {"client", "sale", "saleitem", "payment", "purchaseexpense"},
}


class Command(BaseCommand):
    help = "Crea los grupos de roles iniciales para el sistema Matriplast."

    def handle(self, *args, **options):
        all_core_permissions = Permission.objects.filter(content_type__app_label="core")
        admin_permissions = Permission.objects.filter(
            Q(content_type__app_label="core")
            | Q(content_type__app_label="auth", content_type__model__in={"user", "group"})
        )
        for role, models in ROLE_MODELS.items():
            group, created = Group.objects.get_or_create(name=role)
            if role == "Administrador":
                permissions = admin_permissions
            else:
                permissions = all_core_permissions.filter(content_type__model__in=models)
                permissions = permissions.exclude(codename__startswith="delete_")
            group.permissions.set(permissions)
            state = "creado" if created else "actualizado"
            self.stdout.write(self.style.SUCCESS(f"Rol {role}: {state}."))
