# Rancangan Bot Trading — "Pionex Guard 50x" (n8n + MiroFish + Pionex)

Rancangan lengkap bot trading perpetual futures di Pionex dengan **leverage bursa 50x**,
diatur oleh **n8n**, dengan **MiroFish** sebagai lapisan penilaian risiko peristiwa.

> **Baca `docs/01-ringkasan-dan-ancaman.md` lebih dulu.** Rancangan ini menyertakan
> temuan kuantitatif yang mengubah cara leverage 50x seharusnya dipakai — bukan sekadar
> "pasang 50x lalu berdoa".

---

## Isi repositori

| Bagian | Isi |
|---|---|
| `docs/01` | Ringkasan eksekutif + model ancaman + **temuan angka** |
| `docs/02` | Arsitektur & alur data |
| `docs/03` | Integrasi MiroFish (kontrak sinyal, adapter, batasannya) |
| `docs/04` | **Kerangka risiko & guardrail** (inti rancangan) + daftar lengkap kode alasan |
| `docs/05` | Peta endpoint Pionex + penandatanganan + idempotensi |
| `docs/06` | Spesifikasi 6 workflow n8n, node per node |
| `docs/07` | Deployment, rahasia, hardening, backup |
| `docs/08` | Rencana rollout & validasi (paper → live bertahap) |
| `docs/09` | Runbook insiden + kill switch |
| `docs/10` | Checklist pra-live |
| `risk_engine/` | Kernel risiko (Python, tanpa dependensi) — sumber kebenaran |
| `n8n/code/` | Kembaran JavaScript kernel + adapter verdict MiroFish |
| `n8n/workflows/` | 6 workflow siap impor (dihasilkan, jangan disunting) |
| `mirofish_runner/` | Layanan HTTP yang membungkus CLI MiroFish |
| `simulator/` | Monte Carlo untuk menguji rencana sebelum uang dipakai |
| `config/` | `risk_config.json` — satu-satunya tempat angka risiko diubah |
| `db/schema.sql` | Skema Postgres untuk state + audit trail |
| `deploy/` | `docker-compose.yml`, Dockerfile runner, `.env.example` |
| `tools/` | Generator workflow + 3 validator |
| `tests/` | 122 test Python + 63 test Node (10 + 13 + 24 + 16) |

## Mulai dari sini

```bash
make test        # seluruh pemeriksaan: Python, Node, validator struktur, validator kontrak

# atau satu per satu:
python -m pytest -q                                  # 122 test kernel risiko + runner
node --test tests/test_parity.js \
              tests/test_mirofish_adapter.js \
              tests/test_workflow_code_nodes.js \
              tests/test_mirofish_verdict_parity.js   # 63 test sisi JavaScript
python tools/validate_workflows.py                    # struktur 6 workflow n8n
python tools/validate_mirofish_contract.py            # kontrak antar-lapisan
python tools/validate_deploy.py                       # compose, Dockerfile, .env

# angka risiko rencana Anda sendiri
python simulator/monte_carlo.py --equity 1000 --leverage 50 \
       --win-rate 0.50 --stop-pct 0.008 --tp-pct 0.010 --liq-prob 0.30

# naikkan tumpukan (lihat docs/07)
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d
```

## Kenapa ada tiga validator

Proyek ini punya **tiga salinan dari kebenaran yang sama**: kernel Python, kembaran
JavaScript yang berjalan di n8n, dan SQL yang tertanam di dalam JSON workflow.
Ketiganya bisa menyimpang tanpa ada satu pun test yang gagal, dan penyimpangannya baru
ketahuan saat bot sedang berjalan dengan uang sungguhan.

| Validator | Menjaga agar |
|---|---|
| `tools/make_parity_vectors.py --check` | vektor uji Python↔JS tidak basi terhadap kernel |
| `tools/validate_workflows.py` | JSON n8n valid, tanpa rahasia, gerbang risiko ada di dalamnya |
| `tools/validate_mirofish_contract.py` | respons runner ↔ adapter ↔ kolom DB ↔ dokumen tetap cocok; bobot risiko Python == JavaScript; kode alasan di dokumen == yang dihasilkan kode |
| `tools/validate_deploy.py` | compose ↔ `.env.example` ↔ path di repo; tidak ada port yang terbuka ke semua antarmuka; tidak ada rahasia tertulis langsung |

CI di GitHub Actions menjalankan semuanya, **termasuk** pemeriksaan bahwa
`n8n/workflows/*.json` masih identik dengan keluaran generatornya — jadi kode guard di
dalam workflow tidak bisa tertinggal versi.

## Status verifikasi

| Yang diverifikasi | Bagaimana | Hasil |
|---|---|---|
| Matematika likuidasi, sizing, EV, circuit breaker | `python -m pytest -q` | **122 passed** |
| Port JavaScript identik dengan kernel Python | `tests/test_parity.js` + vektor yang dihasilkan mesin | 10/10 |
| Adapter verdict: gagal = veto, tidak pernah menebak | `tests/test_mirofish_adapter.js` | 13/13 |
| Paritas normalisasi verdict Python ↔ JavaScript | `tests/verdict_cases.json` di kedua bahasa | 11 kasus, 16/16 di JS |
| Kode yang benar-benar tertanam di JSON workflow | `tests/test_workflow_code_nodes.js` | 19/19 |
| Runner MiroFish: job, artefak, kontrak respons | `tests/test_mirofish_runner.py` | 29 passed |
| Lapisan HTTP runner: auth, kode status, bentuk respons | `tests/test_mirofish_runner_app.py` | 15 passed |
| Node multi-input menggabungkan cabang dengan benar | `tests/test_workflow_code_nodes.js` | 24/24 |
| Struktur JSON workflow n8n | `tools/validate_workflows.py` | 6 workflow valid |
| Kontrak antar-lapisan | `tools/validate_mirofish_contract.py` | 7 pemeriksaan |
| Berkas deployment | `tools/validate_deploy.py` | 6 pemeriksaan |
| Konsistensi `liquidation_price()` ↔ `liquidation_distance_pct()` | test identitas di 5 tingkat leverage | identik (< 1e-9) |
| `config/risk_config.json` cocok dengan field `RiskConfig` | `TestConfigFile` | cocok, tanpa kunci liar |
| Sensitivitas rencana terhadap win rate & kegagalan stop | `simulator/monte_carlo.py` | lihat `docs/04` §7 |

**Yang TIDAK diverifikasi di lingkungan ini** (sandbox tanpa akses keluar ke
`api.pionex.com`): nilai `maintMarginRatio` dan `maxLeverage` per simbol, fee tier akun
Anda, dan perilaku akun live. Semua itu diambil saat runtime dari
`GET /api/v1/common/riskTable` dan `GET /uapi/v1/account/positions`, dan guard
`assert_local_liq_matches_exchange()` menolak entry bila estimator lokal tidak cocok
dengan angka bursa. Yang juga belum dijalankan di sini: tumpukan Docker secara nyata dan
CLI MiroFish sungguhan (butuh kredensial LLM) — runner diuji lewat mode `MIROFISH_FAKE`.

## Peringatan

Leverage 50x pada perpetual futures dapat menghapus seluruh margin posisi dalam
pergerakan harga **1.50%** (lihat `docs/04` §2). Rancangan ini membuat kerugian sebesar
itu *terjangkau* dan *terbatas*, bukan *mustahil*. Tidak ada bagian dari rancangan ini
yang merupakan saran investasi.
