"""Acquisition-local notification of committed evidence, never of readiness."""
from contextlib import contextmanager
from contextvars import ContextVar
from asyncio import Event
from uuid import UUID


_listener: ContextVar[tuple[UUID, Event] | None] = ContextVar("readiness_evidence_listener", default=None)


@contextmanager
def evidence_notifications(instrument_id: UUID, changed: Event):
    token = _listener.set((instrument_id, changed))
    try:
        yield
    finally:
        _listener.reset(token)


def evidence_committed(instrument_id: UUID) -> None:
    listener = _listener.get()
    if listener is not None and listener[0] == instrument_id:
        listener[1].set()
