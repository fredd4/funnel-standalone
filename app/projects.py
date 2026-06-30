"""
Project registry.

Loads the project configs once, builds one data-source instance per project
(via the data-source plugin registry), and exposes them through
`get_project()` / `PROJECTS`.

Two things are pluggable:
  * WHERE the config comes from — `settings.CONFIG_BACKEND` ("yaml" | "firestore").
  * WHICH data source each project uses — `source.type` per project (see
    app/data_sources/registry.py). Default "firestore".

Config is data, never secrets: a project points at credentials via ADC/IAM or a
`credentials_path`; the config itself (YAML file or Firestore doc) holds no keys.
"""
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional

import yaml

from app import settings
from app.data_sources.registry import build_source


@dataclass
class SourcePerformanceConfig:
    enabled: bool = False
    allowed_first_events: List[str] = field(default_factory=list)
    revenue_events: List[str] = field(default_factory=list)


@dataclass
class ProjectConfig:
    name: str
    label: str
    firestore_project_id: str
    collection_name: str
    credentials_path: Optional[str]
    timezone: str
    fields: Dict[str, str]
    log_names: Dict[str, str]
    ab_test_event_names: List[str]
    defaults: Dict[str, Any]
    source_performance: SourcePerformanceConfig
    source_type: str = "firestore"
    options: Dict[str, Any] = field(default_factory=dict)  # plugin-specific extras
    data_source: Any = None  # set in build()

    @classmethod
    def build(cls, name: str, raw: Dict[str, Any]) -> "ProjectConfig":
        sp_raw = raw.get("source_performance", {}) or {}
        # A project may describe its data source either with a `source:` block
        # (new, plugin form) or with the flat legacy keys. Prefer the block.
        src = raw.get("source", {}) or {}
        cfg = cls(
            name=name,
            label=raw.get("label", name),
            firestore_project_id=(src.get("project_id")
                                  or raw.get("firestore_project_id")),
            collection_name=(src.get("collection_name")
                             or raw.get("collection_name", "logs")),
            credentials_path=(src.get("credentials_path")
                              if "credentials_path" in src
                              else raw.get("credentials_path")),
            timezone=raw.get("timezone", "UTC"),
            fields=raw.get("fields", {}),
            log_names=raw.get("log_names", {}),
            ab_test_event_names=list(raw.get("ab_test_event_names", [])),
            defaults=raw.get("defaults", {}),
            source_performance=SourcePerformanceConfig(
                enabled=sp_raw.get("enabled", False),
                allowed_first_events=list(sp_raw.get("allowed_first_events", [])),
                revenue_events=list(sp_raw.get("revenue_events", [])),
            ),
            source_type=src.get("type", "firestore"),
            options=dict(src.get("options", {})),
        )
        cfg.data_source = build_source(cfg)
        return cfg


# --- config loaders (pluggable: yaml | firestore) ----------------------------
def _load_raw_yaml() -> Dict[str, Dict[str, Any]]:
    with open(settings.PROJECTS_CONFIG_PATH, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return raw.get("projects") or {}


def _load_raw_firestore() -> Dict[str, Dict[str, Any]]:
    """Each document in CONFIG_COLLECTION is one project; doc id == project name."""
    from google.cloud import firestore

    client = (firestore.Client(project=settings.CONFIG_FIRESTORE_PROJECT_ID)
              if settings.CONFIG_FIRESTORE_PROJECT_ID else firestore.Client())
    out: Dict[str, Dict[str, Any]] = {}
    for doc in client.collection(settings.CONFIG_COLLECTION).stream():
        out[doc.id] = doc.to_dict() or {}
    return out


_LOADERS = {"yaml": _load_raw_yaml, "firestore": _load_raw_firestore}


@lru_cache(maxsize=1)
def _load() -> Dict[str, ProjectConfig]:
    backend = settings.CONFIG_BACKEND
    if backend not in _LOADERS:
        raise ValueError(
            f"Unknown CONFIG_BACKEND '{backend}'. Known: {sorted(_LOADERS)}")
    raw_projects = _LOADERS[backend]()
    return {name: ProjectConfig.build(name, body)
            for name, body in raw_projects.items()}


def get_project(name: str) -> ProjectConfig:
    projects = _load()
    if name not in projects:
        raise KeyError(f"Unknown project '{name}'. Known: {list(projects)}")
    return projects[name]


# Convenience handle: all configured projects, keyed by name.
PROJECTS = _load()
