"""Platform shim: HA resolves platform modules at the integration root.

The merged integration keeps the button platform in the lux subpackage; this
re-export makes `custom_components.fumiha_lighting.button` importable (a
missing root module surfaced as `setup_error` on the deployed instance).
"""

from .lux.button import async_setup_entry

__all__ = ["async_setup_entry"]
