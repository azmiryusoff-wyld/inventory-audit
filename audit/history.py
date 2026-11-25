"""The change log (finding C-43): who uploaded, added, changed or deleted what, with old and new values."""
from datetime import date, datetime
from decimal import Decimal

from django.db import models

from .models import ChangeLog


def _plain(value):
    if isinstance(value, models.Model):
        return str(value)
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value == value.to_integral() else str(value.normalize())
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def snapshot(obj, fields) -> dict:
    return {f: _plain(getattr(obj, f, None)) for f in fields}


def diff(before: dict, after: dict) -> dict:
    return {f: [before.get(f), after.get(f)] for f in after if before.get(f) != after.get(f)}


def log(user, action, what, obj=None, audit=None, changes=None, note="") -> ChangeLog:
    return ChangeLog.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        audit=audit, action=action, what=what[:60],
        object_repr=str(obj)[:300] if obj is not None else "", changes=changes or {}, note=note[:500],
    )


def describe(entry: ChangeLog) -> str:
    """'qty 10 → 12; value 50 → 60' for the history list."""
    return "; ".join(f"{field.replace('_', ' ')} {old if old not in (None, '') else '(blank)'} → "
                     f"{new if new not in (None, '') else '(blank)'}"
                     for field, (old, new) in entry.changes.items())
