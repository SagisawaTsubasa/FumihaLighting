"""Platform shim: HA resolves platform modules at the integration root.

The merged integration keeps the light platform in the lux subpackage; this
re-export makes `custom_components.fumiha_lighting.light` importable (a
missing root module surfaced as `setup_error` on the deployed instance).
"""

from .lux.light import async_setup_entry

__all__ = ["async_setup_entry"]
