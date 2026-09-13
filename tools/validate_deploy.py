#!/usr/bin/env python3
"""tools/validate_deploy.py — memeriksa berkas deployment terhadap sisa repo.

Deployment adalah bagian yang paling sulit diuji otomatis (butuh Docker daemon),
jadi yang bisa diperiksa statically diperiksa di sini:

  1. docker-compose.yml adalah YAML yang sah dan menyebut ketiga layanan
  2. setiap ${VAR} yang dirujuk compose ada di deploy/.env.example
  3. setiap path yang di-mount compose benar-benar ada di repo
  4. tidak ada port yang dipublikasikan ke 0.0.0.0 (hanya 127.0.0.1 yang boleh)
  5. Dockerfile menyalin direktori yang benar-benar ada
  6. tidak ada nilai rahasia yang ditulis langsung di berkas deploy

Keluar dengan kode 1 bila ada pelanggaran.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
failures: list[str] = []
checks: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def ok(msg: str) -> None:
    checks.append(msg)


def main() -> int:
    compose_path = ROOT / "deploy/docker-compose.yml"
    env_path = ROOT / "deploy/.env.example"
    dockerfile = ROOT / "deploy/Dockerfile.mirofish-runner"

    for p in (compose_path, env_path, dockerfile):
        if not p.is_file():
            fail(f"berkas tidak ada: {p.relative_to(ROOT)}")
    if failures:
        for f in failures:
            print(f"[GAGAL] {f}")
        return 1

    compose = compose_path.read_text(encoding="utf-8")
    env_example = env_path.read_text(encoding="utf-8")

    # 1. YAML sah + ketiga layanan ada
    try:
        import yaml  # type: ignore
        doc = yaml.safe_load(compose)
        services = set(doc.get("services", {}))
        for wajib in ("postgres", "n8n", "mirofish-runner"):
            if wajib not in services:
                fail(f"layanan `{wajib}` tidak ada di docker-compose.yml")
        ok(f"YAML sah, {len(services)} layanan: {sorted(services)}")
    except ImportError:
        # Tanpa PyYAML: ambil kunci ber-indentasi 2 spasi di dalam blok `services:`,
        # berhenti di kunci top-level berikutnya. Regex naif "^  (\w+):$" ikut
        # menangkap `volumes:` dan `networks:` dan melaporkan 5 "layanan".
        block = re.search(r"^services:\n(.*?)(?=^[a-z])", compose, re.M | re.S)
        services = set(re.findall(r"^  ([A-Za-z][A-Za-z0-9_-]*):$",
                                  block.group(1) if block else "", re.M))
        for wajib in ("postgres", "n8n", "mirofish-runner"):
            if wajib not in services:
                fail(f"layanan `{wajib}` tidak ada di docker-compose.yml")
        ok(f"PyYAML tidak ada; {len(services)} layanan dikenali lewat regex: {sorted(services)}")

    # 2. ${VAR} di compose harus ada di .env.example
    referenced = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)(?::[-+][^}]*)?\}", compose))
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", env_example, re.M))
    missing = referenced - declared
    if missing:
        fail(f"compose memakai variabel tanpa entri di .env.example: {sorted(missing)}")
    ok(f"{len(referenced & declared)} variabel compose terdaftar di .env.example")

    # 3. path mount harus ada
    for rel in re.findall(r"- \.\./([A-Za-z0-9_./-]+):", compose):
        if not (ROOT / rel).exists():
            fail(f"mount compose merujuk path yang tidak ada: ../{rel}")
    mounted = re.findall(r"- \.\./([A-Za-z0-9_./-]+):", compose)
    if mounted:
        ok(f"{len(mounted)} mount compose mengarah ke path yang ada")

    # 4. tidak ada port yang terbuka ke semua antarmuka
    for port in re.findall(r'^\s+- "([^"]+:\d+:\d+)"', compose, re.M):
        if not port.startswith("127.0.0.1:"):
            fail(f"port dipublikasikan ke semua antarmuka: {port} (harus 127.0.0.1:...)")
    for bare in re.findall(r'^\s+- "\d+:\d+"', compose, re.M):
        fail(f"port dipublikasikan tanpa bind 127.0.0.1: {bare}")
    published = re.findall(r'^\s+- "[^"]*\d+:\d+"', compose, re.M)
    ok(f"{len(published)} port dipublikasikan, semuanya lewat 127.0.0.1")

    # 5. Dockerfile menyalin direktori yang ada
    df = dockerfile.read_text(encoding="utf-8")
    for src in re.findall(r"^COPY ([A-Za-z0-9_./-]+) ", df, re.M):
        if not (ROOT / src).exists():
            fail(f"Dockerfile menyalin path yang tidak ada: {src}")
    if "requirements" not in df and "pip install" in df:
        ok("Dockerfile memasang dependensi secara eksplisit")

    # 6. tidak ada rahasia hardcoded
    for path in (compose_path, env_path, dockerfile):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.split("#")[0]
            if re.search(r"(API_SECRET|PASSWORD|TOKEN|KEY)\s*[:=]\s*['\"]?[A-Za-z0-9]{16,}",
                         stripped):
                fail(f"{path.name}: kemungkinan rahasia tertulis langsung: {line.strip()[:60]}")
    ok("tidak ada nilai rahasia tertulis langsung di berkas deploy")

    for c in checks:
        print(f"[OK  ] {c}")
    if failures:
        print()
        for f in failures:
            print(f"[GAGAL] {f}")
        print(f"\n{len(failures)} pelanggaran deployment.")
        return 1
    print(f"\nBerkas deployment konsisten ({len(checks)} pemeriksaan).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
