"""Platform shim: HA resolves platform modules at the integration root.

The merged integration keeps the number platform in the lux subpackage; this
re-export makes `custom_components.fumiha_lighting.number` importable (a
missing root module surfaced as `setup_error` on the deployed instance).
"""

from .lux.number import async_setup_entry

__all__ = ["async_setup_entry"]
