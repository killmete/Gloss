"""Application entry point.

Phase 2 placeholder: it proves the install and config load. The orchestrator wires the
real pipeline here in Phase 3.
"""

from gloss.core.config import load_config
from gloss.core.contracts import CONTRACT_VERSION


def main() -> int:
    loaded = load_config()
    for warning in loaded.warnings:
        print(f"Config warning: {warning}")
    print(f"Gloss contracts {CONTRACT_VERSION}. The app itself is not built yet (Phase 3).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
