"""Contract version. See CLAUDE.md rule 2 for when and how it changes.

Additive, optional-with-default change: minor bump. Rename or removal: major bump.
Adapters declare ``REQUIRES_CONTRACT = ">=1.0,<2.0"``, and the registry enforces it.
"""

CONTRACT_VERSION = "1.0.0"
