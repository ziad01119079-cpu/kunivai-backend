"""Serialize expensive worker module imports within a Linux replica."""

import contextlib
import importlib
import os


def _load(module: str):
    if os.name != "posix":
        return importlib.import_module(module).app
    import fcntl
    path = os.getenv("KUNIV_WORKER_IMPORT_LOCK", "/tmp/kuniv-worker-import.lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with contextlib.closing(os.fdopen(descriptor, "rb")) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return importlib.import_module(module).app


def create_app():
    return _load("main")


def create_zero_bench_app():
    return _load("scripts.scale_isolated_app")


def create_http_fixture_app():
    return _load("scripts.scalability_mock_app")
