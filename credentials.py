"""Local credentials kept outside the collection database and its exports."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import tempfile
import threading

_path = ContextVar('cinebook_credentials_path', default=None)
_lock = threading.RLock()


@contextmanager
def using_credentials(path):
    token = _path.set(path)
    try:
        yield
    finally:
        _path.reset(token)


def read_credentials(path=None):
    path = path or _path.get()
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError('Invalid credentials file.')
    return data


def update_credentials(path, changes, remove=()):
    # Atomic replacement prevents the worker from seeing a partially written key.
    path = Path(path)
    with _lock:
        data = read_credentials(path)
        data.update(changes)
        for field in remove:
            data.pop(field, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
                temporary = stream.name
                os.chmod(temporary, 0o600)
                json.dump(data, stream)
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
