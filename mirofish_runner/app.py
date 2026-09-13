"""Lapisan HTTP mirofish_runner.

Sengaja tipis: semua logika ada di runner.py supaya bisa diuji tanpa server.
Dijalankan:  uvicorn mirofish_runner.app:app --host 0.0.0.0 --port 8100

`create_app()` ada agar test bisa membangun aplikasi dengan direktori sementara dan
token uji sendiri, tanpa menyentuh keadaan global modul.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .runner import (
    CONTRACT_VERSION,
    ContractError,
    JobStore,
    Runner,
    env_config,
    normalize_verdict,
    utcnow_iso,
)


class JobRequest(BaseModel):
    job_id: str | None = Field(default=None, max_length=64)
    symbol: str = Field(min_length=3, max_length=32)
    max_rounds: int | None = Field(default=None, ge=1, le=50)
    platform: str | None = Field(default=None, max_length=32)


def create_app(cfg: dict[str, Any] | None = None) -> FastAPI:
    cfg = cfg or env_config()
    store = JobStore(cfg["jobs_root"])
    runner = Runner(store=store, **{k: v for k, v in cfg.items()
                                    if k not in ("jobs_root", "token")})

    app = FastAPI(title="mirofish_runner", version=CONTRACT_VERSION)
    app.state.store = store
    app.state.runner = runner

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        """/health terbuka (dipakai Docker healthcheck); sisanya wajib Bearer token.

        Pembandingnya hmac.compare_digest supaya waktu respons tidak membocorkan
        berapa banyak karakter token yang sudah benar.
        """
        if request.url.path == "/health":
            return await call_next(request)
        expected = cfg.get("token", "")
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
            "fake_mode": bool(cfg.get("fake")),
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
            # 409 hanya untuk "sudah ada". Kesalahan bentuk masukan (mis. job_id yang
            # mengandung ../) harus 422: keduanya penyebabnya beda dan penanganannya
            # pun beda -- yang satu boleh di-retry setelah job lama selesai, yang lain
            # tidak akan pernah berhasil berapa kali pun diulang.
            if str(exc) == "JOB_ALREADY_EXISTS":
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise
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
        """Verdict dari job terakhir, atau veto bila belum ada job sama sekali."""
        job = _latest_job(store)
        if job is None:
            return normalize_verdict(None, run_id="none")
        return runner.verdict_for(job)

    @app.exception_handler(ContractError)
    async def _contract_error(_: Request, exc: ContractError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=422)

    return app


def _latest_job(store: JobStore):
    if not store.root.is_dir():
        return None
    for entry in sorted(store.root.iterdir(), reverse=True):
        job = store.get(entry.name)
        if job:
            return job
    return None


app = create_app()
