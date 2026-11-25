"""Who sees which brands and outlets (finding C-42).

Users with "Can see every brand and outlet" (the Chef and Procurement groups, and superusers such as the
CEO) see everything. Anyone else sees only the brands and outlets ticked on their access record; with no
record they see nothing.
"""
from django.db.models import Q
from django.http import Http404

from .models import AuditPeriod, Brand, Outlet


def sees_all(user) -> bool:
    return user.is_superuser or user.has_perm("audit.view_all_outlets")


def outlets_for(user):
    qs = Outlet.objects.select_related("brand")
    if sees_all(user):
        return qs
    access = getattr(user, "audit_access", None)
    if access is None:
        return qs.none()
    return qs.filter(Q(brand__in=access.brands.all()) | Q(pk__in=access.outlets.all())).distinct()


def brands_for(user):
    if sees_all(user):
        return Brand.objects.all()
    return Brand.objects.filter(outlets__in=outlets_for(user)).distinct()


def audits_for(user):
    qs = AuditPeriod.objects.select_related("outlet__brand", "locked_by")
    if sees_all(user):
        return qs
    return qs.filter(outlet__in=outlets_for(user))


def get_audit(user, pk) -> AuditPeriod:
    audit = audits_for(user).filter(pk=pk).first()
    if audit is None:
        raise Http404("No such audit, or you don't have access to its outlet.")
    return audit


def get_brand(user, code) -> Brand:
    brand = brands_for(user).filter(code=code).first()
    if brand is None:
        raise Http404("No such brand, or you don't have access to it.")
    return brand
