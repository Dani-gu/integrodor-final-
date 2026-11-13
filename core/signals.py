from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from .models import AuditLog


def _request_ip(request):
    return request.META.get("REMOTE_ADDR") if request else None


@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    AuditLog.objects.create(
        actor=user,
        action="LOGIN",
        entity="User",
        entity_id=str(user.pk),
        details={"username": user.get_username()},
        ip_address=_request_ip(request),
    )


@receiver(user_logged_out)
def log_user_logout(sender, request, user, **kwargs):
    if user is None:
        return
    AuditLog.objects.create(
        actor=user,
        action="LOGOUT",
        entity="User",
        entity_id=str(user.pk),
        details={"username": user.get_username()},
        ip_address=_request_ip(request),
    )


@receiver(user_login_failed)
def log_failed_login(sender, credentials, request, **kwargs):
    attempted_username = credentials.get("username", "")
    AuditLog.objects.create(
        actor=None,
        action="LOGIN_FAILED",
        entity="Authentication",
        entity_id=str(attempted_username)[:64],
        details={"username": str(attempted_username)[:150]},
        ip_address=_request_ip(request),
    )
