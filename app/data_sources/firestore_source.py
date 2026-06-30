"""
Config-driven Firestore data source.

Every project-specific value (project id, collection, credentials, field names,
log-name constants, A/B event names) comes from a ProjectConfig, so the same
class serves any number of projects.
"""
import json
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional

from dateutil import tz
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import service_account

from app.data_sources.base import PLog, PUser, UserDataSource
from app.data_sources.registry import register


class FirestoreUser(PUser):
    def __init__(self, uid, created_at, platform=None, params=None):
        self.id = uid
        self.uid = uid
        self.user_id = uid
        self.created_at = created_at
        self.platform = platform
        self.params = params or {}

    def get_display_device_name(self):
        return self.params.get("client", {}).get("deviceType")

    def get_country_code(self):
        return self.params.get("client", {}).get("countryCode")


class FirestoreLog(PLog):
    """One log document. `cfg` carries the field mapping + A/B event names."""

    def __init__(self, doc: Dict[str, Any], cfg):
        self._cfg = cfg
        f = cfg.fields
        self.raw_doc = dict(doc)
        self.msg = doc.get(f["event_name"])
        self.value = doc.get(f["value"])
        self.uid = doc.get(f["uid"])
        self.params = doc.get(f["params"]) or {}
        self.event_params = self.params  # generic funnel helpers expect this
        self.timestamp = doc.get(f["timestamp"])

        if isinstance(self.timestamp, datetime) and self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)

        self.platform = self.params.get("client", {}).get("platform")
        self.event_type = self.msg
        self.created_at = self.timestamp
        self.user_id = self.uid

    # --- A/B split test handling ---
    @classmethod
    def split_ab_test_values(cls, raw_value):
        if raw_value is None:
            return []
        if isinstance(raw_value, (list, tuple, set)):
            return [str(v).strip() for v in raw_value if str(v).strip()]
        s = str(raw_value).strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        return [v.strip() for v in s.split(",") if v.strip()]

    @staticmethod
    def _split_ab_test_value(raw_value):
        value = str(raw_value or "").strip()
        if not value:
            return None, None
        for sep in ("_", ":"):
            if sep in value:
                name, variant = value.split(sep, 1)
                return name.strip() or None, variant.strip() or None
        return value, None

    @property
    def funnel_event_name(self):
        if self.msg in self._cfg.ab_test_event_names:
            test_name, _ = self._split_ab_test_value(self.value)
            if test_name:
                return f"{self.msg}: {test_name}"
        return self.msg

    @property
    def funnel_event_value(self):
        if self.msg in self._cfg.ab_test_event_names:
            _, variant = self._split_ab_test_value(self.value)
            if variant:
                return [variant]
            return [] if self.value is None else [str(self.value)]
        return [] if self.value is None else [str(self.value)]

    # --- param extraction ---
    @staticmethod
    def _normalize_param_value(raw):
        if raw is None:
            return None
        if isinstance(raw, dict):
            extracted = (raw.get("string_value") or raw.get("int_value")
                         or raw.get("double_value") or raw.get("bool_value"))
            return str(extracted).strip() or None if extracted is not None else None
        if isinstance(raw, (list, tuple, set)):
            vals = [str(v).strip() for v in raw if str(v).strip()]
            return ",".join(vals) if vals else None
        return str(raw).strip() or None

    def get_param_value(self, key):
        if not key:
            return None
        value = self._normalize_param_value(self.params.get(key))
        if value is not None:
            return value
        client = self.params.get("client")
        if isinstance(client, dict):
            return self._normalize_param_value(client.get(key))
        return None

    def get_country_code(self):
        return self.params.get("client", {}).get("countryCode")

    def get_pretty_params_json(self):
        return json.dumps(self.params or {}, indent=2, sort_keys=True, default=str)


@register("firestore")
class FirestoreDataSource(UserDataSource):
    TIMESTAMP_MULTIPLIER = 1

    def __init__(self, cfg):
        self.cfg = cfg
        self.project_id = cfg.firestore_project_id
        self.collection_name = cfg.collection_name
        self.timezone = tz.gettz(cfg.timezone) if isinstance(cfg.timezone, str) else cfg.timezone
        self.use_cache = True

        # Expose the LOG_NAME_* constants the funnel engine / forms read.
        ln = cfg.log_names
        self.LOG_NAME_FIRST_OPEN = ln.get("first_open")
        self.LOG_NAME_SESSION_START = ln.get("session_start")
        self.LOG_NAME_WEB_FIRST_OPEN = ln.get("web_first_open")
        self.LOG_NAME_INITIAL_PURCHASE = ln.get("initial_purchase")
        self.LOG_NAME_RENEWAL = ln.get("renewal")
        self.LOG_NAME_NON_RENEWING_PURCHASE = ln.get("non_renewing_purchase")

    @property
    def client(self):
        if not hasattr(self, "_client"):
            if self.cfg.credentials_path:
                credentials = service_account.Credentials.from_service_account_file(
                    self.cfg.credentials_path
                )
                self._client = firestore.Client(project=self.project_id, credentials=credentials)
            else:
                # Application Default Credentials (e.g. Cloud Run service account).
                self._client = firestore.Client(project=self.project_id)
        return self._client

    def _day_range(self, for_datetime):
        if isinstance(for_datetime, date) and not isinstance(for_datetime, datetime):
            for_datetime = datetime.combine(for_datetime, datetime.min.time())
        for_datetime = for_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        start = for_datetime.astimezone(timezone.utc)
        end = (for_datetime + timedelta(1)).astimezone(timezone.utc)
        return start, end

    def get_all_logs_of_day(self, for_datetime, log_name=None, user_id=None, reverse=False):
        start, end = self._day_range(for_datetime)
        if start.date() > datetime.now(timezone.utc).date():
            return []

        key = f"{start.date()}_{log_name}_{user_id}_{reverse}"
        if (value := self.get_from_cache(key)) is not None:
            return value

        f = self.cfg.fields
        query = (self.client.collection(self.collection_name)
                 .where(filter=FieldFilter(f["timestamp"], ">=", start))
                 .where(filter=FieldFilter(f["timestamp"], "<", end)))
        if log_name:
            query = query.where(filter=FieldFilter(f["event_name"], "==", log_name))
        if user_id:
            query = query.where(filter=FieldFilter(f["uid"], "==", user_id))
        direction = firestore.Query.DESCENDING if reverse else firestore.Query.ASCENDING
        query = query.order_by(f["timestamp"], direction=direction)

        logs: List[FirestoreLog] = []
        for doc in query.stream():
            data = doc.to_dict() or {}
            event_name = data.get(f["event_name"])
            if event_name in self.cfg.ab_test_event_names:
                splits = FirestoreLog.split_ab_test_values(data.get(f["value"]))
                if splits:
                    for sv in splits:
                        sd = dict(data)
                        sd[f["value"]] = sv
                        logs.append(FirestoreLog(sd, self.cfg))
                    continue
            logs.append(FirestoreLog(data, self.cfg))

        self.add_to_cache(key, logs, for_date=start.date())
        return logs

    def get_all_users_of_day(self, for_datetime, order_by=None, reverse=False):
        start, end = self._day_range(for_datetime)
        if start.date() > datetime.now(timezone.utc).date():
            return []

        key = f"users_{start.date()}_{order_by}_{reverse}"
        if (value := self.get_from_cache(key)) is not None:
            return value

        logs = self.get_all_logs_of_day(start, log_name=None, reverse=False)
        users_by_uid: Dict[str, FirestoreUser] = {}
        for log in logs:
            if not log.uid or not log.timestamp:
                continue
            if log.uid not in users_by_uid or log.timestamp < users_by_uid[log.uid].created_at:
                users_by_uid[log.uid] = FirestoreUser(
                    uid=log.uid, created_at=log.timestamp,
                    platform=log.platform, params=log.params,
                )
        users = list(users_by_uid.values())
        if reverse:
            users.sort(key=lambda u: u.created_at, reverse=True)
        self.add_to_cache(key, users, for_date=start.date())
        return users
