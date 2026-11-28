from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

# Who can change what. The group CEO account is a superuser and sees everything.
# "Outlet manager" sees only the brands and outlets ticked on their access record (Admin > Users).
ROLES = {
    "Chef": {
        "change": ["semifinished", "menuitem", "posalias", "recipe", "recipeline", "substitution",
                   "auditperiod", "saleline", "productionline", "auditsubstitution"],
        "view": ["brand", "outlet", "item", "purchaseline", "stockcount"],
    },
    "Procurement": {
        "change": ["auditperiod", "purchaseline", "stockcount", "saleline", "productionline", "auditsubstitution"],
        "view": ["brand", "outlet", "item", "semifinished", "menuitem", "recipe", "recipeline", "substitution"],
    },
    "Outlet manager": {
        "change": [],
        "view": ["brand", "outlet", "item", "semifinished", "menuitem", "recipe", "recipeline", "substitution",
                 "auditperiod", "saleline", "productionline", "purchaseline", "stockcount", "auditsubstitution"],
        "scoped": True,
    },
}


class Command(BaseCommand):
    help = "Create the Chef, Procurement and Outlet manager groups with their permissions (safe to re-run)."

    def handle(self, *args, **opts):
        for role, spec in ROLES.items():
            group, _ = Group.objects.get_or_create(name=role)
            codenames = [f"view_{m}" for m in spec["view"] + spec["change"]]
            for m in spec["change"]:
                codenames += [f"add_{m}", f"change_{m}", f"delete_{m}"]
            if not spec.get("scoped"):
                codenames.append("view_all_outlets")  # group-wide roles see every brand and outlet
            perms = Permission.objects.filter(content_type__app_label="audit", codename__in=codenames)
            group.permissions.set(perms)
            self.stdout.write(self.style.SUCCESS(f"{role}: {perms.count()} permissions"))
        self.stdout.write("Add users to a group in /admin/auth/user/ and tick 'Staff status'.")
