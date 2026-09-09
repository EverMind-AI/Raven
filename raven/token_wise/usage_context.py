"""Carry session-scoped usage ownership through turns and ACP prompts."""

from contextlib import contextmanager
from contextvars import ContextVar

_session = ContextVar("usage_session", default=None)
_owner = ContextVar("usage_owner", default=None)


def session_key():
    return _session.get()


def root_session_key(session=None):
    return (_owner.get() or {}).get("root_session_key") or session or session_key()


def telemetry_dir():
    return (_owner.get() or {}).get("telemetry_dir")


def delegation(session=None):
    from raven.token_wise.usage_tracker import _default_telemetry_dir

    return {
        "root_session_key": root_session_key(session),
        "telemetry_dir": telemetry_dir() or str(_default_telemetry_dir()),
    }


@contextmanager
def bind(session, owner=None):
    token = _session.set(session)
    owner_token = _owner.set(owner) if owner is not None else None
    try:
        yield
    finally:
        if owner_token is not None:
            _owner.reset(owner_token)
        _session.reset(token)
