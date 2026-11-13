from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def roles_required(*roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.user.is_superuser or request.user.groups.filter(name__in=roles).exists():
                return view(request, *args, **kwargs)
            raise PermissionDenied("Tu usuario no tiene permiso para este módulo.")

        return wrapped

    return decorator


ALL_ROLES = ("Administrador", "Gerente", "Vendedor", "Almacenero", "Supervisor", "Operario", "Contador")
MANAGEMENT_ROLES = ("Administrador", "Gerente")
SALES_ROLES = ("Administrador", "Gerente", "Vendedor")
INVENTORY_ROLES = ("Administrador", "Gerente", "Almacenero", "Supervisor")
PRODUCTION_ROLES = ("Administrador", "Gerente", "Supervisor", "Operario")
REPORT_ROLES = ("Administrador", "Gerente", "Supervisor", "Contador")
