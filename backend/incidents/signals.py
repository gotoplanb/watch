"""Signals (ADR-030): give every new user a rotation-seed keyring at creation. `seed_for` also
lazily creates one, so pre-existing users are covered — this just makes it happen at creation."""
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserKeyring


@receiver(post_save, sender=User)
def create_keyring(sender, instance, created, **kwargs):
    if created:
        UserKeyring.objects.get_or_create(user=instance)
