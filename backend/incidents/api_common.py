"""Shared API plumbing for the `wt` CLI surface (ADR-056): records addressed by their human
number (INC-0042, ASM-0007) as well as their UUID, simple list filters, and who-am-I."""
import uuid

from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import TIER_ORDER


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


class NumberLookupMixin:
    """`/api/incidents/INC-0042/` resolves like `/api/incidents/<uuid>/`. Object permissions still
    run, so authz is unchanged."""

    def get_object(self):
        key = str(self.kwargs[self.lookup_url_kwarg or self.lookup_field])
        lookup = {"pk": key} if _is_uuid(key) else {"number": key.strip().upper()}
        obj = get_object_or_404(self.filter_queryset(self.get_queryset()), **lookup)
        self.check_object_permissions(self.request, obj)
        return obj


def filtered(queryset, params, fields):
    """Apply case-insensitive `?field=value` filters for the allowed fields (incident statuses are
    upper-case choices, assessment statuses lower-case; callers shouldn't have to know)."""
    for field in fields:
        value = params.get(field, "").strip()
        if value:
            queryset = queryset.filter(**{f"{field}__iexact": value})
    return queryset


class MeView(APIView):
    """Who the presented credential is — `wt whoami`."""

    def get(self, request):
        user = request.user
        held = set(user.groups.values_list("name", flat=True))
        return Response({
            "id": user.pk,
            "username": user.username,
            "tiers": [t for t in TIER_ORDER if t in held],
            "is_staff": user.is_staff,
        })
