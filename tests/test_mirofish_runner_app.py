"""Uji lapisan HTTP mirofish_runner (FastAPI) secara nyata lewat TestClient.

Bagian ini tidak bisa diuji lewat runner.py saja: autentikasi, kode status, dan bentuk
respons HTTP adalah kontrak yang dipakai workflow n8n 06. Kalau /verdict mengembalikan
404 alih-alih amplop veto, n8n akan berhenti di node HTTP dan verdict tidak pernah
sampai ke gerbang.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

fastapi = pytest.importorskip(
    "fastapi", reason="fastapi belum terpasang: pip install -r mirofish_runner/requirements.txt"
)
from fastapi.testclient import TestClient  # noqa: E402

from mirofish_runner.app import create_app  # noqa: E402

TOKEN = "token-uji-panjang-dan-acak"


def make_client(tmp_path: Path, token: str = TOKEN) -> TestClient:
    app = create_app({
        "jobs_root": tmp_path / "jobs",
        "artifacts_root": tmp_path / "runs",
        "mirofish_bin": "mirofish",
        "timeout_sec": 60,
        "max_rounds": 10,
        "platform": "parallel",
        "fake": True,
        "cwd": None,
        "token": token,
    })
    return TestClient(app)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ autentikasi


def test_health_terbuka_tanpa_token(tmp_path):
    """Docker healthcheck tidak mengirim token; endpoint ini harus tetap bisa dibaca."""
    client = make_client(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["fake_mode"] is True
    assert body["contract_version"]


def test_tanpa_token_terkonfigurasi_semua_ditolak(tmp_path):
    """Lebih baik menolak semua daripada berjalan tanpa autentikasi."""
    client = make_client(tmp_path, token="")
    assert client.get("/jobs/apa-saja").status_code == 503
    assert client.post("/jobs", json={"symbol": "BTC_USDT_PERP"}).status_code == 503
    assert client.get("/health").status_code == 200     # health tetap terbuka


@pytest.mark.parametrize("header", [
    {},
    {"Authorization": "Bearer salah"},
    {"Authorization": "Bearer "},
    {"Authorization": "token-uji-panjang-dan-acak"},      # tanpa skema Bearer
    {"X-Runner-Token": "token-uji-panjang-dan-acak"},     # header lama tidak dipakai
])
def test_token_salah_ditolak(tmp_path, header):
    client = make_client(tmp_path)
    assert client.get("/jobs/x", headers=header).status_code == 401


def test_job_id_berbahaya_ditolak(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/jobs", json={"job_id": "../../etc", "symbol": "BTC_USDT_PERP"},
                    headers=auth())
    assert r.status_code == 422, r.text
    assert "INVALID_JOB_ID" in r.text


def test_simbol_wajib_diisi(tmp_path):
    client = make_client(tmp_path)
    assert client.post("/jobs", json={"symbol": "BT"}, headers=auth()).status_code == 422


# ------------------------------------------------------------------ alur job


def test_alur_submit_status_verdict(tmp_path):
    client = make_client(tmp_path)

    r = client.post("/jobs", json={"job_id": "job-http-1", "symbol": "BTC_USDT_PERP"},
                    headers=auth())
    assert r.status_code == 202, r.text
    assert r.json()["job_id"] == "job-http-1"
    assert r.json()["status"] == "PENDING"

    deadline = time.time() + 15
    status = None
    while time.time() < deadline:
        status = client.get("/jobs/job-http-1", headers=auth())
        assert status.status_code == 200
        if status.json()["status"] in ("SUCCEEDED", "FAILED"):
            break
        time.sleep(0.05)
    assert status.json()["status"] == "SUCCEEDED", status.json()

    v = client.get("/jobs/job-http-1/verdict", headers=auth())
    assert v.status_code == 200
    body = v.json()
    # Kunci-kunci ini yang dibaca n8n/code/mirofish_adapter.js.
    for key in ("run_id", "job_id", "job_status", "verdict", "summary", "manifest",
                "envelope", "adapter_version", "notes", "contract_version", "created_at"):
        assert key in body, f"kunci {key} hilang dari respons verdict"
    assert isinstance(body["verdict"], dict)
    assert body["manifest"]["run_id"] == body["run_id"]
    assert body["envelope"]["schema_ok"] is True


def test_verdict_job_belum_selesai_adalah_veto_dengan_200(tmp_path):
    """n8n memanggil /verdict tanpa tahu job sudah selesai atau belum.

    Dijawab 200 + veto, bukan 404/500: kalau node HTTP n8n menerima 4xx, eksekusi
    berhenti di situ dan verdict tidak pernah sampai ke gerbang risiko.
    """
    client = make_client(tmp_path)
    app = client.app
    app.state.store.create("job-menggantung", "BTC_USDT_PERP")   # tidak pernah dijalankan

    r = client.get("/jobs/job-menggantung/verdict", headers=auth())
    assert r.status_code == 200, r.text
    env = r.json()["envelope"]
    assert env["schema_ok"] is False
    assert env["event_risk"] == "HIGH"
    assert "JOB_PENDING" in env["notes"]
    assert r.json()["verdict"] is None


def test_job_tidak_ada_memberi_404(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/jobs/tidak-ada", headers=auth()).status_code == 404
    assert client.get("/jobs/tidak-ada/verdict", headers=auth()).status_code == 404


def test_job_id_duplikat_ditolak(tmp_path):
    client = make_client(tmp_path)
    assert client.post("/jobs", json={"job_id": "dup", "symbol": "BTC_USDT_PERP"},
                       headers=auth()).status_code == 202
    r = client.post("/jobs", json={"job_id": "dup", "symbol": "BTC_USDT_PERP"},
                    headers=auth())
    assert r.status_code == 409, r.text
    assert "JOB_ALREADY_EXISTS" in r.text


def test_verdict_terbaru_tanpa_job_adalah_veto(tmp_path):
    """/verdict tidak boleh 500 saat belum ada job: n8n memanggilnya tanpa syarat."""
    client = make_client(tmp_path)
    r = client.get("/verdict", headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["schema_ok"] is False
    assert r.json()["event_risk"] == "HIGH"


def test_job_id_otomatis_bila_tidak_dikirim(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/jobs", json={"symbol": "ETH_USDT_PERP"}, headers=auth())
    assert r.status_code == 202
    assert r.json()["job_id"].startswith("job-")
