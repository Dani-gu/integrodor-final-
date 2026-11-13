def navigation_permissions(request):
    user = request.user
    if not user.is_authenticated:
        return {}
    roles = set(user.groups.values_list("name", flat=True))
    if user.is_superuser:
        roles.update(("Administrador", "Gerente", "Vendedor", "Almacenero", "Supervisor", "Operario", "Contador"))
    return {
        "nav_sales": bool(roles & {"Administrador", "Gerente", "Vendedor"}),
        "nav_inventory": bool(roles & {"Administrador", "Gerente", "Almacenero", "Supervisor"}),
        "nav_production": bool(roles & {"Administrador", "Gerente", "Supervisor", "Operario"}),
        "nav_reports": bool(roles & {"Administrador", "Gerente", "Supervisor", "Contador"}),
        "nav_expenses": bool(roles & {"Administrador", "Gerente", "Contador"}),
        "nav_history_import": bool(roles & {"Administrador", "Gerente"}),
        "nav_logs": bool(roles & {"Administrador", "Gerente"}),
        "nav_users": bool(roles & {"Administrador"}),
    }
