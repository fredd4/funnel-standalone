import time
import re
from decimal import Decimal, InvalidOperation
from collections import defaultdict, deque
from datetime import timedelta, date
from functools import cached_property
from statistics import median
from collections import Counter

from app.projects import get_project
from app.funnel.ab_confidence import ABConfidence
from app.funnel.metadata import ABTestTracker, ab_test_dates, update_filter_options
from app.currency import convert_to_eur


class FunnelEvent:
    PURCHASE_EVENTS = [
        "[RevenueCat] INITIAL_PURCHASE",
        "[RevenueCat] RENEWAL",
        "[RevenueCat] NON_RENEWING_PURCHASE"
    ]

    def __init__(self, event_name):
        self.event_name = event_name
        self.src = defaultdict(int)
        self.dst = defaultdict(int)
        self.count = 0
        self.total_occurrences = 0
        self.users = set()
        self.event_values = defaultdict(int)
        self.times_to_next_step = []
        self.total_revenue = 0.0  # Track total revenue for purchase events
        self.purchase_count = 0   # Track number of purchases

        self.first_event_count = 0
        self.next_event_count = 0
        self.was_used = False
        self.breakdown_value_order = None
        self._ab_test_dates = None  # Cache for AB test dates
        self._confidence_rate = None  # Cache for confidence rate
        self._control_event = None  # Reference to control event for comparison

    def __str__(self):
        if self.event_name in self.PURCHASE_EVENTS:
            return f"{self.event_name}: {self.count} (Total Revenue: ${self.total_revenue:.2f})"
        return f"{self.event_name}: {self.count}"

    def add_event(self, log, previous_event_name=None, previous_event_time=None, funnel_events=None, stripe_customers=None, override_uid=None):
        self.count += 1
        uid = override_uid if override_uid is not None else getattr(log, 'uid', None)
        if uid not in self.users and log.funnel_event_value:
            for value in log.funnel_event_value:
                self.event_values[value] += 1
        if uid is not None:
            self.users.add(uid)

        # Handle purchase events
        if self.event_name in self.PURCHASE_EVENTS and hasattr(log, 'params'):
            params = log.params
            price = params.get('price')
            currency = params.get('currency')
            period_type = params.get('period_type')
            
            # Store period type in event values for analysis
            if period_type:
                self.event_values[f"type_{period_type}"] += 1
            
            # Skip if no price (e.g., trial periods) or missing currency
            if price is not None and currency is not None:
                # For now, we'll assume USD. In a real implementation, you might want to
                # add currency conversion logic here
                if currency == 'USD':
                    price = float(price)  # Will raise ValueError if price is not a valid number
                    self.total_revenue += price
                    self.purchase_count += 1
                    # Store prices in event_values for breakdown, grouped by price point
                    price_key = f"${price:.2f}"
                    self.event_values[price_key] += 1

        if previous_event_name:
            self.src[previous_event_name] += 1
            if previous_event_time and funnel_events:
                time_diff = (log.timestamp - previous_event_time).total_seconds()
                # Store the time_diff in the previous event's times_to_next_step
                previous_event = funnel_events[previous_event_name]
                previous_event.times_to_next_step.append(time_diff)

    def add_stripe_event(self, log, stripe_customers, previous_event_name=None, override_uid=None):
        uid = override_uid if override_uid is not None else getattr(log, 'uid', None)
        if stripe_customers and uid in stripe_customers:
            if uid is not None:
                self.users.add(uid)
            customer = stripe_customers[uid]
            self.event_values["stripe_total_revenue"] += int(customer.total_value / 100) #todo: currency conversion
            self.count = self.event_values["stripe_total_revenue"]
            self.event_values["stripe_projected_revenue"] += int(customer.projected_value / 100)
            if customer.subscription_status == "active":
                self.event_values["stripe_active_subscribers"] += 1
            else:
                self.event_values["stripe_inactive_subscribers"] += 1

        if previous_event_name:
            self.src[previous_event_name] += 1

    def add_dst_event(self, event_name):
        self.dst[event_name] += 1


    @cached_property
    def src_sorted(self):
        return sorted(self.src.items(), key=lambda item: item[1], reverse=True)

    @cached_property
    def dst_sorted(self):
        return sorted(self.dst.items(), key=lambda item: item[1], reverse=True)

    @cached_property
    def event_values_sorted(self):
        return sorted(self.event_values.items(), key=lambda item: item[1], reverse=True)
    @property
    def count_unique_users(self):
        return len(self.users)

    @property
    def conversion_till_event(self):
        return self.count_unique_users / self.first_event_count * 100 if self.first_event_count else 0
    @property
    def dropoff(self):
        return 100 - self.next_event_count / self.count * 100 if self.count else 0

    @property
    def median_time_to_next_step(self):
        if not self.times_to_next_step:
            return None
        return median(self.times_to_next_step)

    def get_breakdown_events(self):
        if self.breakdown_value_order:
            return [self.breakdowns[value] for value in self.breakdown_value_order]
        return None

    def get_value_for_breakdown(self):
        if self.event_name == "stripe_revenue":
            return self.event_values.get("stripe_total_revenue", 0)
        if self.event_name in self.PURCHASE_EVENTS:
            return self.total_revenue
        return self.count_unique_users

    @property
    def ab_test_dates(self):
        """Get the first and last seen dates for this event if it's an AB test."""
        if self._ab_test_dates is not None:
            return self._ab_test_dates
            
        if not ("split test" in self.event_name or "AB Test" in self.event_name):
            self._ab_test_dates = None
            return None
            
        app_name = getattr(self, '_app_name', None)
        onboarding_name = getattr(self, '_onboarding_name', None)
        if not app_name:
            self._ab_test_dates = None
            return None
        self._ab_test_dates = ab_test_dates(app_name, self.event_name, onboarding_name)
        return self._ab_test_dates

    @property
    def display_value(self):
        """Returns the appropriate value for display in the funnel table"""
        if self.event_name in self.PURCHASE_EVENTS:
            avg_price = self.total_revenue / self.purchase_count if self.purchase_count > 0 else 0
            return f"${self.total_revenue:.2f}<br/><small>({self.count_unique_users} users, avg ${avg_price:.2f})</small>"
        
        return str(self.count_unique_users)
    
    @property
    def is_ab_test(self):
        """Check if this event represents an A/B test"""
        event_lower = self.event_name.lower()
        return ("split test" in event_lower or 
                "ab test" in event_lower or
                "split_test" in event_lower or
                "ab_test" in event_lower or
                ("variant" in event_lower and ("_a" in event_lower or "_b" in event_lower)) or
                "control" in event_lower)
    
    @property
    def ab_test_variant(self):
        """Extract variant name from A/B test event name"""
        if not self.is_ab_test:
            return None
        
        # Try to extract variant from common patterns
        event_lower = self.event_name.lower()
        if "variant a" in event_lower or "version a" in event_lower:
            return "A"
        elif "variant b" in event_lower or "version b" in event_lower:
            return "B"
        elif "control" in event_lower:
            return "Control"
        elif "test" in event_lower and "control" not in event_lower:
            return "Test"
        
        # Try to extract from event values (common A/B test value patterns)
        for value, count in self.event_values_sorted:
            if isinstance(value, str):
                value_lower = value.lower()
                if value_lower in ['a', 'variant_a', 'version_a']:
                    return "A"
                elif value_lower in ['b', 'variant_b', 'version_b']:
                    return "B"
                elif value_lower in ['control']:
                    return "Control"
        
        return "Unknown"
    
    @property
    def confidence_rate(self):
        """Calculate confidence rate against control event"""
        if self._confidence_rate is not None:
            return self._confidence_rate
            
        if not self._control_event:
            return None
            
        try:
            # Skip if same event or insufficient data (lowered threshold - only need 2 users minimum)
            if (self._control_event.count_unique_users < 2 or 
                self.count_unique_users < 2 or
                self._control_event == self):
                return None
                
            # Create AB confidence calculation
            # For A/B testing, we compare conversion rates, not just user counts
            control_conversions = int(self._control_event.count_unique_users * self._control_event.conversion_till_event / 100)
            test_conversions = int(self.count_unique_users * self.conversion_till_event / 100)
            
            # Handle edge case where we have zero conversions but still want to show confidence
            # ABConfidence needs at least 1 conversion in both variants to avoid division by zero
            adjusted_control_conversions = max(1, control_conversions)
            adjusted_test_conversions = max(1, test_conversions)
            
            base = ABConfidence(
                self._control_event.count_unique_users, 
                adjusted_control_conversions
            )
            base.add_data_set(self.count_unique_users, adjusted_test_conversions)
            
            # Return confidence as percentage
            self._confidence_rate = base[0][3] * 100
            return self._confidence_rate
            
        except (IndexError, ZeroDivisionError, AttributeError):
            return None
    
    @property
    def statistical_significance(self):
        """Check if the result is statistically significant (>95% confidence)"""
        confidence = self.confidence_rate
        return confidence is not None and confidence >= 95.0
    
    @property
    def display_value_enhanced(self):
        """Enhanced display prioritizing conversion rate for A/B tests"""
        if self.is_ab_test:
            conversion = self.conversion_till_event
            confidence = self.confidence_rate
            significance_marker = "✓" if self.statistical_significance else "?"
            
            confidence_text = f"{confidence:.1f}%" if confidence else "N/A"
            
            return f"""<strong>{conversion:.1f}%</strong> conversion<br/>
<small>({self.count_unique_users} users)</small><br/>
<small class="text-muted">Confidence: {confidence_text} {significance_marker}</small>"""
        
        # Use original display logic for non-A/B tests
        return self.display_value
    
    def set_control_event(self, control_event):
        """Set the control event for confidence rate calculation"""
        self._control_event = control_event
        self._confidence_rate = None  # Reset cache
    
    def get_performance_vs_control(self):
        """Get performance comparison vs control event"""
        if not self._control_event or not self.is_ab_test:
            return None
            
        control_conversion = self._control_event.conversion_till_event
        current_conversion = self.conversion_till_event
        
        if control_conversion == 0:
            return None
            
        lift = ((current_conversion - control_conversion) / control_conversion) * 100
        return {
            'lift_percentage': lift,
            'is_improvement': lift > 0,
            'control_conversion': control_conversion,
            'current_conversion': current_conversion
        }
    
    def get_best_performing_variant(self):
        """Get the best performing variant from breakdown events"""
        if not hasattr(self, 'breakdowns') or not self.breakdowns:
            return None
            
        best_variant = None
        best_conversion = -1
        
        for breakdown_value, breakdown_event in self.breakdowns.items():
            if breakdown_event.conversion_till_event > best_conversion:
                best_conversion = breakdown_event.conversion_till_event
                best_variant = breakdown_event
                
        return best_variant

# ABTestTracker is imported from app.funnel.metadata (Firestore-backed).


def filter_funnel_events(best_funnel, simplify_result):
    if not simplify_result:
        return best_funnel

    filtered_funnel = []
    for i, event in enumerate(best_funnel):
        if any(event.count_unique_users < following_event.count_unique_users * 0.5 for following_event in best_funnel[i+1:]) and \
           not any(keyword in event.event_name for keyword in ["purchase", "trial", "rc_"]):
            continue
        filtered_funnel.append(event)

    return filtered_funnel


def _attach_total_occurrences_to_events(events, total_occurrences_map):
    if not events:
        return
    for event in events:
        event.total_occurrences = total_occurrences_map.get(event.event_name, event.count)
        if hasattr(event, "breakdowns") and event.breakdowns:
            for sub_event in event.breakdowns.values():
                sub_event.total_occurrences = total_occurrences_map.get(
                    sub_event.event_name, sub_event.count
                )


def _parse_metric_events(metric_events):
    if not metric_events:
        return []

    if isinstance(metric_events, str):
        raw_events = [event.strip() for event in metric_events.split(",")]
    else:
        raw_events = [str(event).strip() for event in metric_events]

    deduped_events = []
    seen = set()
    for event_name in raw_events:
        if not event_name or event_name in seen:
            continue
        seen.add(event_name)
        deduped_events.append(event_name)

    return deduped_events


def _extract_param_value(log, key):
    if not key:
        return None
    if hasattr(log, "get_param_value"):
        value = log.get_param_value(key)
        if value:
            return str(value)

    params = getattr(log, "params", None)
    if not isinstance(params, dict):
        return None
    value = params.get(key)
    if value is None:
        client = params.get("client")
        if isinstance(client, dict):
            value = client.get(key)
    if value is None:
        return None
    if isinstance(value, dict):
        value = (
            value.get("string_value")
            or value.get("int_value")
            or value.get("double_value")
            or value.get("bool_value")
        )
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _to_float(value):
    if value is None:
        return None
    if isinstance(value, dict):
        value = (
            value.get("double_value")
            or value.get("int_value")
            or value.get("string_value")
        )
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _extract_payment_amount(log):
    params = getattr(log, "params", None)
    if not isinstance(params, dict):
        return None, "missing_params"

    amount = _to_float(params.get("purchase_value"))
    if amount is not None:
        return amount, None

    # Cents fields (Firestore schema uses purchase_value_cents / price_cents).
    for cents_key in ("purchase_value_cents", "price_cents"):
        amount_cents = _to_float(params.get(cents_key))
        if amount_cents is not None:
            return amount_cents / 100.0, None

    for key in ("price", "amount", "revenue", "purchase_revenue"):
        amount = _to_float(params.get(key))
        if amount is not None:
            return amount, None

    # Last fallback for this event payload: top-level `value` often stores cents.
    top_level_value = _to_float(getattr(log, "value", None))
    if top_level_value is not None:
        return top_level_value / 100.0, None

    return None, "missing_amount"


def _extract_revenue_amount_eur(log):
    amount, amount_warning = _extract_payment_amount(log)
    if amount is None:
        return None, amount_warning

    currency = (
        _extract_param_value(log, "currency")
        or _extract_param_value(log, "priceCurrency")
        or _extract_param_value(log, "currency_code")
    )
    if not currency:
        return None, "missing_currency"

    try:
        eur_amount = convert_to_eur(Decimal(str(amount)), str(currency))
    except (ValueError, InvalidOperation):
        return None, "conversion_failed"
    if eur_amount is None:
        return None, "conversion_failed"
    return float(eur_amount), None


def _build_source_performance(logs_for_user, first_log, breakdown_param_key,
                              metric_events, allowed_first_events, revenue_events):
    if not breakdown_param_key or (allowed_first_events and first_log not in allowed_first_events):
        return None

    metric_events = _parse_metric_events(metric_events)
    source_rows = {}
    revenue_warnings = Counter()

    for user_id, logs in logs_for_user.items():
        if not logs:
            continue

        start_log = None
        for log in logs:
            if log.funnel_event_name == first_log:
                start_log = log
                break
        if not start_log:
            continue

        source_value = _extract_param_value(start_log, breakdown_param_key) or "unknown"
        if source_value not in source_rows:
            source_rows[source_value] = {
                "users": set(),
                "event_users": defaultdict(set),
                "revenue": 0.0,
            }

        row = source_rows[source_value]
        row["users"].add(user_id)
        start_ts = getattr(start_log, "timestamp", None)

        for log in logs:
            log_ts = getattr(log, "timestamp", None)
            if start_ts and log_ts and log_ts < start_ts:
                continue

            event_name = log.funnel_event_name
            if event_name in metric_events:
                row["event_users"][event_name].add(user_id)
            if event_name in revenue_events:
                revenue_amount, warning_key = _extract_revenue_amount_eur(log)
                if warning_key:
                    revenue_warnings[warning_key] += 1
                if revenue_amount is not None:
                    row["revenue"] += revenue_amount

    if not source_rows:
        return {
            "breakdown_param_key": breakdown_param_key,
            "metric_events": metric_events,
            "rows": [],
            "totals": {"entries": 0, "revenue": 0.0, "event_counts": {}},
            "revenue_warnings": {},
        }

    rows = []
    total_entries = 0
    total_revenue = 0.0
    total_event_counts = defaultdict(int)

    for source_value, raw_data in source_rows.items():
        entries = len(raw_data["users"])
        event_counts = {}
        event_percents = {}
        metric_values = []
        for event_name in metric_events:
            count = len(raw_data["event_users"][event_name])
            event_counts[event_name] = count
            event_percents[event_name] = (count / entries * 100) if entries else 0.0
            total_event_counts[event_name] += count
            metric_values.append(
                {
                    "event_name": event_name,
                    "count": count,
                    "percent": event_percents[event_name],
                }
            )

        revenue = round(raw_data["revenue"], 2)
        arpu = (revenue / entries) if entries else 0.0
        rows.append(
            {
                "source": source_value,
                "entries": entries,
                "event_counts": event_counts,
                "event_percents": event_percents,
                "metric_values": metric_values,
                "revenue": revenue,
                "arpu": arpu,
            }
        )
        total_entries += entries
        total_revenue += revenue

    rows.sort(key=lambda item: item["entries"], reverse=True)
    return {
        "breakdown_param_key": breakdown_param_key,
        "metric_events": metric_events,
        "totals_metric_values": [
            {"event_name": event_name, "count": total_event_counts[event_name]}
            for event_name in metric_events
        ],
        "rows": rows,
        "totals": {
            "entries": total_entries,
            "revenue": round(total_revenue, 2),
            "event_counts": dict(total_event_counts),
        },
        "revenue_warnings": dict(revenue_warnings),
    }


def get_funnel(app_name, date_range, first_log, country_codes=None, platform='iOS', 
               user_source=None, breakdown_by_event_value=None,
               event_regex=None, progress_callback=print, app_version=None, use_cache=True, additional_days=0,
               language=None, system_language=None, onboarding_name=None, 
               breakdown_by_second_event_value=None, breakdown_param_key=None, metric_events=None):
    """
    Generate a funnel analysis based on user events.
    
    Parameters:
        app_name: Name of the app to analyze
        date_range: Tuple of (start_date, end_date)
        first_log: Event name to use as the first step in the funnel
        country_codes: Optional list of country codes to filter by
        platform: Platform to filter by (iOS, Android) - web events with webonboarding_id are included regardless
        user_source: Optional source domain to filter by
        breakdown_by_event_value: Optional event name to use for breakdown analysis
        event_regex: Optional regex pattern to filter event names
        progress_callback: Function to call with progress updates
        app_version: Optional app version to filter by
        use_cache: Whether to use cached data for better performance
        additional_days: Number of days beyond date_range to include for event tracking
        language: Optional website language to filter by (e.g., 'en', 'es')
        system_language: Optional system language to filter by (e.g., 'en-US', 'es-ES')
        onboarding_name: Optional onboarding name to filter by (e.g., 'leadership', 'productivity')
        breakdown_by_second_event_value: Optional secondary A/B test event name for correlation analysis
        breakdown_param_key: Optional log param key used for source breakdown (when source_performance is enabled)
        metric_events: Optional list/comma-separated event names to include in source metrics
        
    Returns:
        Tuple of (best_funnel, not_used_events, status_log, source_performance_data)
    """
    start_time = time.time()
    project = get_project(app_name)
    data_source = project.data_source
    data_source.set_use_cache(use_cache)  # Set cache preference on data source

    # Initialize AB test tracker with app_name
    ab_tracker = ABTestTracker(app_name)

    # Stripe revenue enrichment was a lighthouse/BigQuery-only feature; not used here.
    stripe_customers = None

    status_log = []
    if not use_cache:
        status_log.append("Cache disabled - fetching fresh data")
    if progress_callback:
        progress_callback(f"Starting data collection for {app_name}")
    
    # Compile regex if provided.
    regex_pattern = None
    if event_regex:
        try:
            regex_pattern = re.compile(event_regex)
            status_log.append(f"Using event filter: {event_regex}")
        except re.error:
            status_log.append(f"Invalid regex pattern: {event_regex}")
    
    # Parse user sources if provided
    user_sources = None
    if user_source:
        user_sources = [source.strip() for source in user_source.split(',')]
        status_log.append(f"Filtering by user sources: {', '.join(user_sources)}")
    
    # Build list of dates including additional days for event tracking
    base_dates = [date_range[0] + timedelta(days=i) for i in range((date_range[1]-date_range[0]).days+1)]
    additional_dates = []
    if additional_days > 0:
        start_additional = date_range[1] + timedelta(days=1)
        additional_dates = [start_additional + timedelta(days=i) for i in range(additional_days)]
        status_log.append(f"Including {additional_days} additional days for event tracking")
    
    dates = base_dates + additional_dates
    total_dates = len(dates)
    
    # --- REDESIGNED MATCHING STRATEGY ---
    #
    # We use two main mapping dictionaries:
    # 1. webid_to_canonical_uid: Maps webonboarding_id to a canonical user ID
    # 2. uid_mapping: Maps any user ID to its canonical form
    #
    # The matching logic is simplified:
    # - When we see a webonboarding_id, we check if we've seen it before
    # - If yes, we use the canonical UID associated with that webonboarding_id
    # - If no, we create a new entry
    #
    # For consistency, we always follow UID mappings when necessary
    
    funnel_by_user = {}            # Maps canonical_uid -> {'start': timestamp, 'logs': [list of logs]}
    webid_to_canonical_uid = {}    # Maps webonboarding_id -> canonical_uid
    uid_mapping = {}               # Maps uid -> canonical_uid (for cases where we can't use webid)
    cached_event_names = defaultdict(set)  # Maps canonical_uid -> set of event names seen
    
    # Stats for debugging
    match_stats = {
        'total_logs': 0,
        'logs_with_webid': 0,
        'webid_matches': 0,
        'uid_mapping_fallbacks': 0,
        'unique_webids': 0,
        'canonical_uids': 0,
    }
    
    # Helper to extract webonboarding_id safely from any log
    def get_webid(log):
        """Extract webonboarding_id from log object safely."""
        # Check if the log has event_params attribute
        if not hasattr(log, 'event_params') or not log.event_params:
            return None
            
        # Check if event_params has user_webonboarding_id
        webid_param = log.event_params.get('user_webonboarding_id')
        if not webid_param:
            return None
            
        # Extract the string value (handle different potential structures)
        if isinstance(webid_param, dict) and 'string_value' in webid_param:
            webid = webid_param.get('string_value')
        elif isinstance(webid_param, str):
            webid = webid_param
        else:
            return None
            
        # Validate webid is a non-empty string
        if not webid or not isinstance(webid, str):
            return None
            
        return webid.strip()
    
    # Helper to get canonical user ID
    def get_canonical_uid(log):
        """Determine the canonical UID for a log entry based on webid or existing mappings."""
        # First check for webonboarding_id
        webid = get_webid(log)
        
        # Debug info for specific users of interest
        debug_uid = False
        if debug_uid and (log.uid == "TARGET_UID_HERE" or (webid and webid == "TARGET_WEBID_HERE")):
            print(f"DEBUG LOG: uid={log.uid}, webid={webid}, event={log.funnel_event_name}")
        
        if webid:
            match_stats['logs_with_webid'] += 1
            
            # If we've seen this webid before, use its canonical UID
            if webid in webid_to_canonical_uid:
                canonical_uid = webid_to_canonical_uid[webid]
                
                # Record the mapping of this log's uid to the canonical uid
                if log.uid and log.uid != canonical_uid:
                    match_stats['webid_matches'] += 1
                    uid_mapping[log.uid] = canonical_uid
                    if debug_uid:
                        print(f"WEBID MATCH: {log.uid} → {canonical_uid} via webid {webid}")
                    
                return canonical_uid
            
            # If this is a new webid, use this log's uid as canonical
            # (after checking if the uid already has a canonical form)
            if log.uid:
                new_canonical = uid_mapping.get(log.uid, log.uid)
                webid_to_canonical_uid[webid] = new_canonical
                match_stats['unique_webids'] += 1
                if debug_uid:
                    print(f"NEW WEBID: {webid} → {new_canonical}")
                return new_canonical
            else:
                # If no valid uid, use the webid itself as the uid
                # This ensures we don't lose logs with webid but no uid
                webid_to_canonical_uid[webid] = webid
                match_stats['unique_webids'] += 1
                if debug_uid:
                    print(f"WEBID AS UID: {webid}")
                return webid
        
        # No webid, check if we have an existing uid mapping
        if log.uid and log.uid in uid_mapping:
            match_stats['uid_mapping_fallbacks'] += 1
            if debug_uid:
                print(f"UID MAPPING: {log.uid} → {uid_mapping[log.uid]}")
            return uid_mapping[log.uid]
            
        # No mapping exists, use this log's uid as is
        if not log.uid:
            # Generate a placeholder UID for logs without any identifier
            # This is a fallback to prevent errors, but these logs won't be very useful
            placeholder = f"placeholder_{match_stats['total_logs']}"
            if debug_uid:
                print(f"NO UID: assigned {placeholder}")
            return placeholder
            
        return log.uid
    
    # Filter function for starting events
    def passes_filters(log):
        """Check if log passes all filters for inclusion."""
        # Exclude internal/QA traffic (Firestore schema flags it via params.isTester
        # for non-production hostnames). Such users must not enter a sales funnel.
        params = getattr(log, "params", None)
        if isinstance(params, dict) and params.get("isTester"):
            return False

        if country_codes and log.get_country_code() not in country_codes:
            return False

        # Platform filtering logic:
        # 1. If no platform filter specified, don't filter
        # 2. If platform filter is specified (comma-separated allowed), only filter out logs where:
        #    - log has a platform attribute AND
        #    - platform value is not null AND
        #    - platform is not among requested platforms (case-insensitive)
        if platform and hasattr(log, 'platform') and log.platform:
            # Normalize allowed platforms from string or iterable
            if isinstance(platform, (list, tuple, set)):
                allowed_platforms = {str(p).strip().lower() for p in platform if p is not None and str(p).strip()}
            else:
                allowed_platforms = {p.strip().lower() for p in str(platform).split(',') if p.strip()}
            current_platform = str(log.platform).lower()
            if allowed_platforms and current_platform not in allowed_platforms:
                return False
            
        if user_sources and (not hasattr(log, 'manual_source_domain') or 
                          log.manual_source_domain not in user_sources):
            return False
            
        if app_version and (not hasattr(log, 'app_version') or log.app_version != app_version):
            return False
        
        # New filter parameters
        if language and hasattr(log, 'params') and log.params:
            log_language = log.params.get('language')
            if log_language != language:
                return False
                
        if system_language and hasattr(log, 'params') and log.params:
            log_system_language = log.params.get('systemLanguage')
            if log_system_language != system_language:
                return False
                
        if onboarding_name and hasattr(log, 'params') and log.params:
            log_onboarding_name = log.params.get('onboardingName')
            if log_onboarding_name != onboarding_name:
                return False
            
        return True
    
    # Initialize counters for debug output
    event_counters = Counter()
    
    # Initialize statistics collectors
    language_stats = Counter()
    system_language_stats = Counter()
    onboarding_name_stats = Counter()
    
    # Collect values for Variable model updates
    languages_found = set()
    system_languages_found = set()
    onboarding_names_found = set()
    
    # First pass: Process all base dates to collect users and build mappings
    base_users = set()  # Set of canonical UIDs from base date range
    user_first_events = {}  # Track first events for users
    found_users_with_first_event = False  # Flag to check if we're finding any users
    
    for i, date in enumerate(base_dates, 1):
        if progress_callback:
            progress_callback(f"Pass 1: Processing logs for {date} ({i}/{len(base_dates)})")
            
        # Get all logs for this day
        daily_logs = data_source.get_all_logs_of_day(date)
        # Count event types for debugging
        for log in daily_logs:
            if hasattr(log, 'funnel_event_name') and log.funnel_event_name:
                event_counters[log.funnel_event_name] += 1

        # Sort by time to ensure we process events in chronological order
        daily_logs.sort(key=lambda log: log.timestamp)
        
        for log in daily_logs:
            match_stats['total_logs'] += 1
            
            # Process AB test logs for tracking
            ab_tracker.process_log(log)
            
            # Skip logs with no event name
            if not hasattr(log, 'funnel_event_name') or not log.funnel_event_name:
                continue
            
            # Get the canonical UID for this log
            canonical_uid = get_canonical_uid(log)
            
            # Do not mutate Firestore models; just remember canonical_uid for grouping
            
            # Process logs with first_event - we want ALL users with this first event
            if log.funnel_event_name == first_log:
                found_users_with_first_event = True
                
                # Apply filters
                if not passes_filters(log):
                    continue
                
                # Add this user to our base set if first time seeing this user with this event
                if canonical_uid not in base_users:
                    base_users.add(canonical_uid)
                    user_first_events[canonical_uid] = log
                    
                    # Initialize user in funnel
                    funnel_by_user[canonical_uid] = {
                        'start': log.timestamp,
                        'logs': [log]
                    }
                    cached_event_names[canonical_uid].add(log.funnel_event_name)
                    
                    # Record statistics
                    if hasattr(log, 'manual_source_domain') and log.manual_source_domain:
                        if 'source_counts' not in locals():
                            source_counts = defaultdict(int)
                        source_counts[log.manual_source_domain] += 1
                    if hasattr(log, 'app_version') and log.app_version:
                        if 'version_counts' not in locals():
                            version_counts = defaultdict(int)
                        version_counts[log.app_version] += 1
                    
                    # Collect language and onboarding statistics
                    if hasattr(log, 'params') and log.params:
                        log_language = log.params.get('language')
                        log_system_language = log.params.get('systemLanguage')
                        log_onboarding_name = log.params.get('onboardingName')
                        
                        if log_language:
                            language_stats[log_language] += 1
                            languages_found.add(log_language)
                        if log_system_language:
                            system_language_stats[log_system_language] += 1
                            system_languages_found.add(log_system_language)
                        if log_onboarding_name:
                            onboarding_name_stats[log_onboarding_name] += 1
                            onboarding_names_found.add(log_onboarding_name)
    
    if not found_users_with_first_event:
        # Debug output to help identify the issue
        progress_callback(f"WARNING: No users found with first event '{first_log}'")
        progress_callback(f"Available event types: {event_counters.most_common(10)}")
    
    progress_callback(f"Found {len(base_users)} users with '{first_log}' event")
    
    # Second pass: Process all logs for users we care about
    total_event_occurrences = Counter()
    for i, date in enumerate(dates, 1):
        if progress_callback:
            progress_callback(f"Pass 2: Processing events for {date} ({i}/{total_dates})")
                
        daily_logs = data_source.get_all_logs_of_day(date)
        daily_logs.sort(key=lambda log: log.timestamp)
        
        for log in daily_logs:
            match_stats['total_logs'] += 1
            
            # Skip logs with no event name
            if not hasattr(log, 'funnel_event_name') or not log.funnel_event_name:
                continue
            
            # Apply regex filtering if provided
            if regex_pattern and not regex_pattern.match(log.funnel_event_name):
                continue
                
            # Get canonical UID
            canonical_uid = get_canonical_uid(log)
            
            # Do not mutate Firestore models; rely on canonical_uid for grouping
            
            # Skip if not a base user - these users don't have the first_log event
            if canonical_uid not in base_users:
                continue
            
            # Process the log for users in our funnel (users with first_log event)
            if canonical_uid in funnel_by_user:
                user_start_ts = funnel_by_user[canonical_uid].get('start')
                if user_start_ts and hasattr(log, 'timestamp') and log.timestamp and log.timestamp < user_start_ts:
                    # Ignore events that happened before the selected first event.
                    continue
                # Count every occurrence for diagnostic total-events display.
                total_event_occurrences[log.funnel_event_name] += 1
                # For first log events, skip if we already have this event
                if log.funnel_event_name == first_log and log.funnel_event_name in cached_event_names[canonical_uid]:
                    # Check if this is the exact same log we already have (possible duplicate)
                    skip = False
                    for existing_log in funnel_by_user[canonical_uid]['logs']:
                        if (existing_log.funnel_event_name == log.funnel_event_name and
                            hasattr(existing_log, 'timestamp') and hasattr(log, 'timestamp') and
                            abs((existing_log.timestamp - log.timestamp).total_seconds()) < 1):
                            skip = True
                            break
                    if skip:
                        continue
                
                # Add event to user's funnel
                if log.funnel_event_name.startswith('server_'):
                    # Always add server events
                    funnel_by_user[canonical_uid]['logs'].append(log)
                    cached_event_names[canonical_uid].add(log.funnel_event_name)
                elif log.funnel_event_name not in cached_event_names[canonical_uid]:
                    # Add new event types
                    funnel_by_user[canonical_uid]['logs'].append(log)
                    cached_event_names[canonical_uid].add(log.funnel_event_name)
    
    # Count unique canonical UIDs
    match_stats['canonical_uids'] = len(funnel_by_user)

    # Report statistics
    progress_callback(f"Collected {match_stats['canonical_uids']} unique users")

    # Persist discovered A/B-test first/last-seen dates (best-effort).
    try:
        ab_tracker.flush()
    except Exception as e:
        status_log.append(f"Warning: Could not persist AB test dates: {str(e)}")

    # Update dynamic filter options with new values found
    try:
        if languages_found:
            update_filter_options(app_name, 'language', languages_found)
        if system_languages_found:
            update_filter_options(app_name, 'system_language', system_languages_found)
        if onboarding_names_found:
            update_filter_options(app_name, 'onboarding_name', onboarding_names_found)
    except Exception as e:
        status_log.append(f"Warning: Could not update filter options: {str(e)}")
    
    # IMPORTANT: First add the old format stats that the web interface expects
    status_log.append("\nUser matching statistics:")
    status_log.append(f"- Total logs processed: {match_stats['total_logs']}")
    status_log.append(f"- Matches by webonboarding_id: {match_stats['webid_matches']}")
    status_log.append(f"- Matches by uid mapping: {match_stats['uid_mapping_fallbacks']}")
    status_log.append(f"- Matches by heuristic: 0")  # We don't use heuristic matching anymore
    status_log.append(f"- Privacy deduplications: 0")  # We don't use privacy deduplication anymore
    status_log.append(f"- Total webonboarding_ids: {match_stats['unique_webids']}")
    
    # Then add our new format stats
    status_log.append("\nDetailed matching statistics:")
    
    # Calculate percentage safely, avoiding division by zero
    if match_stats['total_logs'] > 0:
        webid_percentage = match_stats['logs_with_webid'] / match_stats['total_logs'] * 100
    else:
        webid_percentage = 0.0
    
    status_log.append(f"- Logs with webonboarding_id: {match_stats['logs_with_webid']} " +
                     f"({webid_percentage:.1f}%)")
    status_log.append(f"- Unique webonboarding_ids: {match_stats['unique_webids']}")
    status_log.append(f"- Webid matches applied: {match_stats['webid_matches']}")
    status_log.append(f"- UID mapping fallbacks: {match_stats['uid_mapping_fallbacks']}")
    status_log.append(f"- Final canonical UIDs: {match_stats['canonical_uids']}")
    
    # Add language and onboarding statistics
    if language_stats or system_language_stats or onboarding_name_stats:
        status_log.append("\nLanguage and Onboarding Statistics:")
        if language_stats:
            status_log.append(f"- Website Languages: {dict(language_stats.most_common(10))}")
        if system_language_stats:
            status_log.append(f"- System Languages: {dict(system_language_stats.most_common(10))}")
        if onboarding_name_stats:
            status_log.append(f"- Onboarding Names: {dict(onboarding_name_stats.most_common(10))}")
    
    # If no users were found, exit early
    if not funnel_by_user:
        return [], [], ["No users found"], None
    
    # Format the logs for funnel computation
    logs_for_user = {uid: data['logs'] for uid, data in funnel_by_user.items()}
    source_performance_data = None
    if project.source_performance.enabled:
        source_performance_data = _build_source_performance(
            logs_for_user=logs_for_user,
            first_log=first_log,
            breakdown_param_key=breakdown_param_key,
            metric_events=metric_events,
            allowed_first_events=set(project.source_performance.allowed_first_events),
            revenue_events=set(project.source_performance.revenue_events),
        )
        if source_performance_data:
            status_log.append(
                f"Source performance breakdown: {source_performance_data['breakdown_param_key']}"
            )
            if source_performance_data.get("revenue_warnings"):
                warnings = source_performance_data["revenue_warnings"]
                status_log.append(
                    "Revenue parsing warnings: "
                    f"missing_params={warnings.get('missing_params', 0)}, "
                    f"missing_amount={warnings.get('missing_amount', 0)}, "
                    f"missing_currency={warnings.get('missing_currency', 0)}, "
                    f"conversion_failed={warnings.get('conversion_failed', 0)}"
                )
    
    # Compute final funnel
    if breakdown_by_event_value and breakdown_by_second_event_value:
        if progress_callback:
            progress_callback("Performing A/B test correlation analysis...")
        status_log.append(f"A/B test correlation analysis: {breakdown_by_event_value} vs {breakdown_by_second_event_value}")
        best_funnel, not_used_events, status_log_tmp = get_funnel_with_ab_correlation(
            logs_for_user, first_log, stripe_customers, breakdown_by_event_value, breakdown_by_second_event_value, app_name=app_name, onboarding_name=onboarding_name)
        _attach_total_occurrences_to_events(best_funnel, total_event_occurrences)
        _attach_total_occurrences_to_events(not_used_events, total_event_occurrences)
        status_log.extend(status_log_tmp)
        status_log.append(f"Time elapsed: {time.time() - start_time:.2f}s")
        progress_callback(f"Time elapsed: {time.time() - start_time:.2f}s")
        return best_funnel, not_used_events, status_log, source_performance_data
    elif not breakdown_by_event_value:
        try:
            best_funnel, not_used_events, status_log_tmp = get_funnel_for(
                logs_for_user, first_log, stripe_customers, app_name=app_name, onboarding_name=onboarding_name)
            _attach_total_occurrences_to_events(best_funnel, total_event_occurrences)
            _attach_total_occurrences_to_events(not_used_events, total_event_occurrences)
            status_log.extend(status_log_tmp)
        except KeyError as e:
            error_msg = f"Funnel analysis failed: {str(e)}"
            status_log.append(error_msg)
            if progress_callback:
                progress_callback(error_msg)
            # Return empty results instead of crashing
            return [], [], status_log, source_performance_data
        status_log.append(f"Time elapsed: {time.time() - start_time:.2f}s")
        progress_callback(f"Time elapsed: {time.time() - start_time:.2f}s")
        return best_funnel, not_used_events, status_log, source_performance_data
    else:
        if progress_callback:
            progress_callback("Breakdown by event value...")
        status_log.append(f"Breakdown by event value: {breakdown_by_event_value}")
        try:
            best_funnel, not_used_events, status_log_tmp = get_funnel_with_breakdown(
                logs_for_user, first_log, stripe_customers, breakdown_by_event_value, app_name=app_name, onboarding_name=onboarding_name)
            _attach_total_occurrences_to_events(best_funnel, total_event_occurrences)
            _attach_total_occurrences_to_events(not_used_events, total_event_occurrences)
            status_log.extend(status_log_tmp)
        except KeyError as e:
            error_msg = f"Funnel analysis failed: {str(e)}"
            status_log.append(error_msg)
            if progress_callback:
                progress_callback(error_msg)
            # Return empty results instead of crashing
            return [], [], status_log, source_performance_data
        status_log.append(f"Time elapsed: {time.time() - start_time:.2f}s")
        return best_funnel, not_used_events, status_log, source_performance_data


def get_funnel_with_breakdown(logs_for_user, first_log, stripe_customers, breakdown_by_event_value, app_name=None, onboarding_name=None):
    funnel_events = defaultdict(dict)
    status_log = []

    for user_id, logs in logs_for_user.items():
        first_log_found = False
        values = []
        prev_event_name = None
        for log in logs:
            if log.funnel_event_name == breakdown_by_event_value:
                first_log_found = True
                values = log.funnel_event_value
            if not first_log_found:
                continue

            # If values is None, treat it as a single None value
            if values is None:
                values = [None]

            for value in values:
                if log.funnel_event_name not in funnel_events[value]:
                    funnel_events[value][log.funnel_event_name] = FunnelEvent(log.funnel_event_name)

                funnel_events[value][log.funnel_event_name].add_event(
                    log,
                    previous_event_name=prev_event_name,
                    funnel_events=funnel_events[value],
                    override_uid=user_id
                )

                if prev_event_name:
                    funnel_events[value][prev_event_name].add_dst_event(log.funnel_event_name)
                prev_event_name = log.funnel_event_name

                if log.funnel_event_name == "subscribe_completed":
                    if "stripe_revenue" not in funnel_events[value]:
                        funnel_events[value]["stripe_revenue"] = FunnelEvent(
                            "stripe_revenue")
                    funnel_events[value]["stripe_revenue"].add_stripe_event(
                        log,
                        stripe_customers,
                        previous_event_name="subscribe_completed",
                        override_uid=user_id
                    )

                    funnel_events[value][prev_event_name].add_dst_event(
                        "stripe_revenue")
                    prev_event_name = "stripe_revenue"


    status_log.append(f"Breakdown values: {funnel_events.keys()}")

    # now, let's get correct funnel order based on all events
    best_funnel, not_used_events, status_log_tmp = get_funnel_for(
        logs_for_user,
        breakdown_by_event_value,
        stripe_customers,
        app_name=app_name,
        onboarding_name=onboarding_name,
    )
    status_log.extend(status_log_tmp)

    # final_list = defaultdict(list)
    breakdown_value_order = sorted(funnel_events.keys(), key=lambda value: sum([x.count for x in funnel_events[value].values()]), reverse=True)

    # Set up control relationships for confidence calculations
    control_breakdown_value = breakdown_value_order[0] if breakdown_value_order else None
    
    for base_event in best_funnel:
        base_event.breakdowns = {}
        control_event = None
        
        # First pass: create all breakdown events
        for breakdown_value in breakdown_value_order:
            event_list = funnel_events[breakdown_value]
            event = event_list.get(base_event.event_name, FunnelEvent(base_event.event_name))

            event.first_event_count = event_list[breakdown_by_event_value].count_unique_users
            event.next_event_count = sum([count for name, count in event.dst_sorted])
            # Set context for AB test identification
            event._app_name = app_name
            event._onboarding_name = onboarding_name
            base_event.breakdowns[breakdown_value] = event
            
            # Set the first/most common breakdown as control
            if breakdown_value == control_breakdown_value:
                control_event = event
        
        # Second pass: set control relationships for confidence calculations
        if control_event:
            for breakdown_value, event in base_event.breakdowns.items():
                if breakdown_value != control_breakdown_value:
                    event.set_control_event(control_event)
        
        base_event.breakdown_value_order = breakdown_value_order

    #         final_list[breakdown_value].append(event)
    #
    return best_funnel, not_used_events, status_log


def get_funnel_with_ab_correlation(logs_for_user, first_log, stripe_customers, 
                                 primary_test, secondary_test, app_name=None, onboarding_name=None):
    """
    Analyze correlation between two A/B tests by creating a unified breakdown showing:
    - Primary Test Variant A + Secondary Test Variant A
    - Primary Test Variant A + Secondary Test Variant B  
    - Primary Test Variant B + Secondary Test Variant A
    - Primary Test Variant B + Secondary Test Variant B
    """
    status_log = []
    correlation_events = defaultdict(dict)
    
    # First, identify users who participated in both tests
    users_with_both_tests = {}  # user_id -> {'primary': variant, 'secondary': variant}
    
    for user_id, logs in logs_for_user.items():
        user_tests = {}
        primary_test_found = False
        
        for log in logs:
            if log.funnel_event_name == primary_test:
                primary_test_found = True
            if not primary_test_found:
                continue
                
            # Check for primary test
            if log.funnel_event_name == primary_test and log.funnel_event_value:
                user_tests['primary'] = log.funnel_event_value[0] if log.funnel_event_value else 'Unknown'
                
            # Check for secondary test  
            if log.funnel_event_name == secondary_test and log.funnel_event_value:
                user_tests['secondary'] = log.funnel_event_value[0] if log.funnel_event_value else 'Unknown'
        
        # Only include users who participated in both tests
        if 'primary' in user_tests and 'secondary' in user_tests:
            users_with_both_tests[user_id] = user_tests
    
    status_log.append(f"Found {len(users_with_both_tests)} users participating in both A/B tests")
    
    # Create combination breakdown values
    combination_breakdown = {}
    for user_id, test_variants in users_with_both_tests.items():
        primary_variant = test_variants['primary']
        secondary_variant = test_variants['secondary']
        combination_key = f"{primary_variant} + {secondary_variant}"
        
        if combination_key not in combination_breakdown:
            combination_breakdown[combination_key] = []
        combination_breakdown[combination_key].append(user_id)
    
    status_log.append(f"Test combinations found: {list(combination_breakdown.keys())}")
    
    # Now process events for each combination
    for user_id, logs in logs_for_user.items():
        if user_id not in users_with_both_tests:
            continue
            
        # Get the combination for this user
        test_variants = users_with_both_tests[user_id]
        combination_key = f"{test_variants['primary']} + {test_variants['secondary']}"
        
        primary_test_found = False
        prev_event_name = None
        
        for log in logs:
            if log.funnel_event_name == primary_test:
                primary_test_found = True
            if not primary_test_found:
                continue
                
            # Create funnel event for this combination if not exists
            if log.funnel_event_name not in correlation_events[combination_key]:
                correlation_events[combination_key][log.funnel_event_name] = FunnelEvent(log.funnel_event_name)
                # Set context for AB test identification
                correlation_events[combination_key][log.funnel_event_name]._app_name = app_name
                correlation_events[combination_key][log.funnel_event_name]._onboarding_name = onboarding_name
                
            # Add the event
            correlation_events[combination_key][log.funnel_event_name].add_event(
                log,
                previous_event_name=prev_event_name,
                funnel_events=correlation_events[combination_key],
                override_uid=user_id
            )
            
            if prev_event_name:
                correlation_events[combination_key][prev_event_name].add_dst_event(log.funnel_event_name)
            prev_event_name = log.funnel_event_name
    
    # Get the base funnel order starting from the primary test
    base_funnel, not_used_events, base_status_log = get_funnel_for(
        logs_for_user, primary_test, stripe_customers, app_name=app_name, onboarding_name=onboarding_name
    )
    status_log.extend(base_status_log)
    
    # Sort combination keys for consistent display
    combination_order = sorted(combination_breakdown.keys(), 
                             key=lambda x: len(combination_breakdown[x]), reverse=True)
    
    # Set up control event relationships for confidence calculations
    control_combination = combination_order[0] if combination_order else None
    
    # Create the correlation breakdown similar to regular breakdown
    for base_event in base_funnel:
        base_event.breakdowns = {}
        base_event.correlation_matrix = {}
        
        for combination_key in combination_order:
            event_list = correlation_events[combination_key]
            event = event_list.get(base_event.event_name, FunnelEvent(base_event.event_name))
            
            # Set reference counts for conversion calculations
            if combination_key in correlation_events and primary_test in correlation_events[combination_key]:
                event.first_event_count = correlation_events[combination_key][primary_test].count_unique_users
            else:
                event.first_event_count = 0
                
            event.next_event_count = sum([count for name, count in event.dst_sorted])
            
            # Set control event for confidence calculations
            if control_combination and control_combination != combination_key:
                control_event = base_event.breakdowns.get(control_combination)
                if control_event:
                    event.set_control_event(control_event)
            
            base_event.breakdowns[combination_key] = event
            
        base_event.breakdown_value_order = combination_order
        
        # Create correlation matrix for easier access
        if len(combination_order) == 4:  # 2x2 matrix
            primary_variants = list(set(k.split(' + ')[0] for k in combination_order))
            secondary_variants = list(set(k.split(' + ')[1] for k in combination_order))
            
            if len(primary_variants) == 2 and len(secondary_variants) == 2:
                base_event.correlation_matrix = {
                    'primary_variants': primary_variants,
                    'secondary_variants': secondary_variants,
                    'combinations': {}
                }
                
                for combination_key in combination_order:
                    parts = combination_key.split(' + ')
                    if len(parts) == 2:
                        primary, secondary = parts
                        base_event.correlation_matrix['combinations'][combination_key] = {
                            'primary': primary,
                            'secondary': secondary,
                            'event': base_event.breakdowns[combination_key]
                        }
    
    return base_funnel, not_used_events, status_log


def get_funnel_for(logs_for_user, first_log, stripe_customers, app_name=None, onboarding_name=None):
    status_log = []
    funnel_events = {}

    for user_id, logs in logs_for_user.items():
        first_log_found = False
        prev_event_name = None
        prev_event_time = None
        for log in logs:
            # Skip logs with no event name
            if not hasattr(log, 'funnel_event_name') or not log.funnel_event_name:
                continue
                
            if log.funnel_event_name == first_log:
                first_log_found = True
            if not first_log_found:
                continue

            # Now we know log.funnel_event_name exists
            if log.funnel_event_name not in funnel_events:
                funnel_events[log.funnel_event_name] = FunnelEvent(log.funnel_event_name)
                # Set context for AB test date lookup
                funnel_events[log.funnel_event_name]._app_name = app_name
                funnel_events[log.funnel_event_name]._onboarding_name = onboarding_name

            funnel_events[log.funnel_event_name].add_event(
                log,
                previous_event_name=prev_event_name,
                previous_event_time=prev_event_time,
                funnel_events=funnel_events,
                override_uid=user_id
            )

            if prev_event_name:
                funnel_events[prev_event_name].add_dst_event(log.funnel_event_name)
            prev_event_name = log.funnel_event_name
            prev_event_time = log.timestamp

            if log.funnel_event_name == "subscribe_completed":
                if "stripe_revenue" not in funnel_events:
                    funnel_events["stripe_revenue"] = FunnelEvent("stripe_revenue")
                funnel_events["stripe_revenue"].add_stripe_event(
                    log,
                    stripe_customers,
                    previous_event_name="subscribe_completed",
                    override_uid=user_id
                )

                funnel_events[prev_event_name].add_dst_event("stripe_revenue")
                prev_event_name = "stripe_revenue"


    status_log.append(f"Based on {sum(x.count for x in funnel_events.values())} events in total")
    
    # Check if first_log exists in funnel_events
    if first_log not in funnel_events:
        error_msg = f"First log event '{first_log}' not found in funnel events. Available events: {list(funnel_events.keys())}"
        status_log.append(error_msg)
        raise KeyError(error_msg)
    
    best_funnel = [funnel_events[first_log]]
    funnel_events[first_log].was_used = True

    while True:
        for event in best_funnel[-1].dst_sorted:
            event_name = event[0]
            if event_name in funnel_events and not funnel_events[event_name].was_used:
                funnel_events[event_name].was_used = True
                best_funnel.append(funnel_events[event_name])
                break
        else:
            break

    not_used_events = [event for event in funnel_events.values() if not event.was_used]

    for event in not_used_events:
        if event.count * 0.5 > event.src_sorted[0][1]:
            continue
        if event.src_sorted[0][0] in [x.event_name for x in best_funnel]:
            parent_event_position = [x.event_name for x in best_funnel].index(event.src_sorted[0][0])
            best_funnel.insert(parent_event_position+1, event)
            event.was_used = True

    # Add any remaining RevenueCat events at the end of the funnel
    remaining_revenuecat = [event for event in not_used_events 
                           if event.event_name.startswith("[RevenueCat]")]
    remaining_revenuecat.sort(key=lambda x: x.count, reverse=True)
    for event in remaining_revenuecat:
        event.was_used = True
        best_funnel.append(event)

    for event in best_funnel:
        event.first_event_count = best_funnel[0].count_unique_users
        event.next_event_count = sum([count for name, count in event.dst_sorted])
    not_used_events = [event for event in funnel_events.values() if not event.was_used]

    return best_funnel, not_used_events, status_log

def _print_funnel(best_funnel, not_used_events, status_log, source_performance_data=None):
    """Plain-text dump of a funnel result (used by scripts/run_funnel.py)."""
    print("STATUS LOG")
    for line in status_log:
        print(line)
    def dump(funnel):
        if not funnel:
            print("  (no events)")
            return
        total = funnel[0].count_unique_users or 0
        print(f"  total unique users: {total}")
        for event in funnel:
            conv = (event.count_unique_users / total * 100) if total else 0
            best = event.event_values_sorted[:5]
            print(f"  {event.event_name}: {event.count_unique_users} users "
                  f"({event.count} events) | {conv:.1f}% | best: {best}")
    print("\nFUNNEL")
    dump(best_funnel)
    print("\nNOT USED EVENTS (top 10)")
    dump(not_used_events[:10] if not_used_events else [])
    if source_performance_data and source_performance_data.get("rows"):
        print("\nSOURCE PERFORMANCE")
        print(f"  breakdown param: {source_performance_data['breakdown_param_key']}")
        for row in source_performance_data["rows"][:15]:
            print(f"  {row['source']}: entries={row['entries']} revenue_eur={row['revenue']:.2f} arpu={row['arpu']:.2f}")


def analyze_event_types(app_name='lighthouse'):
    """Analyze available event types in the data for today."""
    from datetime import date, datetime
    from collections import Counter
    
    date_today = date.today()
    data_source = get_project(app_name).data_source
    
    print(f"Analyzing event types for {app_name} on {date_today}")
    
    daily_logs = data_source.get_all_logs_of_day(date_today)
    
    if not daily_logs:
        print(f"No logs found for {date_today}")
        # Try yesterday
        yesterday = date_today - timedelta(1)
        print(f"Trying {yesterday}")
        daily_logs = data_source.get_all_logs_of_day(yesterday)
        if not daily_logs:
            print(f"No logs found for {yesterday} either")
            return
    
    print(f"Found {len(daily_logs)} logs")
    
    # Count event types
    event_counts = Counter()
    webid_counts = Counter()
    
    for log in daily_logs:
        if hasattr(log, 'funnel_event_name') and log.funnel_event_name:
            event_counts[log.funnel_event_name] += 1
            
            # Check for webonboarding_id
            webid = None
            if hasattr(log, 'event_params') and log.event_params:
                webid_param = log.event_params.get('user_webonboarding_id')
                if webid_param and isinstance(webid_param, dict) and 'string_value' in webid_param:
                    webid = webid_param.get('string_value')
            
            if webid:
                webid_counts[log.funnel_event_name] += 1
    
    # Print results
    print("\nEvent types by frequency:")
    for event, count in event_counts.most_common(20):
        webid_count = webid_counts.get(event, 0)
        percentage = webid_count / count * 100 if count > 0 else 0
        print(f"  {event}: {count} occurrences, {webid_count} with webid ({percentage:.1f}%)")
    
    return event_counts.most_common(20)

def debug_event_in_logs(event_name='age_completed', app_name='lighthouse'):
    """Examine all logs with a specific event to diagnose issues."""
    from datetime import date, datetime
    
    date_today = date.today()
    data_source = get_project(app_name).data_source
    
    print(f"Diagnosing event '{event_name}' for {app_name} on {date_today}")
    
    daily_logs = data_source.get_all_logs_of_day(date_today)
    
    if not daily_logs:
        print(f"No logs found for {date_today}")
        return
    
    # Sort by timestamp
    daily_logs.sort(key=lambda log: log.timestamp if hasattr(log, 'timestamp') else 0)
    
    # Filter logs with the event
    matching_logs = []
    webid_map = {}
    
    for log in daily_logs:
        # Check for event name match
        event_name_matches = False
        if hasattr(log, 'funnel_event_name') and log.funnel_event_name == event_name:
            event_name_matches = True
        
        if not event_name_matches:
            continue
        
        # Check for webonboarding_id
        webid = None
        if hasattr(log, 'event_params') and log.event_params:
            webid_param = log.event_params.get('user_webonboarding_id')
            if webid_param and isinstance(webid_param, dict) and 'string_value' in webid_param:
                webid = webid_param.get('string_value')
        
        matching_logs.append({
            'uid': log.uid if hasattr(log, 'uid') else 'None',
            'timestamp': log.timestamp if hasattr(log, 'timestamp') else 'Unknown',
            'platform': log.platform if hasattr(log, 'platform') else 'Unknown',
            'webid': webid,
        })
        
        if webid:
            webid_map[webid] = webid_map.get(webid, 0) + 1
    
    print(f"Found {len(matching_logs)} logs with event '{event_name}'")
    print(f"Unique webids: {len(webid_map)}")
    
    # Print sample logs
    print("\nSample logs (first 10):")
    for i, log in enumerate(matching_logs[:10]):
        print(f"{i+1}. UID: {log['uid']}, Timestamp: {log['timestamp']}, Platform: {log['platform']}, WebID: {log['webid']}")
    
    return matching_logs
