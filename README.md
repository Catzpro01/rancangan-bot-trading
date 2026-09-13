# Rancangan Bot Trading — "Pionex Guard 50x" (n8n + MiroFish + Pionex)

Rancangan lengkap bot trading perpetual futures di Pionex dengan **leverage bursa 50x**,
diatur oleh **n8n**, dengan **MiroFish** sebagai lapisan penilaian risiko peristiwa.

> **Baca `docs/01-ringkasan-dan-ancaman.md` lebih dulu.** Rancangan ini menyertakan temuan
> kuantitatif yang mengubah cara leverage 50x seharusnya dipakai — bukan sekadar
> "pasang 50x lalu berdoa".

---

## Isi repositori

| Bagian | Isi |
|---|---|
| `docs/01` | Ringkasan eksekutif + model ancaman + **temuan angka** |
| `docs/02` | Arsitektur & alur data |
| `docs/03` | Integrasi MiroFish (kontrak sinyal, adapter, batasannya) |
| `docs/04` | **Kerangka risiko & guardrail** (inti rancangan) |
| `docs/05` | Peta endpoint Pionex + penandatanganan + idempotensi |
| `docs/06` | Spesifikasi workflow n8n (5 workflow) |
| `docs/07` | Deployment, rahasia, hardening, backup |
| `docs/08` | Rencana rollout & validasi (paper → live bertahap) |
| `docs/09` | Runbook insiden + kill switch |
| `docs/10` | Checklist pra-live |
| `risk_engine/` | Kernel risiko (Python, tanpa dependensi) — **69 unit test** |
| `tests/` | Unit test kernel risiko |
| `simulator/` | Monte Carlo untuk menguji rencana sebelum uang dipakai |
| `n8n/` | 4 workflow n8n siap impor + 3 Code node |
| `mirofish_runner/` | Job-runner HTTP kecil yang membungkus CLI MiroFish |
| `config/` | `risk_config.json` — satu-satunya tempat angka risiko diubah |
| `db/schema.sql` | Skema Postgres untuk audit trail |
| `tools/` | Validator workflow n8n |

## Mulai dari sini

```bash
# 1. jalankan seluruh gerbang risiko (harus hijau sebelum apa pun)
python -m pytest -q

# 2. lihat angka risiko rencana Anda sendiri
python simulator/monte_carlo.py --equity 1000 --leverage 50 \
       --win-rate 0.50 --stop-pct 0.008 --tp-pct 0.010 --liq-prob 0.30

# 3. validasi berkas workflow n8n
python tools/validate_workflows.py

# 4. naikkan stack lokal
docker compose up -d        # lihat docs/07
```

## Status verifikasi

| Yang diverifikasi | Bagaimana | Hasil |
|---|---|---|
| Matematika likuidasi, sizing, EV, circuit breaker | `python -m pytest -q` | **69 passed** |
| Konsistensi `liquidation_price()` ↔ `liquidation_distance_pct()` | test identitas di 5 tingkat leverage | identik (< 1e-9) |
| `config/risk_config.json` cocok dengan field `RiskConfig` | `TestConfigFile` | cocok, tanpa kunci liar |
| Sensitivitas rencana terhadap win rate & kegagalan stop | `simulator/monte_carlo.py` | lihat `docs/04` §7 |
| Struktur JSON workflow n8n | `tools/validate_workflows.py` | lihat `docs/06` |

**Yang TIDAK diverifikasi di lingkungan ini** (sandbox tanpa akses keluar ke
`api.pionex.com`): nilai `maintMarginRatio` dan `maxLeverage` per simbol, fee tier akun
Anda, dan perilaku akun live. Semua itu diambil saat runtime dari
`GET /api/v1/common/riskTable` dan `GET /uapi/v1/account/positions`, dan guard
`assert_local_liq_matches_exchange()` menolak entry bila estimator lokal tidak cocok
dengan angka bursa.

## Peringatan

Leverage 50x pada perpetual futures dapat menghapus seluruh margin posisi dalam
pergerakan harga **1.50%** (lihat `docs/04` §2). Rancangan ini membuat kerugian sebesar
itu *terjangkau* dan *terbatas*, bukan *mustahil*. Tidak ada bagian dari rancangan ini
yang merupakan saran investasi.
