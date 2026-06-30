#!/usr/bin/env python3
"""
Quick text-mode funnel runner.

Connects to the project's Firestore (using config/projects.yaml + the service
account), runs get_funnel(), and prints the funnel as plain text. Use it to
verify the engine end-to-end without the web server.

Examples:
    python scripts/run_funnel.py myapp
    python scripts/run_funnel.py myapp --days 7
    python scripts/run_funnel.py myapp --from 2025-05-01 --to 2025-05-07 \
        --first-event first_visit
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

# Allow running as `python scripts/run_funnel.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.projects import get_project          # noqa: E402
from app.funnel.core import get_funnel, _print_funnel  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Run a funnel and print it as text.")
    ap.add_argument("project", help="Project name from config/projects.yaml (e.g. myapp)")
    ap.add_argument("--from", dest="date_from", help="YYYY-MM-DD (default: --days ago)")
    ap.add_argument("--to", dest="date_to", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--days", type=int, default=4, help="Look-back window when --from is omitted")
    ap.add_argument("--first-event", dest="first_event", help="Override the funnel's first event")
    ap.add_argument("--platform", default="",
                    help="Platform filter (default: empty = all; the engine's own "
                         "default is 'iOS', which would exclude web projects like myapp)")
    ap.add_argument("--breakdown-param", dest="breakdown_param_key",
                    help="UTM param for source-performance breakdown")
    ap.add_argument("--metric-events", dest="metric_events",
                    help="Comma separated metric events for source performance")
    ap.add_argument("--no-cache", action="store_true", help="Bypass the cache")
    ap.add_argument("--verbose", action="store_true", help="Print scan progress")
    args = ap.parse_args()

    project = get_project(args.project)
    defaults = project.defaults or {}

    date_to = date.fromisoformat(args.date_to) if args.date_to else date.today()
    date_from = (date.fromisoformat(args.date_from) if args.date_from
                 else date_to - timedelta(args.days))
    first_event = args.first_event or defaults.get("first_event")
    breakdown_param_key = args.breakdown_param_key or defaults.get("breakdown_param_key")
    metric_events = args.metric_events or ",".join(defaults.get("metric_events", []))

    print(f"Project:      {project.name} ({project.label})")
    print(f"Firestore:    {project.firestore_project_id} / {project.collection_name}")
    print(f"Date range:   {date_from} .. {date_to}")
    print(f"First event:  {first_event}")
    print(f"Breakdown:    {breakdown_param_key}")
    print("-" * 60)

    progress = (lambda *a: print("…", *a)) if args.verbose else (lambda *a: None)

    best_funnel, not_used_events, status_log, source = get_funnel(
        project.name,
        (date_from, date_to),
        first_event,
        platform=args.platform,
        breakdown_param_key=breakdown_param_key,
        metric_events=metric_events,
        progress_callback=progress,
        use_cache=not args.no_cache,
    )

    _print_funnel(best_funnel, not_used_events, status_log, source)

    print("-" * 60)
    print(f"OK: {len(best_funnel)} funnel steps, "
          f"{best_funnel[0].count_unique_users if best_funnel else 0} users in first step.")


if __name__ == "__main__":
    main()
