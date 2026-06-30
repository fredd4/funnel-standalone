"""
Data-source plugin registry.

A data source is any class that knows how to fetch users/logs for a project
(see app/data_sources/base.py:UserDataSource). Each one registers under a short
`type` string; a project's config picks the type via its `source.type` field
(default "firestore"). Adding a new backend (BigQuery, an external Firebase
collection, a REST API, …) is therefore: write a UserDataSource subclass, mark
it with @register("yourtype"), and select it from config — no other code changes.
"""
from typing import Dict, Type

_REGISTRY: Dict[str, Type] = {}


def register(name: str):
    """Class decorator: register a data-source class under `name`."""
    def deco(cls):
        _REGISTRY[name] = cls
        return cls
    return deco


def _ensure_builtins() -> None:
    # Import built-in source modules so their @register decorators run. Imported
    # lazily to avoid import cycles (projects -> registry -> source -> base).
    from app.data_sources import firestore_source  # noqa: F401


def build_source(cfg):
    """Instantiate the data source selected by `cfg.source_type`."""
    _ensure_builtins()
    source_type = getattr(cfg, "source_type", None) or "firestore"
    if source_type not in _REGISTRY:
        raise ValueError(
            f"Unknown data source type '{source_type}'. Known: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[source_type](cfg)
