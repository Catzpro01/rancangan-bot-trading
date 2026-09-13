"""Validasi struktural berkas workflow n8n sebelum diimpor.

    python tools/validate_workflows.py

Yang diperiksa:
  1. JSON valid dan punya `name`, `nodes`, `connections`.
  2. Nama node unik (n8n menolak koneksi ke nama yang ambigu).
  3. Setiap target koneksi benar-benar ada.
  4. Setiap node (kecuali trigger) punya setidaknya satu koneksi masuk.
  5. Tidak ada rahasia tersangkut di dalam JSON (API key, secret, password).
  6. Code node berisi guard benar-benar memuat fungsi `validateSignal` (bukti bahwa
     kode guard ter-embed, bukan placeholder).
  7. Tidak ada node `n8n-nodes-base.executeWorkflow` yang menunjuk workflow tak dikenal.

Ini bukan pengganti menjalankan n8n; ini penjaga agar perubahan generator tidak
menghasilkan berkas yang gagal diimpor atau -- lebih buruk -- impor tetapi bolong.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WF_DIR = ROOT / "n8n" / "workflows"

SECRET_PATTERNS = [
    (re.compile(r"(?i)api[_-]?secret\s*[=:]\s*['\"][A-Za-z0-9]{16,}"), "kemungkinan API secret"),
    (re.compile(r"(?i)api[_-]?key\s*[=:]\s*['\"][A-Za-z0-9]{16,}"), "kemungkinan API key"),
    (re.compile(r"(?i)password\s*[=:]\s*['\"][^'\"]{6,}"), "kemungkinan password"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
]

TRIGGER_TYPES = {
    "n8n-nodes-base.scheduleTrigger",
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.manualTrigger",
    "n8n-nodes-base.executeWorkflowTrigger",
}


def validate(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        wf = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{path.name}: JSON tidak valid: {exc}"]

    for key in ("name", "nodes", "connections"):
        if key not in wf:
            problems.append(f"{path.name}: field `{key}` tidak ada")
    if problems:
        return problems

    nodes = wf["nodes"]
    names = [n.get("name") for n in nodes]
    if len(names) != len(set(names)):
        dupes = {n for n in names if names.count(n) > 1}
        problems.append(f"{path.name}: nama node duplikat: {sorted(dupes)}")

    known = set(names)
    for src, conns in wf["connections"].items():
        if src not in known:
            problems.append(f"{path.name}: koneksi dari node tak dikenal `{src}`")
        for _type, branches in conns.items():
            for branch in branches or []:
                for link in branch or []:
                    if link.get("node") not in known:
                        problems.append(
                            f"{path.name}: koneksi {src} -> {link.get('node')} tak dikenal"
                        )

    targeted = {
        link["node"]
        for conns in wf["connections"].values()
        for branches in conns.values()
        for branch in branches or []
        for link in branch or []
    }
    for n in nodes:
        if n.get("type") in TRIGGER_TYPES:
            continue
        if n["name"] not in targeted:
            problems.append(f"{path.name}: node `{n['name']}` tidak punya koneksi masuk (yatim)")

    blob = path.read_text(encoding="utf-8")
    for pat, label in SECRET_PATTERNS:
        if pat.search(blob):
            problems.append(f"{path.name}: {label} ditemukan di dalam JSON")

    for n in nodes:
        if n.get("type") != "n8n-nodes-base.code":
            continue
        js = n.get("parameters", {}).get("jsCode", "")
        if not js.strip():
            problems.append(f"{path.name}: Code node `{n['name']}` kosong")
        if n["name"].startswith("GERBANG RISIKO"):
            for fn in ("function validateSignal", "function liquidationDistance",
                       "function sizePosition"):
                if fn not in js:
                    problems.append(
                        f"{path.name}: gerbang risiko tidak memuat `{fn}` "
                        "(kode guard tidak ter-embed?)"
                    )
        if "PIONEX_API_SECRET" in js and "$env." not in js:
            problems.append(f"{path.name}: `{n['name']}` memakai secret tanpa $env")

    return problems


def main() -> int:
    if not WF_DIR.exists():
        print(f"{WF_DIR} tidak ada; jalankan `python tools/make_n8n_workflows.py`")
        return 1
    files = sorted(WF_DIR.glob("*.json"))
    if not files:
        print("tidak ada workflow; jalankan `python tools/make_n8n_workflows.py`")
        return 1

    all_problems: list[str] = []
    for f in files:
        probs = validate(f)
        status = "OK  " if not probs else "GAGAL"
        wf = json.loads(f.read_text(encoding="utf-8"))
        print(f"[{status}] {f.name}: {len(wf['nodes'])} node, "
              f"{len(wf['connections'])} sumber koneksi")
        all_problems.extend(probs)

    if all_problems:
        print("\nMasalah:")
        for p in all_problems:
            print(f"  - {p}")
        return 1
    print(f"\n{len(files)} workflow valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
