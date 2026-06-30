"""
Abstract data-source layer.

Defines the PUser / PLog interface, the project-scoped caching (TTL) logic, and
the high-level get_users / get_logs / get_logs_for_user helpers that the funnel
engine relies on. Concrete sources (e.g. FirestoreDataSource) subclass
UserDataSource and implement the per-day fetch methods.
"""
import time
from datetime import datetime, timedelta, timezone, date
from functools import cached_property

from app.cache import cache as cache_backend


class PUser:
    def __str__(self):
        return f"User(user_id={self.user_id}, platform={self.platform}, created_at={self.created_at})"

    def __repr__(self):
        return self.__str__()

    def get_user_id_for_url(self):
        return f"{self.created_at.strftime('%Y-%m-%d')}_{self.id}"

    def get_user_id_with_date(self):
        return self.get_user_id_for_url()

    def is_signed_up(self):
        return True

    def get_created_at(self):
        return self.created_at

    @cached_property
    def campaign_source_data(self):
        return {"source": None, "campaign_id": None, "adset_id": None, "ad_id": None}


class PLog:
    def __str__(self):
        return (
            f"Log(user_id={self.user_id}, platform={self.platform}, "
            f"created_at={self.created_at}, event_type={self.event_type})"
        )

    def __repr__(self):
        return self.__str__()

    @property
    def funnel_event_name(self):
        raise NotImplementedError

    @property
    def funnel_event_value(self):
        raise NotImplementedError

    def is_data_fresh(self):
        return True


class UserDataSource:
    """Base class with the project-scoped cache + day/range fetch helpers."""

    # Subclasses set these.
    project_id: str = None
    use_cache: bool = True
    timezone = None
    LOG_NAME_SESSION_START: str = None

    # --- caching ---
    def add_to_cache(self, key, value, for_date):
        key = f"{self.project_id}_{key}"
        timeout = 60 * 60 * 24 * 365
        if for_date == date.today():
            timeout = 5 * 60
        elif for_date >= date.today() - timedelta(2):
            timeout = 60 * 60
        cache_backend.set(key, value, timeout=timeout)

    def get_from_cache(self, key):
        if not self.use_cache:
            return None
        key = f"{self.project_id}_{key}"
        return cache_backend.get(key)

    def set_use_cache(self, use_cache: bool):
        self.use_cache = use_cache

    # --- to be implemented by concrete sources ---
    def get_all_users_of_day(self, for_date, order_by=None, reverse=False):
        raise NotImplementedError

    def get_all_logs_of_day(self, for_date, log_name=None, user_id=None, reverse=False):
        raise NotImplementedError

    # --- high level helpers ---
    def get_users(self, from_date, to_date, platform=None, version=None,
                  limit=None, order_by=None, reverse=False, update_progress=None):
        from_date = from_date.astimezone(timezone.utc)
        to_date = to_date.astimezone(timezone.utc)
        users = []
        days = (range((to_date - from_date).days, -1, -1) if reverse
                else range((to_date - from_date).days + 1))
        for day in days:
            d = from_date + timedelta(day)
            for user in self.get_all_users_of_day(d, order_by=order_by, reverse=reverse):
                if (from_date <= user.get_created_at() < to_date and
                        (not platform or user.platform == platform)):
                    users.append(user)
                    if limit and len(users) >= limit:
                        break
        return users

    def get_logs(self, from_dt, to_dt, log_name, reverse=False, update_progress=None):
        from_dt = from_dt.astimezone(timezone.utc)
        to_dt = to_dt.astimezone(timezone.utc)
        logs = []
        for day in range((to_dt - from_dt).days + 1):
            d = from_dt + timedelta(day)
            for log in self.get_all_logs_of_day(d, log_name, reverse=reverse):
                if from_dt <= log.timestamp < to_dt:
                    logs.append(log)
        return logs

    def get_logs_for_user(self, user, log_name=None, start_after=None, limit=None,
                          reverse=False, time_limit=None):
        start_time = time.time()
        if reverse:
            start_day = datetime.now().astimezone().date()
            days = range((start_day - user.get_created_at().date()).days, -1, -1)
        else:
            start_day = user.get_created_at().date()
            days = range((datetime.now().astimezone().date() - start_day).days + 1)
        all_logs = []
        for day in days:
            for_date = start_day + timedelta(day)
            all_logs += list(self.get_all_logs_of_day(for_date, log_name, user.id, reverse=reverse))
            if limit and len(all_logs) >= limit:
                break
            if time_limit and time.time() - start_time > time_limit:
                break
        return all_logs

    def get_user(self, user_id_with_date):
        date_str, user_id = user_id_with_date.split("_")
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        for day in range(8):
            for_date = d - timedelta(day)
            for user in self.get_all_users_of_day(for_date, order_by=self.LOG_NAME_SESSION_START):
                if user.id == user_id:
                    return user
        return None
