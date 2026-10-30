"""Session-only password handles. Only random handles may enter saved jobs."""
import threading
import uuid

_values = {}
_lock = threading.Lock()


def remember(value, token=None):
    if not isinstance(value, str) or "\0" in value or len(value) > 256:
        raise ValueError("Passwords must be text, contain no nulls and be at most 256 characters.")
    token = token or uuid.uuid4().hex
    with _lock:
        _values[token] = value
    return token


def resolve(token):
    if not token:
        return ""
    with _lock:
        if token not in _values:
            raise ValueError("Re-enter the password for this saved document job.")
        return _values[token]


def forget(token):
    with _lock:
        _values.pop(token, None)
