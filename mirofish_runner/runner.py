"""mirofish_runner — pembungkus HTTP tipis di atas CLI MiroFish.

Kenapa perlu lapisan ini?
    Workflow n8n tidak boleh memanggil `mirofish run` secara langsung: n8n tidak punya
    tempat yang wajar untuk menjalankan proses panjang (simulasi multi-agen butuh
    menit sampai puluhan menit), dan keluaran CLI itu teks + berkas artefak, bukan
    kontrak API yang stabil. Lapisan ini memberi tiga hal:

      1. ASINKRON  — POST /jobs langsung balas, n8n polling tiap 2 menit.
      2. KONTRAK   — /jobs/{id}/verdict mengembalikan JSON dengan bentuk pasti; kalau
         bentuknya berubah, adapter di sisi n8n menolak (fail-closed), bukan menebak.
      3. ISOLASI   — kredensial LLM dan artefak run tidak pernah menyentuh n8n.

Yang TIDAK dilakukan lapisan ini: mengubah angka, mengambil keputusan, atau
memperbaiki verdict yang rusak. Ia hanya memindahkan dan melabeli.

Modul ini murni (tanpa dependensi web) supaya bisa diuji tanpa FastAPI; lapisan
HTTP-nya ada di app.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ADAPTER_VERSION = "1.0.0"
CONTRACT_VERSION = "1"

# Bentuk respons yang DIJAMIN lapisan ini. Diverifikasi silang oleh
# tools/validate_mirofish_contract.py terhadap n8n/code/mirofish_adapter.js dan
# kolom tabel mirofish_verdict di db/schema.sql.
VERDICT_KEYS = (
    "run_id",
    "verdict_ts",
    "schema_ok",
    "bias",
    "confidence",
    "event_risk",
    "horizon_hours",
    "evidence",
    "adapter_version",
    "notes",
    "risk_score",
)

# Daftar kata ini HARUS menghasilkan klasifikasi yang sama dengan
# n8n/code/mirofish_adapter.js pada teks yang sama. tests/verdict_cases.json adalah
# fixture bersama; tests/test_mirofish_runner.py dan
# tests/test_mirofish_verdict_parity.js menjalankannya di kedua bahasa.
BIAS_KEYWORDS_LONG = ("bullish", "rally", "uptrend", "breakout", "surge",
                      "buying pressure")
BIAS_KEYWORDS_SHORT = ("bearish", "selloff", "sell-off", "downtrend", "dump",
                       "selling pressure")
# Bobot per kata kunci. WAJIB identik dengan RISK_WORDS di
# n8n/code/mirofish_adapter.js; tools/validate_mirofish_contract.py membandingkan
# keduanya dan tests/verdict_cases.json menguji hasilnya di kedua bahasa.
RISK_WEIGHTS: dict[str, float] = {
    "hack": 0.4, "exploit": 0.4, "rug pull": 0.4, "depeg": 0.4,
    "insolvent": 0.4, "insolven": 0.4, "black swan": 0.45, "crash": 0.4,
    "lawsuit": 0.3, "gugatan": 0.3, "delisting": 0.3, "sanksi": 0.3,
    "regulator": 0.25, "panic": 0.3, "kepanikan": 0.3, "ban": 0.3,
    "default": 0.3, "outage": 0.25, "likuidasi": 0.25, "pengumuman": 0.2,
    "kebijakan": 0.2, "intervensi": 0.3, "ketidakpastian": 0.25,
    "volatilitas tinggi": 0.3,
}
LOW_WEIGHTS: dict[str, float] = {
    "stable": 0.1, "stabil": 0.1, "calm": 0.1, "tenang": 0.1, "normal": 0.1,
    "sideways": 0.1,
}

HIGH_THRESHOLD = 0.66
MEDIUM_THRESHOLD = 0.33


def utcnow_iso() -> str:
    """ISO-8601 UTC dengan presisi detik (bukan milidetik) — `2026-09-13T08:00:00Z`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ContractError(ValueError):
    """Keluaran MiroFish tidak bisa dipetakan ke kontrak tanpa menebak."""


# -------------------------------------------------------------------------- verdict


def _coerce_confidence(value: Any) -> float:
    """Confidence 0..1, atau ContractError.

    Sengaja ketat. Di JavaScript `Number(undefined) === 0` dan di Python
    `float(None)` melempar TypeError — dua-duanya jalan menuju "confidence 0" yang
    lolos gerbang. Kalau angkanya tidak ada, verdict-nya harus ditolak, bukan
    dibaca sebagai nol.
    """
    if value is None or isinstance(value, bool):
        raise ContractError("CONFIDENCE_MISSING")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ContractError("CONFIDENCE_MISSING")
        try:
            value = float(value)
        except ValueError:
            raise ContractError("CONFIDENCE_MISSING") from None
    try:
        conf = float(value)
    except (TypeError, ValueError):
        raise ContractError("CONFIDENCE_MISSING") from None
    if conf != conf or conf < 0.0 or conf > 1.0:
        raise ContractError("CONFIDENCE_OUT_OF_RANGE")
    return conf


def _bias_from_text(text: str) -> str:
    """LONG/SHORT/NEUTRAL dari teks bebas.

    Hanya istilah yang tidak ambigu dihitung, dan kalau dua arah sama-sama kena
    hasilnya NEUTRAL. Kata tunggal yang bisa dua arah ("volatile", "kuat", "naik
    tapi") tidak dipakai sama sekali — false positive di sini berarti bot menahan
    diri tanpa alasan, atau lebih buruk, masuk tanpa alasan.
    """
    lowered = text.lower()
    long_hits = sum(lowered.count(k) for k in BIAS_KEYWORDS_LONG)
    short_hits = sum(lowered.count(k) for k in BIAS_KEYWORDS_SHORT)
    if long_hits and short_hits:
        return "NEUTRAL"
    if long_hits >= 2:
        return "LONG"
    if short_hits >= 2:
        return "SHORT"
    return "NEUTRAL"


def _hits(text: str, keyword: str) -> int:
    """Hitung kemunculan kata kunci.

    Kata kunci pendek (<= 4 karakter, mis. "ban") hanya dihitung sebagai KATA UTUH.
    Tanpa ini, "ban" cocok di dalam "besar", "banjir", atau "band" — dan sebuah
    verdict tenang akan terbaca sebagai risiko sedang. Kesalahan seperti itu tidak
    menghentikan bot, ia hanya membuatnya terlihat waspada tanpa alasan, yang
    mengikis kepercayaan pada sinyal yang benar-benar penting.
    """
    if len(keyword) > 4:
        return text.count(keyword)
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text))


def _risk_score(text: str) -> float:
    """0..1 dari jumlah bobot kata risiko yang muncul.

    Teks tanpa kata risiko apa pun menghasilkan 0.0, bukan 0.5. Ini penting: dengan
    pemetaan (x+1)/2 yang dipakai sebelumnya, verdict paling tenang justru jatuh di
    tengah pita MEDIUM, sehingga setiap verdict normal terlihat "berisiko sedang"
    dan level HIGH kehilangan artinya.
    """
    lowered = text.lower()
    score = sum(w for k, w in RISK_WEIGHTS.items() if _hits(lowered, k))
    score -= sum(w for k, w in LOW_WEIGHTS.items() if _hits(lowered, k))
    return round(max(0.0, min(1.0, score)), 4)


def _risk_level(score: float) -> str:
    if score >= HIGH_THRESHOLD:
        return "HIGH"
    if score >= MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def normalize_verdict(
    verdict: dict[str, Any] | None,
    *,
    run_id: str,
    horizon_hours: int | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """verdict.json MiroFish -> amplop standar. GAGAL = VETO.

    Aturan tunggal fungsi ini: kalau ada bagian yang tidak bisa dipastikan, hasilnya
    `schema_ok: false` dan `event_risk: "HIGH"`. Tidak ada jalur yang menghasilkan
    "aman" dari data yang tidak lengkap.
    """
    notes: list[str] = []
    envelope: dict[str, Any] = {
        "run_id": run_id,
        "verdict_ts": now or utcnow_iso(),
        "schema_ok": False,
        "bias": "NEUTRAL",
        "confidence": None,
        "event_risk": "HIGH",
        "horizon_hours": horizon_hours,
        "evidence": [],
        "adapter_version": ADAPTER_VERSION,
        "notes": notes,
        "risk_score": 1.0,
    }

    if not isinstance(verdict, dict) or not verdict:
        notes.append("VERDICT_MISSING")
        return envelope

    prediction = verdict.get("prediction")
    if not isinstance(prediction, str) or len(prediction.strip()) < 20:
        notes.append("PREDICTION_MISSING_OR_TOO_SHORT")
        return envelope

    try:
        confidence = _coerce_confidence(verdict.get("confidence"))
    except ContractError as exc:
        notes.append(str(exc))
        return envelope

    dynamics = verdict.get("key_dynamics") or []
    signals = verdict.get("signals") or []
    if not isinstance(dynamics, list) or not isinstance(signals, list):
        notes.append("EVIDENCE_NOT_A_LIST")
        return envelope

    text_blob = " ".join(
        [prediction] + [str(x) for x in dynamics] + [str(x) for x in signals]
    )
    risk_score = _risk_score(text_blob)

    envelope.update(
        {
            "schema_ok": True,
            "bias": _bias_from_text(text_blob),
            "confidence": confidence,
            "event_risk": _risk_level(risk_score),
            "evidence": [str(x) for x in dynamics][:10],
            "notes": notes,
            "risk_score": round(risk_score, 4),
        }
    )
    if confidence < 0.60:
        notes.append("CONFIDENCE_BELOW_GATE")
    return envelope


# ------------------------------------------------------------------ keluaran CLI


def parse_cli_json(stdout: str) -> dict[str, Any]:
    """Ambil objek JSON dari `mirofish run --json`.

    CLI bisa mencetak baris log sebelum JSON-nya. Diambil objek JSON terakhir yang
    punya `run_id` — kalau tidak ada, itu kegagalan, bukan dict kosong.
    """
    if not stdout or not stdout.strip():
        raise ContractError("CLI_NO_OUTPUT")
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    idx = 0
    text = stdout
    while idx < len(text):
        start = text.find("{", idx)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        idx = max(end, start + 1)
    with_run = [o for o in found if o.get("run_id")]
    if not with_run:
        raise ContractError("CLI_NO_RUN_ID")
    return with_run[-1]


def locate_artifacts(run_id: str, root: Path) -> Path | None:
    """Cari direktori artefak run (immutable) milik MiroFish."""
    candidates = [
        root / "uploads" / "runs" / run_id,
        root / "runs" / run_id,
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def read_verdict_file(artifacts: Path) -> dict[str, Any] | None:
    for rel in ("report/verdict.json", "verdict.json"):
        path = artifacts / rel
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
            if isinstance(data, dict):
                return data
    return None


# ----------------------------------------------------------------------- job store


@dataclass
class Job:
    job_id: str
    symbol: str
    status: str = "PENDING"          # PENDING|RUNNING|SUCCEEDED|FAILED
    created_at: str = field(default_factory=utcnow_iso)
    started_at: str | None = None
    finished_at: str | None = None
    run_id: str | None = None
    error: str | None = None
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "symbol": self.symbol,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "run_id": self.run_id,
            "error": self.error,
            "exit_code": self.exit_code,
        }


class JobStore:
    """Store job berbasis direktori. Satu berkas state.json per job.

    Bukan database: job bersifat sementara dan satu-satunya yang perlu bertahan
    adalah verdict-nya, yang sudah masuk Postgres lewat n8n.
    """

    _VALID_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _dir(self, job_id: str) -> Path:
        if not self._VALID_ID.match(job_id):
            raise ContractError("INVALID_JOB_ID")
        return self.root / job_id

    def create(self, job_id: str, symbol: str) -> Job:
        with self._lock:
            d = self._dir(job_id)
            if d.exists():
                raise ContractError("JOB_ALREADY_EXISTS")
            d.mkdir(parents=True)
            job = Job(job_id=job_id, symbol=symbol)
            (d / "state.json").write_text(
                json.dumps(job.to_dict(), indent=2), encoding="utf-8"
            )
            return job

    def save(self, job: Job) -> None:
        with self._lock:
            path = self._dir(job.job_id) / "state.json"
            path.write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")

    def get(self, job_id: str) -> Job | None:
        path = self._dir(job_id) / "state.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return Job(**{k: data.get(k) for k in Job.__dataclass_fields__})

    def latest_running(self) -> Job | None:
        jobs = []
        for entry in sorted(self.root.iterdir(), reverse=True):
            job = self.get(entry.name)
            if job and job.status == "RUNNING":
                jobs.append(job)
        return jobs[0] if jobs else None


# ------------------------------------------------------------------ eksekusi CLI


def build_command(symbol: str, *, requirement: str | None, max_rounds: int,
                  platform: str, mirofish_bin: str) -> list[str]:
    """Susun perintah CLI. Tidak ada shell=True, tidak ada interpolasi string."""
    req = requirement or (
        f"Prediksi arah {symbol} dalam 4 jam ke depan berdasarkan dinamika pasar, "
        f"berita, dan sentimen sosial. Sebutkan pemicu risiko peristiwa yang bisa "
        f"membatalkan prediksi."
    )
    return [
        mirofish_bin, "run",
        "--requirement", req,
        "--max-rounds", str(max_rounds),
        "--platform", platform,
        "--json",
    ]


def _fake_run(job_dir: Path, symbol: str) -> None:
    """Mode uji/offline: hasilkan artefak palsu dengan bentuk yang benar.

    Dipakai saat MIROFISH_FAKE=1. Tanpa ini, seluruh lapisan runner tidak bisa diuji
    di CI karena CLI MiroFish (dan kredensial LLM-nya) tidak ada di sana — dan kode
    yang tidak bisa diuji adalah kode yang tidak akan ketahuan rusaknya.
    """
    run_id = f"fake-{job_dir.name}-{int(time.time())}"
    report = job_dir / "artifacts" / "report"
    report.mkdir(parents=True, exist_ok=True)
    verdict = {
        "prediction": (
            f"Tidak ada pemicu besar terdeteksi untuk {symbol}; dinamika pasar "
            f"cenderung stabil dan bergerak dalam rentang normal."
        ),
        "confidence": 0.71,
        "key_dynamics": ["Volume stabil", "Tidak ada berita regulatori"],
        "signals": ["Sentimen sosial netral"],
    }
    (report / "verdict.json").write_text(
        json.dumps(verdict, indent=2), encoding="utf-8"
    )
    (job_dir / "cli.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    (job_dir / "run_id").write_text(run_id, encoding="utf-8")


class Runner:
    """Menjalankan satu job MiroFish di thread latar."""

    def __init__(
        self,
        *,
        store: JobStore,
        artifacts_root: Path,
        mirofish_bin: str = "mirofish",
        timeout_sec: int = 3600,
        max_rounds: int = 10,
        platform: str = "parallel",
        fake: bool = False,
        cwd: str | None = None,
    ) -> None:
        self.store = store
        self.artifacts_root = Path(artifacts_root)
        self.mirofish_bin = mirofish_bin
        self.timeout_sec = timeout_sec
        self.max_rounds = max_rounds
        self.platform = platform
        self.fake = fake
        self.cwd = cwd

    # -- API yang dipakai lapisan HTTP ------------------------------------------------

    def submit(self, job_id: str, symbol: str) -> Job:
        job = self.store.create(job_id, symbol)
        thread = threading.Thread(
            target=self._execute, args=(job_id, symbol), daemon=True
        )
        thread.start()
        return job

    def verdict_for(self, job: Job) -> dict[str, Any]:
        """Bangun respons verdict untuk job.

        BENTUK RESPONS INI ADALAH KONTRAK. n8n/code/mirofish_adapter.js membaca
        `verdict`, `manifest`, dan `run_id` dari sini; tools/validate_mirofish_contract.py
        memverifikasi silang keduanya. Mengubah salah satu tanpa yang lain akan
        membuat SETIAP verdict terbaca sebagai VERDICT_MISSING, yaitu veto permanen
        -- bot berhenti trading diam-diam, yang lebih buruk daripada error.

        Job yang belum SUCCEEDED tetap mengembalikan 200 dengan verdict kosong,
        sehingga amplopnya `schema_ok: false` / `event_risk: "HIGH"`. Tidak ada
        keadaan setengah jadi yang bisa terbaca sebagai izin trading.
        """
        artifacts = None
        raw_verdict = None
        if job.status == "SUCCEEDED" and job.run_id:
            artifacts = locate_artifacts(job.run_id, self.artifacts_root)
            if artifacts is None:
                fallback = self.store._dir(job.job_id) / "artifacts"
                if fallback.is_dir():
                    artifacts = fallback
            if artifacts is not None:
                raw_verdict = read_verdict_file(artifacts)

        envelope = normalize_verdict(
            raw_verdict, run_id=job.run_id or job.job_id, now=utcnow_iso()
        )
        if job.status != "SUCCEEDED":
            envelope["notes"] = envelope["notes"] + [f"JOB_{job.status}"]
            envelope["risk_score"] = 1.0

        notes = list(envelope["notes"])
        if raw_verdict is None and job.status == "SUCCEEDED":
            notes.append("ARTIFACT_VERDICT_UNREADABLE")

        return {
            "run_id": job.run_id or job.job_id,
            "job_id": job.job_id,
            "job_status": job.status,
            "created_at": job.created_at,
            "contract_version": CONTRACT_VERSION,
            "verdict": raw_verdict if isinstance(raw_verdict, dict) else None,
            "summary": None,
            "manifest": {
                "run_id": job.run_id or job.job_id,
                "created_at": job.created_at,
                "artifacts": str(artifacts) if artifacts else None,
            },
            "envelope": envelope,
            "adapter_version": ADAPTER_VERSION,
            "notes": notes,
        }

    # -- internals --------------------------------------------------------------------

    def _execute(self, job_id: str, symbol: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        job.status = "RUNNING"
        job.started_at = utcnow_iso()
        self.store.save(job)

        job_dir = self.store._dir(job_id)
        try:
            if self.fake:
                _fake_run(job_dir, symbol)
                run_id = (job_dir / "run_id").read_text(encoding="utf-8").strip()
                job.exit_code = 0
            else:
                cmd = build_command(
                    symbol, requirement=None, max_rounds=self.max_rounds,
                    platform=self.platform, mirofish_bin=self.mirofish_bin,
                )
                with (job_dir / "stdout.log").open("wb") as out, \
                     (job_dir / "stderr.log").open("wb") as err:
                    proc = subprocess.run(
                        cmd, stdout=out, stderr=err, timeout=self.timeout_sec,
                        cwd=self.cwd, check=False,
                    )
                job.exit_code = proc.returncode
                if proc.returncode != 0:
                    raise ContractError(f"CLI_EXIT_{proc.returncode}")
                payload = parse_cli_json(
                    (job_dir / "stdout.log").read_text(encoding="utf-8")
                )
                run_id = str(payload["run_id"])
                if locate_artifacts(run_id, self.artifacts_root) is None:
                    raise ContractError("ARTIFACTS_NOT_FOUND")

            job.run_id = run_id
            job.status = "SUCCEEDED"
        except subprocess.TimeoutExpired:
            job.status = "FAILED"
            job.error = f"CLI_TIMEOUT_{self.timeout_sec}s"
        except (ContractError, OSError, json.JSONDecodeError) as exc:
            job.status = "FAILED"
            job.error = str(exc)
        finally:
            job.finished_at = utcnow_iso()
            self.store.save(job)


def env_config(base_dir: Path | None = None) -> dict[str, Any]:
    """Baca konfigurasi dari environment. Tidak ada nilai rahasia yang punya default."""
    base = Path(base_dir or os.environ.get("MIROFISH_RUNNER_DATA", "data/mirofish"))
    return {
        "jobs_root": base / "jobs",
        "artifacts_root": Path(
            os.environ.get("MIROFISH_ARTIFACTS", str(base / "runs"))
        ),
        "mirofish_bin": os.environ.get("MIROFISH_BIN", "mirofish"),
        "timeout_sec": int(os.environ.get("MIROFISH_TIMEOUT_SEC", "3600")),
        "max_rounds": int(os.environ.get("MIROFISH_MAX_ROUNDS", "10")),
        "platform": os.environ.get("MIROFISH_PLATFORM", "parallel"),
        "fake": os.environ.get("MIROFISH_FAKE", "0") == "1",
        "cwd": os.environ.get("MIROFISH_CWD"),
        "token": os.environ.get("MIROFISH_RUNNER_TOKEN", ""),
    }
