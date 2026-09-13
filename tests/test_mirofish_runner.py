"""Uji mirofish_runner: kontrak verdict, job store, dan eksekusi CLI.

Runner-nya sendiri tidak bisa memanggil MiroFish sungguhan di CI (butuh kredensial
LLM dan dependensi OASIS yang berat), jadi eksekusi diuji lewat MIROFISH_FAKE yang
menghasilkan artefak dengan bentuk yang benar. Yang diuji di sini adalah LOGIKA
lapisannya: apa yang terjadi bila verdict rusak, hilang, atau job belum selesai.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mirofish_runner.runner import (  # noqa: E402
    ADAPTER_VERSION,
    VERDICT_KEYS,
    ContractError,
    JobStore,
    Runner,
    build_command,
    locate_artifacts,
    normalize_verdict,
    parse_cli_json,
    read_verdict_file,
)

FIXTURE = json.loads((ROOT / "tests/verdict_cases.json").read_text(encoding="utf-8"))


# ------------------------------------------------------- paritas dengan adapter JS


@pytest.mark.parametrize(
    "case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]]
)
def test_amplop_sesuai_fixture_bersama(case):
    """Fixture yang sama dijalankan oleh tests/test_mirofish_verdict_parity.js."""
    env = normalize_verdict(case["verdict"], run_id="parity-run",
                            now="2026-09-13T08:00:00Z")
    for key, expected in case["expect"].items():
        assert env[key] == expected, f"{key}: {env[key]!r} != {expected!r}"


def test_amplop_selalu_memiliki_semua_kunci_kontrak():
    for verdict in (None, {}, {"prediction": "x"}, FIXTURE["cases"][0]["verdict"]):
        env = normalize_verdict(verdict, run_id="r")
        assert set(VERDICT_KEYS) <= set(env), set(VERDICT_KEYS) - set(env)


def test_gagal_apa_pun_berarti_veto():
    """Tidak boleh ada jalan dari data rusak menuju event_risk != HIGH."""
    rusak = [None, {}, [], "bukan dict", {"prediction": "pendek"},
             {"prediction": "cukup panjang untuk lolos pemeriksaan minimal",
              "confidence": "kosong"},
             {"prediction": "cukup panjang untuk lolos pemeriksaan minimal",
              "confidence": 2},
             {"prediction": "cukup panjang untuk lolos pemeriksaan minimal",
              "confidence": True},
             {"prediction": "cukup panjang untuk lolos pemeriksaan minimal",
              "confidence": 0.5, "key_dynamics": "bukan list"}]
    for v in rusak:
        env = normalize_verdict(v, run_id="r")
        assert env["schema_ok"] is False, v
        assert env["event_risk"] == "HIGH", v


# --------------------------------------------------------------- confidence ketat


def test_confidence_tidak_pernah_jadi_nol_diam_diam():
    """Bug yang dulu ada di JS: Number(undefined) === 0 lolos sebagai confidence 0."""
    with pytest.raises(ContractError, match="CONFIDENCE_MISSING"):
        from mirofish_runner.runner import _coerce_confidence
        _coerce_confidence(None)


def test_confidence_string_kosong_ditolak():
    from mirofish_runner.runner import _coerce_confidence
    with pytest.raises(ContractError):
        _coerce_confidence("   ")
    assert _coerce_confidence("0.7") == 0.7


def test_confidence_boolean_ditolak():
    """True == 1 di Python; tanpa pemeriksaan ini confidence jadi 1.0."""
    from mirofish_runner.runner import _coerce_confidence
    with pytest.raises(ContractError):
        _coerce_confidence(True)


# ---------------------------------------------------------------- bias & risiko


def test_bias_ambigu_tidak_pernah_menebak():
    for text in ("bullish tapi juga bearish", "rally dan selloff bersamaan",
                 "tidak ada arah jelas"):
        env = normalize_verdict(
            {"prediction": text + " " * 20, "confidence": 0.9,
             "key_dynamics": [], "signals": []},
            run_id="r",
        )
        assert env["bias"] == "NEUTRAL", text


def test_kata_pendek_tidak_cocok_di_tengah_kata():
    """'ban' tidak boleh cocok di dalam 'besar' atau 'banjir'."""
    from mirofish_runner.runner import _hits
    assert _hits("tidak ada pemicu besar hari ini", "ban") == 0
    assert _hits("ada ban baru", "ban") == 1


def test_teks_tenang_mendapat_skor_nol_bukan_setengah():
    """(x+1)/2 dulu membuat verdict paling tenang jatuh di pita MEDIUM."""
    from mirofish_runner.runner import _risk_score, _risk_level
    assert _risk_score("Perdagangan berjalan normal dan tenang.") == 0.0
    assert _risk_level(0.0) == "LOW"


# ------------------------------------------------------------------- parse CLI


def test_parse_cli_json_ambil_objek_dengan_run_id():
    stdout = '2026-09-13 08:00 INFO mulai\n{"progress": 1}\n{"run_id": "abc123"}\n'
    assert parse_cli_json(stdout)["run_id"] == "abc123"


def test_parse_cli_json_tanpa_run_id_adalah_kegagalan():
    with pytest.raises(ContractError, match="CLI_NO_RUN_ID"):
        parse_cli_json('{"progress": 1}\n{"status": "ok"}')
    with pytest.raises(ContractError, match="CLI_NO_OUTPUT"):
        parse_cli_json("   ")


# ------------------------------------------------------------------- job store


def test_job_store_buat_ambil_ulang(tmp_path):
    store = JobStore(tmp_path)
    job = store.create("job-1", "BTC_USDT_PERP")
    assert job.status == "PENDING"
    fetched = store.get("job-1")
    assert fetched is not None and fetched.symbol == "BTC_USDT_PERP"
    assert store.get("tidak-ada") is None


def test_job_store_menolak_id_berbahaya(tmp_path):
    store = JobStore(tmp_path)
    for bad in ("../etc", "a/b", "a b", "x" * 65, ""):
        with pytest.raises(ContractError, match="INVALID_JOB_ID"):
            store.create(bad, "BTC_USDT_PERP")


def test_job_store_menolak_duplikat(tmp_path):
    store = JobStore(tmp_path)
    store.create("job-1", "BTC_USDT_PERP")
    with pytest.raises(ContractError, match="JOB_ALREADY_EXISTS"):
        store.create("job-1", "BTC_USDT_PERP")


# ------------------------------------------------------------------ runner palsu


def _wait_job(runner: Runner, job_id: str, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = runner.store.get(job_id)
        if job and job.status in ("SUCCEEDED", "FAILED"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} tidak selesai dalam {timeout}s")


def test_runner_palsu_menghasilkan_verdict_sehat(tmp_path):
    store = JobStore(tmp_path / "jobs")
    runner = Runner(store=store, artifacts_root=tmp_path / "runs", fake=True)
    runner.submit("job-ok", "BTC_USDT_PERP")
    job = _wait_job(runner, "job-ok")

    assert job.status == "SUCCEEDED", job.error
    assert job.run_id and job.run_id.startswith("fake-")

    resp = runner.verdict_for(job)
    # Bentuk respons ini yang dibaca n8n/code/mirofish_adapter.js.
    assert set(resp) >= {"run_id", "job_id", "job_status", "verdict", "manifest",
                         "envelope", "contract_version", "notes", "created_at",
                         "summary", "adapter_version"}
    assert isinstance(resp["verdict"], dict)
    assert resp["manifest"]["run_id"] == job.run_id
    assert resp["envelope"]["adapter_version"] == ADAPTER_VERSION

    artifacts = locate_artifacts(job.run_id, runner.artifacts_root) or (
        store._dir(job.job_id) / "artifacts"
    )
    assert read_verdict_file(artifacts) is not None


def test_job_belum_selesai_mengembalikan_veto_bukan_error(tmp_path):
    store = JobStore(tmp_path / "jobs")
    runner = Runner(store=store, artifacts_root=tmp_path / "runs", fake=True)
    job = store.create("job-pending", "BTC_USDT_PERP")   # tidak pernah dijalankan

    resp = runner.verdict_for(job)
    assert resp["verdict"] is None
    assert resp["envelope"]["schema_ok"] is False
    assert resp["envelope"]["event_risk"] == "HIGH"
    assert "JOB_PENDING" in resp["envelope"]["notes"]


def test_artefak_hilang_tetap_veto(tmp_path):
    store = JobStore(tmp_path / "jobs")
    runner = Runner(store=store, artifacts_root=tmp_path / "runs", fake=True)
    job = store.create("job-rusak", "BTC_USDT_PERP")
    job.status = "SUCCEEDED"
    job.run_id = "run-yang-tidak-ada"
    store.save(job)

    resp = runner.verdict_for(job)
    assert resp["verdict"] is None
    assert resp["envelope"]["event_risk"] == "HIGH"
    assert "ARTIFACT_VERDICT_UNREADABLE" in resp["notes"]


def test_cli_gagal_menandai_job_failed(tmp_path):
    store = JobStore(tmp_path / "jobs")
    runner = Runner(store=store, artifacts_root=tmp_path / "runs",
                    mirofish_bin="/bin/false", fake=False)
    runner.submit("job-gagal", "BTC_USDT_PERP")
    job = _wait_job(runner, "job-gagal")
    assert job.status == "FAILED"
    assert job.error and job.error.startswith("CLI_EXIT_")


# ------------------------------------------------------------------ perintah CLI


def test_perintah_cli_tanpa_shell_dan_dengan_batas_putaran():
    cmd = build_command("BTC_USDT_PERP", requirement=None, max_rounds=7,
                        platform="parallel", mirofish_bin="mirofish")
    assert cmd[0] == "mirofish" and cmd[1] == "run"
    assert "--max-rounds" in cmd and cmd[cmd.index("--max-rounds") + 1] == "7"
    assert "--json" in cmd
    assert not any(";" in c or "&&" in c for c in cmd)


def_runner_keys = None
