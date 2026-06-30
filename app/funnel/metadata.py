"""
Funnel metadata persistence. Two kinds of small, Firestore-backed metadata:

  * first/last-seen dates of A/B-test events
    (used by FunnelEvent.ab_test_dates + ABTestTracker)
  * dynamic filter option lists (language / system language / onboarding name)
    discovered while scanning logs

Both are stored as small Firestore documents (well under the 1 MB limit), keyed
by project so multiple projects stay isolated.
"""
import json
from datetime import datetime
from typing import Iterable, Optional

from app import settings


def _col():
    from google.cloud import firestore
    return firestore.Client().collection(settings.METADATA_COLLECTION)


# --- A/B test first/last seen (was LogFirstAndLastSeen) ----------------------
def _ab_doc_id(app_name, name, onboarding_name=None):
    return f"ab__{app_name}__{onboarding_name or ''}__{name}"


def ab_test_dates(app_name: str, name: str, onboarding_name: Optional[str] = None):
    """Return {'first': 'YYYY-MM-DD', 'last': 'YYYY-MM-DD'} or None."""
    if not settings.METADATA_ENABLED:
        return None
    try:
        doc = _col().document(_ab_doc_id(app_name, name, onboarding_name)).get()
    except Exception:
        return None
    if not doc.exists:
        return None
    d = doc.to_dict()
    return {"first": d.get("first_seen"), "last": d.get("last_seen")}


class ABTestTracker:
    """Firestore-backed tracker of first/last-seen dates for A/B-test events.

    Accumulates first/last-seen timestamps in memory while scanning logs, then
    flush() persists them. Keep the same process_log() interface the engine uses.
    """

    def __init__(self, app_name: str):
        self.app_name = app_name
        self._seen = {}  # (name, onboarding) -> [first_dt, last_dt]

    def update_test_dates(self, log_name, timestamp, onboarding_name=None):
        if not ("split test" in log_name or "AB Test" in log_name):
            return
        key = (log_name, onboarding_name)
        if key not in self._seen:
            self._seen[key] = [timestamp, timestamp]
        else:
            first, last = self._seen[key]
            self._seen[key] = [min(first, timestamp), max(last, timestamp)]

    def process_log(self, log):
        name = getattr(log, "funnel_event_name", None)
        ts = getattr(log, "timestamp", None)
        if name and ts:
            onboarding = (getattr(log, "params", {}) or {}).get("onboardingName")
            self.update_test_dates(name, ts, onboarding)

    def flush(self):
        if not settings.METADATA_ENABLED:
            return
        col = _col()
        for (name, onboarding), (first, last) in self._seen.items():
            col.document(_ab_doc_id(self.app_name, name, onboarding)).set({
                "app_name": self.app_name,
                "name": name,
                "onboarding_name": onboarding,
                "first_seen": first.strftime("%Y-%m-%d"),
                "last_seen": last.strftime("%Y-%m-%d"),
                "updated_at": datetime.utcnow().isoformat(),
            })


# --- Dynamic filter options ------------------------------------------------
def _opt_doc_id(app_name, option_type):
    return f"opts__{app_name}__{option_type}"


def get_filter_options(app_name: str, option_type: str):
    if not settings.METADATA_ENABLED:
        return []
    try:
        doc = _col().document(_opt_doc_id(app_name, option_type)).get()
    except Exception:
        return []
    if not doc.exists:
        return []
    try:
        return json.loads(doc.to_dict().get("values", "[]"))
    except (json.JSONDecodeError, TypeError):
        return []


def update_filter_options(app_name: str, option_type: str, new_values: Iterable[str]):
    if not settings.METADATA_ENABLED:
        return []
    options = set(get_filter_options(app_name, option_type))
    for value in new_values:
        if value and value.strip():
            options.add(value.strip())
    updated = sorted(options)
    _col().document(_opt_doc_id(app_name, option_type)).set({"values": json.dumps(updated)})
    return updated
