"""repo-ripper — FastAPI application entrypoint."""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, auth, db, scheduler
from .config import get_settings
from .metadata import assets_job as _assets_job  # noqa: F401  (registers the job)
from .metadata import job as _metadata_job  # noqa: F401  (registers the metadata job)
from .routes import (
    auth_routes,
    browse,
    dashboard,
    folders,
    health,
    repos,
    settings,
    setup,
    watch,
)
from .routes import metadata as metadata_routes
from .web import BASE_DIR, csrf_protect

log = logging.getLogger("repo-ripper")


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = get_settings().insecure_defaults()
    if problems:
        for p in problems:
            log.error("Refusing to start: %s. Edit .env (see .env.example).", p)
        raise RuntimeError("Insecure configuration: " + "; ".join(problems))
    db.init_db()
    db.mark_interrupted_jobs()  # before scheduler.start(): no job runs yet
    auth.init_auth()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(
    title="repo-ripper",
    version=__version__,
    lifespan=lifespan,
    dependencies=[Depends(csrf_protect)],
    docs_url=None, redoc_url=None, openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_settings().secret_key,
    max_age=get_settings().session_max_age,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(auth.NotAuthenticated)
async def _unauth(request: Request, exc: auth.NotAuthenticated):
    if request.headers.get("HX-Request"):
        resp = Response(status_code=401)
        resp.headers["HX-Redirect"] = "/login"
        return resp
    return RedirectResponse("/login", status_code=303)


# Public routers.
app.include_router(health.router)
app.include_router(auth_routes.router)

# Private routers: add new feature routers to this tuple.
for _module in (dashboard, repos, settings, setup, folders, watch, metadata_routes, browse):
    app.include_router(_module.router, dependencies=[Depends(auth.require_user)])
