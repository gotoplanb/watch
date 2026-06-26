"""Reachability helpers — integration tests skip (not fail) when a dependency
container isn't up, so `pytest -m integration` is friendly outside the compose stack."""
import socket

import pytest


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def require(host: str, port: int, name: str):
    if not tcp_open(host, port):
        pytest.skip(f"{name} not reachable at {host}:{port} — start the compose stack")
