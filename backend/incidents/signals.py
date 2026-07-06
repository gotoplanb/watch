"""Signals: give every new user a rotation-seed keyring at creation (ADR-030); index each login
session so it can be revoked (ADR-008)."""
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import session_index
from .models import UserKeyring


@receiver(post_save, sender=User)
def create_keyring(sender, instance, created, **kwargs):
    if created:
        UserKeyring.objects.get_or_create(user=instance)


@receiver(user_logged_in)
def index_login_session(sender, request, user, **kwargs):
    """Record the session key so 'sign out everywhere' / admin force-sign-out can find it (ADR-008)."""
    session = getattr(request, "session", None)
    session_index.remember(user, getattr(session, "session_key", "") or "")
