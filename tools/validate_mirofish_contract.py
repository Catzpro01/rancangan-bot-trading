#!/usr/bin/env python3
"""tools/validate_mirofish_contract.py — pemeriksa kontrak antar-lapisan.

Yang diperiksa (semuanya dibaca dari berkas, tidak ada yang di-hardcode dua kali):

  1. RUNNER  -> ADAPTER   : kunci respons runner.py benar-benar dibaca mirofish_adapter.js
  2. ADAPTER -> DATABASE   : kunci amplop adapter == kolom INSERT INTO mirofish_verdict
  3. WORKFLOW-> DATABASE   : setiap tabel & kolom yang dirujuk SQL workflow ada di db/schema.sql
  4. KODE    -> DOKUMENTASI: setiap kode alasan penolakan di kode tercatat di docs/04,
                             dan sebaliknya (daftar usang di docs itu menyesatkan saat insiden)
  5. WORKFLOW-> LINGKUNGAN : setiap $env.X yang dipakai workflow ada di deploy/.env.example
  6. WORKFLOW-> DOKUMENTASI: setiap endpoint Pionex yang dipanggil tercatat di docs/05

Alasan berkas ini ada: proyek ini punya TIGA salinan dari kebenaran yang sama
(Python, JavaScript, dan SQL di dalam JSON n8n). Ketiganya bisa menyimpang tanpa
ada satu pun test yang gagal, dan penyimpangannya biasanya baru ketahuan saat bot
sedang berjalan dengan uang sungguhan.

Keluar dengan kode 1 bila ada pelanggaran.
"""

from __future__ import annotations

import json
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


# ------------------------------------------------------------------- 1. runner -> adapter

RUNNER_KEYS = {
    "run_id", "job_id", "job_status", "created_at", "contract_version",
    "verdict", "summary", "manifest", "envelope", "adapter_version", "notes",
}


def read(path: Path) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def check_runner_adapter_contract() -> None:
    runner_src = read(Path("mirofish_runner/runner.py"))
    adapter_src = read(Path("n8n/code/mirofish_adapter.js"))

    # Kunci yang benar-benar dikembalikan verdict_for (dari dict literal terakhir).
    start = runner_src.index("def verdict_for")
    block = runner_src[start:runner_src.index("# -- internals")]
    returned = set(re.findall(r'^\s{12}"([a-z_]+)":', block, re.M))
    missing_in_runner = RUNNER_KEYS - returned
    if missing_in_runner:
        fail(f"runner.py tidak mengembalikan kunci kontrak: {sorted(missing_in_runner)}")

    # Kunci yang dibaca adapter dari respons (raw?.X / raw.X).
    consumed = set(re.findall(r"raw\??\.([a-z_]+)", adapter_src))
    consumed |= set(re.findall(r"manifest\??\.([a-z_]+)", adapter_src))
    unconsumed_top = {"verdict", "manifest", "run_id", "job_id", "summary"} - consumed
    if unconsumed_top:
        fail(f"adapter tidak membaca kunci yang dikirim runner: {sorted(unconsumed_top)}")

    # Kunci yang dibaca adapter dari dalam verdict mentah.
    verdict_reads = set(re.findall(r"verdict\??\.([a-z_]+)", adapter_src))
    required_verdict_keys = {"prediction", "confidence", "key_dynamics", "signals"}
    if not required_verdict_keys <= verdict_reads:
        fail(
            "adapter tidak memeriksa field verdict wajib: "
            f"{sorted(required_verdict_keys - verdict_reads)}"
        )
    ok(f"runner -> adapter: {len(consumed & RUNNER_KEYS)} kunci respons terpakai")


# ------------------------------------------------------------------ 2. adapter -> tabel


def check_adapter_matches_insert() -> None:
    adapter_src = read(Path("n8n/code/mirofish_adapter.js"))
    envelope = set(
        re.findall(r"^\s{4}([a-z_]+):", adapter_src[adapter_src.index("const base = {"):], re.M)
    )
    envelope |= {"risk_score"}   # hanya ada di jalur sukses

    schema = read(Path("db/schema.sql"))
    insert = None
    for wf in sorted((ROOT / "n8n/workflows").glob("*.json")):
        for m in re.finditer(r"INSERT INTO mirofish_verdict\s*\(([^)]+)\)", json.dumps(wf.read_text())):
            insert = m.group(1)
    if not insert:
        fail("tidak ditemukan INSERT INTO mirofish_verdict di workflow")
        return
    columns = {c.strip() for c in insert.split(",")}
    envelope_no_meta = envelope - {"notes"}   # notes disimpan di dalam kolom raw
    missing_columns = envelope_no_meta - columns
    if missing_columns:
        fail(
            "amplop adapter punya kunci tanpa kolom di mirofish_verdict: "
            f"{sorted(missing_columns)}"
        )
    ok(f"adapter -> DB: {len(envelope_no_meta & columns)} kunci amplop punya kolom")


# --------------------------------------------------------------------- 3. SQL -> schema

DDL_KEYWORDS = {
    "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT", "CREATE", "COMMENT",
    "INSERT", "SELECT", "WHERE", "INDEX", "OR", "EXISTS", "TABLE", "VIEW",
}


def parse_schema(sql: str) -> dict[str, set[str]]:
    """Petakan nama_tabel -> himpunan kolom dari CREATE TABLE."""
    tables: dict[str, set[str]] = {}
    for m in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", sql, re.S
    ):
        name, body = m.group(1), m.group(2)
        cols: set[str] = set()
        depth = 0
        for raw_line in body.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            first = re.match(r"([A-Za-z_]\w*)", line)
            if not first:
                continue
            word = first.group(1).upper()
            if word in DDL_KEYWORDS:
                continue
            # Baris definisi kolom: nama diikuti tipe. Baris constraint multi-kata
            # yang tidak diawali kata kunci (jarang) tetap lolos karena tokennya >1.
            tokens = line.split()
            if len(tokens) >= 2:
                cols.add(first.group(1).lower())
            _ = depth
        tables[name.lower()] = cols
    return tables


def check_workflows_against_schema() -> None:
    schema = parse_schema(read(Path("db/schema.sql")))
    if not schema:
        fail("db/schema.sql tidak menghasilkan tabel apa pun")
        return
    all_columns = set().union(*schema.values())
    n8n_tokens = {
        "json", "env", "now", "int", "jsonb", "text", "boolean", "count", "greatest",
        "max", "date_trunc", "day", "extract", "epoch", "round", "filter", "true",
        "false", "null", "and", "or", "not", "is", "in", "on", "conflict", "do",
        "nothing", "where", "values", "set", "order", "by", "desc", "asc", "limit",
        "select", "from", "insert", "into", "update", "group", "having", "as",
    }
    sql_count = 0
    for wf_path in sorted((ROOT / "n8n/workflows").glob("*.json")):
        wf = json.loads(wf_path.read_text(encoding="utf-8"))
        for node in wf["nodes"]:
            params = json.dumps(node.get("parameters", {}))
            for m in re.finditer(
                r'"((?:SELECT|INSERT INTO|UPDATE|DELETE FROM)[^"]{15,})"', params
            ):
                sql = m.group(1)
                sql_count += 1
                clean = re.sub(r"\{\{[^}]*\}\}", " 0 ", sql)
                clean = re.sub(r"'[^']*'", " 'x' ", clean)
                clean = re.sub(r"::[a-z]+", " ", clean)
                for tm in re.finditer(
                    r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([a-z_][a-z0-9_]*)", clean, re.I
                ):
                    table = tm.group(1).lower()
                    if table in n8n_tokens or table in ("select",):
                        continue
                    if table not in schema:
                        fail(f"{wf_path.name} :: {node['name']}: tabel `{table}` tidak ada di db/schema.sql")
                for ident in re.findall(r"\b([a-z_][a-z0-9_]{2,})\b", clean.lower()):
                    if ident in n8n_tokens or ident in all_columns or ident in schema:
                        continue
                    if "_" not in ident:      # hanya periksa yang bergaya nama kolom
                        continue
                    fail(f"{wf_path.name} :: {node['name']}: kolom `{ident}` tidak dikenal di schema")
    ok(f"SQL workflow -> schema: {sql_count} pernyataan diperiksa, {len(schema)} tabel")


# -------------------------------------------------------- 4. kode alasan <-> dokumentasi

REASON_FILES = (
    Path("risk_engine/risk_engine.py"),
    Path("n8n/code/risk_guard.js"),
    Path("n8n/code/mirofish_adapter.js"),
    Path("tools/make_n8n_workflows.py"),   # berisi badan Code node yang di-embed
)
IGNORED_CODES = {
    # Nilai enum/HTTP/method, bukan kode penolakan.
    "LONG", "SHORT", "NEUTRAL", "NONE", "HIGH", "MEDIUM", "LOW", "UNKNOWN",
    "HOLD", "POST", "DELETE", "EFFECTIVE_LEVERAGE", "MARGIN_CAP", "RISK_BUDGET",
    "STOP_LOSS", "TAKE_PROFIT", "TIME_STOP", "ANOMALY", "BUY", "SELL", "SENDING",
}


# Kode yang dibangun secara tidak langsung sehingga tidak tertangkap pola teks:
# LEVERAGE_<n> dirangkai lewat template string, dan tiga kode ContractError di
# runner Python dilempar sebagai exception lalu masuk notes lewat str(exc).
EXTRA_REASON_CODES = {
    "CONFIDENCE_MISSING", "CONFIDENCE_OUT_OF_RANGE", "EVIDENCE_NOT_A_LIST",
    "CONFIDENCE_BELOW_GATE",
    "LEVERAGE_",   # dirangkai sebagai `LEVERAGE_${angka}` di Code node monitor
}


def collect_reason_codes() -> set[str]:
    """Kumpulkan kode alasan yang BENAR-BENAR dihasilkan kode.

    Tiga jenis kutip dipakai di sumber (', ", `), jadi kelas kutip dibangun dari
    karakternya langsung. Pola lama yang menulis "?\'? justru menelan tanda kutip
    tunggal dan membuat notes.push('VERDICT_MISSING') tidak pernah tertangkap.
    """
    codes: set[str] = set(EXTRA_REASON_CODES)
    q = chr(39) + chr(34) + chr(96)          # ' " `
    patterns = (
        r"(?:reasons?|notes|warnings|problems|anomalies)\.(?:append|push)\(\s*f?["
        + q + r"]([A-Z][A-Z0-9_]{3,})",
        r"reason\s*=\s*f?[" + q + r"]([A-Z][A-Z0-9_]{3,})",
        r"reason:\s*f?[" + q + r"]([A-Z][A-Z0-9_]{3,})",
        r"return \[false, [" + q + r"]([A-Z][A-Z0-9_]{3,})",
        r"halt_reason = [" + q + r"]([A-Z][A-Z0-9_]{3,})",
        r"anomalies\.push\([" + q + r"]([A-Z][A-Z0-9_]{3,})",
        r"anomalies\.push\(" + chr(96) + r"([A-Z][A-Z0-9_]{3,})",
    )
    sources = [read(rel) for rel in REASON_FILES]
    sources += [wf.read_text(encoding="utf-8")
                for wf in sorted((ROOT / "n8n/workflows").glob("*.json"))]
    for text in sources:
        for pat in patterns:
            for m in re.finditer(pat, text):
                codes.add(m.group(m.lastindex))
    # ANOMALY:LEVERAGE_<n> dirangkai lewat template string; dokumentasikan bentuknya.
    codes.add("LEVERAGE_")
    return {c for c in codes if c not in IGNORED_CODES}


def check_reason_codes_documented() -> None:
    codes = collect_reason_codes()
    docs = read(Path("docs/04-risiko-dan-guardrail.md"))
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", docs)) - IGNORED_CODES
    # Kode ANOMALY dirangkai dengan angka, jadi didokumentasikan sebagai
    # `LEVERAGE_<n>`; regex di atas hanya menangkap "LEVERAGE_" dari bentuk itu.
    if "`LEVERAGE_<n>`" in docs:
        documented.add("LEVERAGE_")
    undocumented = codes - documented
    if undocumented:
        fail(f"kode alasan ada di kode tapi tidak di docs/04: {sorted(undocumented)}")
    phantom = documented - codes
    if phantom:
        fail(f"docs/04 menyebut kode yang tidak dihasilkan kode mana pun: {sorted(phantom)}")
    ok(f"kode alasan: {len(codes & documented)} kode cocok antara kode dan docs/04")


# ---------------------------------------------------------------- 5. $env <-> .env.example


def check_risk_weights_parity() -> None:
    """Bobot kata risiko di runner Python harus sama dengan adapter JavaScript."""
    py_src = read(Path("mirofish_runner/runner.py"))
    js_src = read(Path("n8n/code/mirofish_adapter.js"))

    def parse_py(name: str) -> dict[str, float]:
        block = re.search(name + r"[^=]*=\s*\{(.*?)\n\}", py_src, re.S)
        if not block:
            fail(f"{name} tidak ditemukan di mirofish_runner/runner.py")
            return {}
        return {m.group(1): float(m.group(2))
                for m in re.finditer(r'"([^"]+)"\s*:\s*([0-9.]+)', block.group(1))}

    def parse_js(name: str) -> dict[str, float]:
        block = re.search(name + r"\s*=\s*\[(.*?)\n\];", js_src, re.S)
        if not block:
            fail(f"{name} tidak ditemukan di n8n/code/mirofish_adapter.js")
            return {}
        return {m.group(1): float(m.group(2))
                for m in re.finditer(r"\['([^']+)',\s*([0-9.]+)\]", block.group(1))}

    def parse_js_flat(name: str) -> dict[str, float]:
        block = re.search(name + r"\s*=\s*\[(.*?)\];", js_src, re.S)
        if not block:
            fail(f"{name} tidak ditemukan di n8n/code/mirofish_adapter.js")
            return {}
        return {w: 0.1 for w in re.findall(r"'([^']+)'", block.group(1))}

    py = {**parse_py("RISK_WEIGHTS"), **{k: -v for k, v in parse_py("LOW_WEIGHTS").items()}}
    js = {**parse_js("RISK_WORDS"), **{k: -v for k, v in parse_js_flat("LOW_WORDS").items()}}
    if py != js:
        only_py = sorted(set(py) - set(js))
        only_js = sorted(set(js) - set(py))
        beda = sorted(k for k in set(py) & set(js) if abs(py[k] - js[k]) > 1e-9)
        fail(f"bobot risiko Python != JavaScript: hanya-Python={only_py} "
             f"hanya-JS={only_js} bobot-beda={beda}")
    ok(f"bobot risiko: {len(py)} kata kunci identik di Python dan JavaScript")


def check_env_documented() -> None:
    env_file = ROOT / "deploy/.env.example"
    if not env_file.is_file():
        fail("deploy/.env.example tidak ada")
        return
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", env_file.read_text(), re.M))
    used: set[str] = set()
    for wf_path in sorted((ROOT / "n8n/workflows").glob("*.json")):
        used |= set(re.findall(r"\$env\.([A-Z][A-Z0-9_]+)", wf_path.read_text()))
    missing = used - declared
    if missing:
        fail(f"workflow memakai $env yang tidak ada di .env.example: {sorted(missing)}")
    unused = declared - used - {
        # Dipakai docker-compose / mirofish_runner, bukan workflow n8n.
        "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "DATABASE_URL",
        "N8N_BASIC_AUTH_USER", "N8N_BASIC_AUTH_PASSWORD", "N8N_HOST",
        "N8N_ENCRYPTION_KEY", "GENERIC_TIMEZONE", "MIROFISH_BIN",
        "MIROFISH_ARTIFACTS", "MIROFISH_TIMEOUT_SEC", "MIROFISH_MAX_ROUNDS",
        "MIROFISH_PLATFORM", "MIROFISH_FAKE", "MIROFISH_CWD", "MIROFISH_RUNNER_DATA",
        "TZ", "PIONEX_BASE_URL",
    }
    if unused:
        fail(f".env.example mendefinisikan variabel yang tidak dipakai siapa pun: {sorted(unused)}")
    ok(f"$env: {len(used & declared)} variabel workflow terdokumentasi")


# ------------------------------------------------------------- 6. endpoint <-> docs/05


def check_endpoints_documented() -> None:
    docs = read(Path("docs/05-pionex-api.md"))
    used: set[str] = set()
    for wf_path in sorted((ROOT / "n8n/workflows").glob("*.json")):
        src = wf_path.read_text(encoding="utf-8")
        used |= set(re.findall(r"(/(?:uapi|api)/v1/[A-Za-z0-9/{}$._-]+)", src))
    normalized = set()
    for path in used:
        path = path.rstrip(".")
        path = re.sub(r"\{\{[^}]*\}\}", "{symbol}", path)
        normalized.add(path.rstrip("/"))
    missing = sorted(p for p in normalized if p not in docs)
    if missing:
        fail(f"endpoint Pionex dipakai workflow tapi tidak ada di docs/05: {missing}")
    ok(f"endpoint Pionex: {len(normalized - set(missing))} endpoint tercatat di docs/05")


def main() -> int:
    for fn in (
        check_runner_adapter_contract,
        check_adapter_matches_insert,
        check_workflows_against_schema,
        check_reason_codes_documented,
        check_risk_weights_parity,
        check_env_documented,
        check_endpoints_documented,
    ):
        fn()

    for c in checks:
        print(f"[OK  ] {c}")
    if failures:
        print()
        for f in failures:
            print(f"[GAGAL] {f}")
        print(f"\n{len(failures)} pelanggaran kontrak.")
        return 1
    print(f"\nKontrak antar-lapisan konsisten ({len(checks)} pemeriksaan).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
