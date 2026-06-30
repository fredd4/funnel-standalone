"""
FastAPI app serving the funnel.

Design choice: SYNCHRONOUS computation + strong caching. Cloud Run allows long
request timeouts, and get_funnel already caches its day-log fetches, so the first
request for a date range is slow and subsequent ones are fast.

Endpoints:
  GET  /                                  -> health check
  GET  /{project}/funnel                  -> full HTML page (filter form + result)
  GET  /{project}/funnel/fragment         -> just the result table fragment (JSON-less HTML)
  GET  /{project}/funnel.json             -> machine-readable funnel data
"""
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import settings
from app.projects import get_project, PROJECTS
from app.funnel.core import get_funnel, filter_funnel_events

app = FastAPI(title="Funnel (standalone)")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Cookie the browser stores the token in (set via the /login form). Lets a human
# use the app from a browser without manually attaching an Authorization header.
# MUST be named "__session": Firebase Hosting strips every cookie except this one
# before proxying to Cloud Run, so any other name never reaches the app.
COOKIE_NAME = "__session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


# --- tiny auth ---------------------------------------------------------------
def _token_from_request(request: Request) -> str:
    """Token from the Authorization header (API clients) or the cookie (browser)."""
    header = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    return header or (request.cookies.get(COOKIE_NAME) or "").strip()


def _is_authed(request: Request) -> bool:
    if not settings.API_TOKENS:
        return True  # open (only behind Hosting/IAP or local dev)
    return _token_from_request(request) in settings.API_TOKENS


def require_auth(request: Request):
    """For API/AJAX endpoints: hard 401 when the token is missing or wrong."""
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _safe_next(next_url: str | None) -> str:
    """Only allow same-site relative redirects to avoid open-redirect abuse."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def _parse_filters(request: Request, project):
    q = request.query_params
    today = date.today()
    defaults = project.defaults or {}

    def d(name, fallback):
        v = q.get(name)
        return date.fromisoformat(v) if v else fallback

    metric_events = q.get("metric_events") or ",".join(defaults.get("metric_events", []))
    return {
        "date_from": d("date_from", today - timedelta(4)),
        "date_to": d("date_to", today),
        "first_event": q.get("first_event") or defaults.get("first_event"),
        "country_codes": (q.get("country_code").upper().split(",")
                          if q.get("country_code") else None),
        "platform": q.get("platform") or None,
        "breakdown_by_event_value": q.get("breakdown_by_event_value") or None,
        "event_regex": q.get("event_regex") or None,
        "app_version": q.get("app_version") or None,
        "additional_days": int(q.get("additional_days") or 0),
        "language": q.get("language") or None,
        "system_language": q.get("system_language") or None,
        "onboarding_name": q.get("onboarding_name") or None,
        "breakdown_by_second_event_value": q.get("breakdown_by_second_event_value") or None,
        "breakdown_param_key": q.get("breakdown_param_key") or defaults.get("breakdown_param_key"),
        "metric_events": metric_events,
        "simplify_result": q.get("simplify_result") in ("1", "true", "on"),
    }


def _run_funnel(project, f):
    best_funnel, not_used_events, status_log, source_performance_data = get_funnel(
        project.name,
        (f["date_from"], f["date_to"]),
        f["first_event"],
        country_codes=f["country_codes"],
        platform=f["platform"],
        breakdown_by_event_value=f["breakdown_by_event_value"],
        event_regex=f["event_regex"],
        progress_callback=lambda *_: None,
        app_version=f["app_version"],
        additional_days=f["additional_days"],
        language=f["language"],
        system_language=f["system_language"],
        onboarding_name=f["onboarding_name"],
        breakdown_by_second_event_value=f["breakdown_by_second_event_value"],
        breakdown_param_key=f["breakdown_param_key"],
        metric_events=f["metric_events"],
    )
    if f["simplify_result"]:
        best_funnel = filter_funnel_events(best_funnel, True)
        not_used_events = filter_funnel_events(not_used_events, True)
    return best_funnel, not_used_events, status_log, source_performance_data


@app.get("/")
def health():
    return {"status": "ok", "projects": list(PROJECTS)}


# --- browser login: paste the token once, it's stored in a cookie ------------
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    next_url = _safe_next(request.query_params.get("next"))
    if _is_authed(request):
        return RedirectResponse(next_url, status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"next": next_url, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, token: str = Form(""), next: str = Form("/")):
    next_url = _safe_next(next)
    if settings.API_TOKENS and token.strip() not in settings.API_TOKENS:
        return templates.TemplateResponse(
            request, "login.html",
            {"next": next_url, "error": "Invalid token."}, status_code=401)
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(COOKIE_NAME, token.strip(), max_age=COOKIE_MAX_AGE,
                    httponly=True, secure=True, samesite="lax")
    return resp


@app.get("/logout")
def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/{project_name}/funnel", response_class=HTMLResponse)
def funnel_page(project_name: str, request: Request):
    if not _is_authed(request):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    project = get_project(project_name)
    f = _parse_filters(request, project)
    best, not_used, status_log, source = _run_funnel(project, f)
    return templates.TemplateResponse(request, "funnel.html", {
        "app_name": project_name, "project": project, "filters": f,
        "best_funnel": best, "not_used_events": not_used, "status_log": status_log,
        "source_performance_data": source,
        "breakdown_by_event_value": f["breakdown_by_event_value"],
        "date_from": f["date_from"], "date_to": f["date_to"],
    })


@app.get("/{project_name}/funnel/fragment", response_class=HTMLResponse)
def funnel_fragment(project_name: str, request: Request, _=Depends(require_auth)):
    project = get_project(project_name)
    f = _parse_filters(request, project)
    best, not_used, status_log, source = _run_funnel(project, f)
    return templates.TemplateResponse(request, "funnel_part.html", {
        "app_name": project_name, "best_funnel": best,
        "not_used_events": not_used, "status_log": status_log,
        "source_performance_data": source,
        "breakdown_by_event_value": f["breakdown_by_event_value"],
        "date_from": f["date_from"], "date_to": f["date_to"],
    })


@app.get("/{project_name}/funnel.json")
def funnel_json(project_name: str, request: Request, _=Depends(require_auth)):
    project = get_project(project_name)
    f = _parse_filters(request, project)
    best, not_used, status_log, source = _run_funnel(project, f)

    def serialize(event):
        return {
            "event_name": event.event_name,
            "count": event.count,
            "count_unique_users": event.count_unique_users,
            "conversion_till_event": event.conversion_till_event,
            "dropoff": event.dropoff,
            "total_occurrences": event.total_occurrences,
            "median_time_to_next_step": event.median_time_to_next_step,
            "next": event.dst_sorted[0][0] if event.dst_sorted else None,
        }

    return JSONResponse({
        "best_funnel": [serialize(e) for e in best],
        "not_used_events": [serialize(e) for e in not_used],
        "source_performance": source,
        "status_log": status_log,
    })
