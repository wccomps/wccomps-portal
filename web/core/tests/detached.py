"""Waiting for the operations core.utils runs in threads of their own."""

import threading

from core.utils import RUN_DETACHED_THREAD


def join_detached_runs() -> None:
    for thread in threading.enumerate():
        if thread.name == RUN_DETACHED_THREAD:
            thread.join(timeout=10)
