from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import template

register = template.Library()


def _bolivian_number(value, decimal_places):
    if value is None or value == "":
        return "—"
    try:
        number = Decimal(str(value))
        formatted = format(number, f",.{decimal_places}f")
    except (InvalidOperation, TypeError, ValueError):
        return value
    whole, fraction = formatted.split(".")
    return f"{whole.replace(',', '.')},{fraction}"


@register.filter
def bo_quantity(value):
    """Format Bolivian quantities, trimming decimals that add no information."""
    formatted = _bolivian_number(value, 3)
    if not isinstance(formatted, str) or "," not in formatted:
        return formatted
    whole, fraction = formatted.rsplit(",", 1)
    fraction = fraction.rstrip("0")
    return f"{whole},{fraction}" if fraction else whole


@register.filter
def bo_money(value):
    """Format Bolivian bolivianos with two decimal places and dot grouping."""
    return _bolivian_number(value, 2)


@register.filter
def bo_product_quantity(value, unit):
    """Show whole pieces as integers while preserving decimals for measured goods."""
    if value is None or value == "":
        return "—"
    normalized_unit = str(unit or "").strip().casefold()
    if normalized_unit in {"unidad", "unidades", "pieza", "piezas", "ud", "uds"}:
        try:
            number = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            return format(number, ",.0f").replace(",", ".")
        except (InvalidOperation, TypeError, ValueError):
            return value
    return _bolivian_number(value, 3)
