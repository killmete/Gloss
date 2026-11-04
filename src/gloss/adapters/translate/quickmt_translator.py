"""Placeholder for the optional ``quickmt`` offline translator. Not registered, not built yet.

Why it is a stub: ADR-0001 section 5 makes ``quickmt`` an installable offline pack (a roughly
400 MB model plus ``ctranslate2`` and ``sentencepiece``, both already in the ``offline``
extra). A real adapter cannot be tested here without that pack and its model files, and the
contract suite would only skip it. Building it blind would mean shipping unverified code, so
this slice leaves it for the phase that installs and measures the pack.

What the adapter must do when it is built (from the ADR and the port docs):
- ``name = "quickmt"``, ``REQUIRES_CONTRACT = ">=1.0,<2.0"``, and ``LOCAL_ONLY = True`` on its
  own class, so it still works with ``privacy.cloud_translation = false``.
- Import ``ctranslate2`` and ``sentencepiece`` inside ``open()`` only. If the pack
  (``pack_dir`` in ``[translate.adapters.quickmt]``) or the libraries are missing, raise
  ``AdapterUnavailable``, so the chain falls to ``null``.
- Load the model on first use and unload it after ``idle_unload_s``, to respect the RAM target.
- Translate each line on its own. Ignore glossary and context, and mark every result
  ``rough=True`` so the interface can label it. Report ``usage=None`` (it costs nothing).
- Until registered, a ``fallbacks = ["quickmt", "null"]`` entry in config is skipped by the
  registry with a warning, which is the intended behaviour for "pack not installed".
"""
