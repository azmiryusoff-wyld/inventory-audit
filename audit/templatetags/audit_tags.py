from django import template

register = template.Library()


@register.filter
def money(value, places=2):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(v) < 0.005:
        return "-"
    text = f"{abs(v):,.{places}f}"
    return f"({text})" if v < 0 else text


@register.filter
def pct(value, places=1):
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value) * 100:.{places}f}%"
    except (TypeError, ValueError):
        return "-"


@register.filter
def num(value, places=2):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{v:,.{places}f}"
