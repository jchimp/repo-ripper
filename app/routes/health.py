"""Liveness check for Docker and monitors. Public, and reveals nothing."""
import sqlite3

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import db

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        return JSONResponse({"status": "error", "check": "database"}, status_code=503)
    return JSONResponse({"status": "ok"})
