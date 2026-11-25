"""Keep the stored dashboards (ReportCache) honest.

Any change to an audit's lines clears the stored dashboards of that outlet (the next month's opening
check reads this month's closing count); any master data change clears them all. Clearing is queued and
done once when the transaction commits, so a 2,000-line upload costs one delete, not 2,000.
"""
import threading

from django.db import connection, transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import models

_pending = threading.local()

MASTER_MODELS = (models.Brand, models.Item, models.SemiFinished, models.MenuItem, models.PosAlias,
                 models.Recipe, models.RecipeLine, models.Substitution)
LINE_MODELS = (models.SaleLine, models.ProductionLine, models.PurchaseLine, models.StockCount)


def flush_pending():
    """Clear what has been queued. Also called before stored dashboards are read."""
    outlets = getattr(_pending, "outlets", set())
    _pending.outlets, _pending.scheduled = set(), False
    if not outlets:
        return
    qs = models.ReportCache.objects.all()
    if "*" not in outlets:
        qs = qs.filter(audit__outlet_id__in=outlets)
    qs.delete()


def _queue(key):
    if not hasattr(_pending, "outlets"):
        _pending.outlets, _pending.scheduled = set(), False
    _pending.outlets.add(key)
    if not connection.in_atomic_block:
        flush_pending()
    elif not _pending.scheduled:
        _pending.scheduled = True
        transaction.on_commit(flush_pending)


def invalidate(audit=None, outlet_id=None):
    """Clear the stored dashboards of one outlet (from an audit), or all of them."""
    if audit is not None:
        outlet_id = audit.outlet_id
    _queue("*" if outlet_id is None else outlet_id)


def _line_changed(sender, instance, **kwargs):
    try:
        outlet_id = models.AuditPeriod.objects.filter(pk=instance.audit_id).values_list("outlet_id", flat=True).first()
    except Exception:  # the audit itself is being deleted
        outlet_id = None
    if outlet_id is not None:
        invalidate(outlet_id=outlet_id)


def _master_changed(sender, instance, raw=False, **kwargs):
    if not raw:
        invalidate()


def _audit_changed(sender, instance, raw=False, update_fields=None, **kwargs):
    if not raw:
        invalidate(audit=instance)


for model in LINE_MODELS:
    post_save.connect(_line_changed, sender=model, dispatch_uid=f"cache-{model.__name__}-save")
    post_delete.connect(_line_changed, sender=model, dispatch_uid=f"cache-{model.__name__}-delete")
for model in MASTER_MODELS:
    post_save.connect(_master_changed, sender=model, dispatch_uid=f"cache-{model.__name__}-save")
    post_delete.connect(_master_changed, sender=model, dispatch_uid=f"cache-{model.__name__}-delete")
post_save.connect(_audit_changed, sender=models.AuditPeriod, dispatch_uid="cache-audit-save")
