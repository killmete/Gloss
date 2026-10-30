"""The three failure types every adapter may raise. Behaviour is defined in ``ports.py``.

Messages may reach the status tooltip and the logs, so they must never contain API keys,
screenshot data or chat text.
"""


class GlossError(Exception):
    """Base class for all expected Gloss failures."""


class AdapterUnavailable(GlossError):
    """The adapter cannot start: missing DLL, model, API key or language pack."""


class TransientError(GlossError):
    """A temporary failure: timeout, HTTP 429, network blip.

    ``retry_after_s`` carries a server-supplied wait, if there was one.
    """

    def __init__(self, message: str = "", *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class PermanentError(GlossError):
    """A failure retrying cannot fix: bad config, invalid key, spend limit reached."""
