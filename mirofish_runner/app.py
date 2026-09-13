"""Lapisan HTTP mirofish_runner.

Sengaja tipis: semua logika ada di runner.py supaya bisa diuji tanpa server.
Dijalankan:  uvicorn mirofish_runner.app:app --host 0.0.0.0 --port 8100
"""

from __future__ import annotations

import hmac
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .runner import (
    CONTRACT_VERSION,
    ContractError,
    JobStore,
    Runner,
    env_config,
    utcnow_iso,
)

_cfg = env_config()
store = JobStore(_cfg["jobs_root"])
runner = Runner(store=store, **{k: v for k, v in _cfg.items()
                                if k not in ("jobs_root", "token")})

app = FastAPI(title="mirofish_runner", version=CONTRACT_VERSION)


class JobRequest(BaseModel):
    job_id: str | None = Field(default=None, max_length=64)
    symbol: str = Field(min_length=3, max_length=32)
    max_rounds: int | None = Field(default=None, ge=1, le=50)
    platform: str | None = Field(default=None, max_length=32)


@app.middleware("http")
async def _auth(request: Request, call_next):
    """/health terbuka (dipakai Docker healthcheck); sisanya wajib Bearer token.

    Pembandingnya hmac.compare_digest supaya panjang respons tidak membocorkan
    berapa banyak karakter token yang sudah benar.
    """
    if request.url.path == "/health":
        return await call_next(request)
    expected = _cfg.get("token", "")
    if not expected:
        return JSONResponse({"error": "RUNNER_TOKEN_NOT_SET"}, status_code=503)
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, expected):
        return JSONResponse({"error": "UNAUTHORIZED"}, status_code=401)
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "contract_version": CONTRACT_VERSION,
        "fake_mode": bool(_cfg.get("fake")),
        "ts": utcnow_iso(),
    }


@app.post("/jobs", status_code=202)
def submit_job(body: JobRequest) -> dict[str, Any]:
    job_id = body.job_id or f"job-{uuid.uuid4().hex[:16]}"
    if body.max_rounds:
        runner.max_rounds = body.max_rounds
    if body.platform:
        runner.platform = body.platform
    try:
        job = runner.submit(job_id, body.symbol)
    except ContractError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.to_dict()


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="JOB_NOT_FOUND")
    return job.to_dict()


@app.get("/jobs/{job_id}/verdict")
def get_verdict(job_id: str) -> dict[str, Any]:
    """Selalu 200 dengan amplop yang valid — termasuk saat job gagal.

    Konsekuensinya disengaja: n8n tidak perlu menangani 404/500 sebagai kasus
    khusus. Job yang belum selesai atau gagal menghasilkan `schema_ok: false` dan
    `event_risk: "HIGH"`, yang di sisi n8n dibaca sebagai veto.
    """
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="JOB_NOT_FOUND")
    return runner.verdict_for(job)


@app.get("/verdict")
def latest_verdict() -> dict[str, Any]:
    """Verdict dari job RUNNING terakhir, atau veto kalau tidak ada.

    Dipakai n8n sebagai jalur pendek: satu panggilan, selalu dapat jawaban yang
    aman untuk dipakai.
    """
    job = store.latest_running() or _latest_job()
    if job is None:
        from .runner import normalize_verdict
        return normalize_verdict(None, run_id="none")
    return runner.verdict_for(job)


def _latest_job():
    entries = sorted(store.root.iterdir(), reverse=True) if store.root.is_dir() else []
    for entry in entries:
        job = store.get(entry.name)
        if job:
            return job
    return None


@app.exception_handler(ContractError)
async def _contract_error(_: Request, exc: ContractError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=422)
