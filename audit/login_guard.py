"""Login lockout: after 5 wrong passwords for a username within 15 minutes, that username can't log in
for 15 minutes (even with the right password). A successful login clears the count."""
from datetime import timedelta

from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.core.exceptions import PermissionDenied, ValidationError
from django.dispatch import receiver
from django.utils import timezone

MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)


def _norm(username) -> str:
    return (username or "").strip().lower()[:150]


def is_locked(username) -> bool:
    from .models import FailedLogin

    since = timezone.now() - WINDOW
    return FailedLogin.objects.filter(username=_norm(username), at__gte=since).count() >= MAX_FAILURES


class LockoutBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username and is_locked(username):
            raise PermissionDenied("Too many failed logins")
        return super().authenticate(request, username=username, password=password, **kwargs)


class LoginForm(AdminAuthenticationForm):
    def clean(self):
        if is_locked(self.cleaned_data.get("username")):
            raise ValidationError(
                "Too many wrong passwords for this username. Wait 15 minutes and try again, "
                "or ask the CEO to reset your password.", code="locked")
        return super().clean()


@receiver(user_login_failed, dispatch_uid="audit-login-failed")
def record_failure(sender, credentials, request=None, **kwargs):
    from .models import FailedLogin

    ip = request.META.get("REMOTE_ADDR") if request is not None else None
    FailedLogin.objects.create(username=_norm(credentials.get("username")), ip=ip or None)
    FailedLogin.objects.filter(at__lt=timezone.now() - timedelta(days=1)).delete()


@receiver(user_logged_in, dispatch_uid="audit-logged-in")
def clear_failures(sender, user, request=None, **kwargs):
    from .models import FailedLogin

    FailedLogin.objects.filter(username=_norm(user.get_username())).delete()
