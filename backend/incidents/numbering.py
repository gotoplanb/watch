"""Human record numbers (ADR-031): INC-0142 / PRB-0007 / RCA-0003 from a per-prefix monotonic
counter. Incremented under `select_for_update` so concurrent creators can't collide (a no-op on
sqlite, which serializes writes anyway). Gaps are fine — ServiceNow-style."""
from django.db import transaction

from .models import RecordCounter


def next_number(prefix: str) -> str:
    with transaction.atomic():
        counter, _ = RecordCounter.objects.select_for_update().get_or_create(prefix=prefix)
        counter.value += 1
        counter.save(update_fields=["value"])
    return f"{prefix}-{counter.value:04d}"
