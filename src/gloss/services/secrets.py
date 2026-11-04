"""Secret storage in Windows Credential Manager (via ``keyring``), and the first-run key flow.

Never logs, prints or puts a secret in an exception message or ``repr``. Never reads ``.env``
files or environment variables for keys.

How the UI uses this (the "enter your API key" dialog)
------------------------------------------------------
::

    manager = ApiKeyManager(KeyringSecretStore(), secret_name)   # secret_name = settings
                                                                 # "secret_name" ("anthropic")
    manager.has_api_key()              # False -> show the dialog on first run
    manager.check_format(text)         # instant, offline: None if fine, else a message to show
    manager.set_api_key(text)          # strips pasted whitespace, then stores
    manager.clear_api_key()            # "remove my key" button

To test a stored key, open the translator (``registry.create(...)`` then ``open()``, then
``close()``) and show the exception message. ``open()`` looks the model up, which sends no
text and costs nothing, and raises plain-English errors. It cannot tell if the account has
credit: that only shows when a translation is requested. Run it off the UI thread.

Where the key lives: keyring service ``gloss/<secret_name>`` (``gloss/anthropic``) and
username ``api_key``. After ``set_api_key`` a running translator must be re-opened to see it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gloss.core.contracts import PermanentError

log = logging.getLogger(__name__)

SERVICE_PREFIX = "gloss"
USERNAME = "api_key"
MIN_KEY_LENGTH = 20


class ThrowawayBackend:
    """An in-memory keyring backend for tests. Never touches the Credential Manager.

    Two instances made with the same ``path`` share one store, like two runs of the app
    sharing one Credential Manager.
    """

    _shared: dict[str, dict[tuple[str, str], str]] = {}
    _guard = threading.Lock()

    def __init__(self, path: Path | str | None = None) -> None:
        with self._guard:
            self._data = self._shared.setdefault(str(path), {}) if path is not None else {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._data.pop((service, username), None)

    def __repr__(self) -> str:
        return f"ThrowawayBackend({len(self._data)} secrets)"


class KeyringSecretStore:
    """Implements ``SecretStore`` over Windows Credential Manager.

    A secret called ``name`` is stored under service ``gloss/<name>`` and username
    ``api_key``. ``backend`` is for tests: any object with keyring's
    ``get_password``/``set_password``/``delete_password``. The real ``keyring`` module is
    imported only when it is first needed.
    """

    CONTRACT_PORT = "secret"

    def __init__(
        self,
        backend: Any = None,
        *,
        service_prefix: str = SERVICE_PREFIX,
        username: str = USERNAME,
    ) -> None:
        self._backend = backend
        self._prefix = service_prefix
        self._username = username

    @classmethod
    def for_contract_test(cls, path: Path) -> KeyringSecretStore:
        return cls(ThrowawayBackend(path))

    def _keyring(self) -> Any:
        if self._backend is None:
            import keyring

            self._backend = keyring
        return self._backend

    def _service(self, name: str) -> str:
        return f"{self._prefix}/{name}"

    def _call(self, action: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:
            if type(exc).__name__ == "PasswordDeleteError" and action == "delete":
                return None  # Deleting a secret that is not there is not an error.
            log.warning("Credential store %s failed (%s)", action, type(exc).__name__)
            raise PermanentError("the Windows Credential Manager could not be used") from None

    def get(self, name: str) -> str | None:
        service = self._service(name)
        value = self._call("read", lambda: self._keyring().get_password(service, self._username))
        return value if isinstance(value, str) else None

    def set(self, name: str, value: str) -> None:
        service = self._service(name)
        self._call("write", lambda: self._keyring().set_password(service, self._username, value))

    def delete(self, name: str) -> None:
        service = self._service(name)
        self._call("delete", lambda: self._keyring().delete_password(service, self._username))

    def __repr__(self) -> str:
        return "KeyringSecretStore()"


def clean_key(text: str) -> str:
    """Remove the whitespace and quotes people pick up when pasting."""
    return text.strip().strip("\"'").strip()


def check_key_format(text: str) -> str | None:
    """Return a message if ``text`` cannot be an API key, or None. Offline and instant."""
    key = clean_key(text)
    if not key:
        return "Paste your API key first."
    if any(ch.isspace() for ch in key) or not key.isascii():
        return "That does not look like an API key. It has spaces or unusual characters."
    if len(key) < MIN_KEY_LENGTH:
        return "That key looks too short. Copy the whole key from the provider's console."
    return None


class ApiKeyManager:
    """The small API the first-run dialog needs. See the module docstring."""

    def __init__(self, store: Any, secret_name: str) -> None:
        self._store = store
        self._name = secret_name

    def has_api_key(self) -> bool:
        value = self._store.get(self._name)
        return bool(value and value.strip())

    def set_api_key(self, key: str) -> None:
        """Store the key after cleaning pasted whitespace. Raises ValueError if unusable."""
        problem = check_key_format(key)
        if problem:
            raise ValueError(problem)
        self._store.set(self._name, clean_key(key))

    def clear_api_key(self) -> None:
        self._store.delete(self._name)

    @staticmethod
    def check_format(key: str) -> str | None:
        return check_key_format(key)
