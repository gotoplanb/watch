"""
Triage routing matrix (ADR-037) — loader + evaluator for `triage-matrix.yaml`.

The YAML document is the single source of truth for deterministic triage policy: humans read
the same artifact the code executes, so policy and behavior can't drift. This module loads it
once (validated hard at first use — a malformed matrix is a startup defect, not a runtime
surprise), classifies span evidence against the ordered rules, and resolves dispositions from
the (verdict × cell × mode) table. Pure lookups — no I/O beyond the one file read, no AI:
the assistant (triage_ai) is consulted only when no rule matches (ADR-036/037).

Origin note: a span's origin is `third_party` when its kind is `client` — an outbound call,
where the failure surface is the party we called. In a future multi-service watch, internal
service-to-service client spans would need a service allowlist to avoid reading as vendor;
the v1 backend is a monolith, so kind alone is sufficient (recorded in ADR-037).
"""
import yaml
from django.conf import settings

from .models import FaultDomain, OperatingMode, Responsibility, TriageVerdict

_CELLS = [f"{r}/{d}" for r in Responsibility.values for d in FaultDomain.values]


class MatrixError(RuntimeError):
    """The matrix file is missing, unparseable, or fails validation — a config defect."""


_matrix_cache = None


def set_matrix_for_tests(matrix: dict | None) -> None:
    """Inject a parsed matrix (or None to re-read the file) — same seam style as flags/queue."""
    global _matrix_cache
    _matrix_cache = matrix


def matrix() -> dict:
    global _matrix_cache
    if _matrix_cache is None:
        _matrix_cache = _load(settings.TRIAGE_MATRIX_PATH)
    return _matrix_cache


def _load(path) -> dict:
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise MatrixError(f"cannot load triage matrix {path}: {exc}") from exc
    _validate(data)
    return data


_ACTIONS = ("no_action", "auto_resolve", "auto_escalate")


def _validate(data) -> None:
    if not isinstance(data, dict):
        raise MatrixError("matrix root must be a mapping")
    _validate_classification(data.get("classification") or [])
    if sorted(data.get("precedence") or []) != sorted(_CELLS):
        raise MatrixError("precedence must list every responsibility/fault_domain cell exactly once")
    _validate_disposition(data.get("disposition") or {})


def _validate_classification(rules) -> None:
    for rule in rules:
        cell = rule.get("cell")
        if cell not in _CELLS:
            raise MatrixError(f"classification rule has unknown cell {cell!r}")
        match = rule.get("match")
        if not isinstance(match, dict) or not match:
            raise MatrixError(f"classification rule for {cell} has no match conditions")


def _validate_disposition(disposition) -> None:
    if sorted(disposition) != sorted(TriageVerdict.values):
        raise MatrixError("disposition must cover exactly the verdicts real/false_positive/undetermined")
    for verdict, cells in disposition.items():
        if verdict == TriageVerdict.REAL and sorted(cells) != sorted(_CELLS):
            raise MatrixError("disposition.real must cover every cell explicitly")
        for cell, by_mode in cells.items():
            _validate_cell_modes(verdict, cell, by_mode)


def _validate_cell_modes(verdict, cell, by_mode) -> None:
    if cell != "*" and cell not in _CELLS:
        raise MatrixError(f"disposition.{verdict} has unknown cell {cell!r}")
    if sorted(by_mode or {}) != sorted(OperatingMode.values):
        raise MatrixError(f"disposition.{verdict}.{cell} must map every operating mode")
    for mode, action in by_mode.items():
        if action not in _ACTIONS:
            raise MatrixError(f"disposition.{verdict}.{cell}.{mode}: unknown action {action!r}")


def _origin(span) -> str:
    return "third_party" if span.kind == "client" else "ours"


def _rule_matches(match: dict, span) -> bool:
    if "origin" in match and match["origin"] != _origin(span):
        return False
    if match.get("no_status") and span.http_status is not None:
        return False
    if "statuses" in match and span.http_status not in match["statuses"]:
        return False
    return True


def classify(spans) -> tuple[str, str] | None:
    """Classify error spans against the ordered rules (first match wins per span), then
    aggregate mixed evidence to the worst matched cell by precedence. Returns
    (responsibility, fault_domain), or None when nothing matched — the AI fallback's cue."""
    matched = set()
    for span in spans:
        for rule in matrix()["classification"]:
            if _rule_matches(rule["match"], span):
                matched.add(rule["cell"])
                break
    for cell in matrix()["precedence"]:
        if cell in matched:
            responsibility, fault_domain = cell.split("/")
            return responsibility, fault_domain
    return None


def dispose(responsibility: str, fault_domain: str, verdict: str, mode: str) -> str:
    """The deterministic disposition — AI classifies, THIS decides (ADR-036/037). A pure table
    lookup: every action is reproducible from the TriageDecision row and the matrix version."""
    cells = matrix()["disposition"][verdict]
    by_mode = cells.get(f"{responsibility}/{fault_domain}") or cells["*"]
    return by_mode[mode]
