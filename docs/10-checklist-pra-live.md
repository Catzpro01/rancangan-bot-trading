# 10 — Checklist Pra-Live

Centang **semuanya**. Checklist ini bukan formalitas: setiap baris di bawah ini
menambal satu cara spesifik untuk kehilangan uang pada leverage 50x.

## A. Kode & verifikasi

- [ ] `make test` lulus: Python, Node, validator struktur, validator kontrak
- [ ] `python tools/make_parity_vectors.py --check` → "sinkron"
- [ ] `python tools/make_n8n_workflows.py` lalu `git diff --exit-code n8n/workflows` → kosong
- [ ] `python tools/validate_workflows.py` → `6 workflow valid.`
- [ ] `python tools/validate_mirofish_contract.py` → `Kontrak antar-lapisan konsisten`
- [ ] CI di GitHub hijau pada commit yang persis sama dengan yang di-deploy

## B. Config risiko (`config/risk_config.json`)

- [ ] `leverage: 50` dan `max_margin_per_trade_pct: 5.0` — keduanya masih ada
- [ ] `max_effective_leverage: 3.0` tidak dinaikkan
- [ ] `liq_buffer_pct: 33` — stop tidak boleh melewati 67% jarak likuidasi
- [ ] `max_daily_loss_pct`, `max_drawdown_pct`, `max_consecutive_losses` diisi, bukan 0
- [ ] `veto_on_negative_ev: true` dan `veto_if_high_event_risk: true`
- [ ] `min_rr_ratio` ≥ 1.1 (di bawah itu EV tipis setelah biaya)
- [ ] Perubahan config di-commit bersama alasannya

## C. Akun Pionex

- [ ] Leverage **50x** disetel manual di aplikasi (bot hanya membacanya)
- [ ] Mode margin **isolated** — bukan cross
- [ ] Mode posisi **one-way (BUYSELL)**
- [ ] Kunci API: **reading + trading saja**, tanpa transfer
- [ ] Kunci API dibatasi ke IP server
- [ ] Dua kunci terpisah: trading untuk bot, reading-only untuk pemantauan
- [ ] Anda sudah menemukan tombol "cabut API key" dan tahu letaknya

## D. Infrastruktur

- [ ] `docker compose ps` → ketiga layanan `healthy`
- [ ] Postgres **tidak** punya port yang dipublikasikan
- [ ] mirofish-runner **tidak** punya port yang dipublikasikan
- [ ] n8n hanya di 127.0.0.1, di belakang reverse proxy + TLS
- [ ] Ada lapisan autentikasi kedua di depan n8n (IP allowlist atau VPN)
- [ ] `deploy/.env` berizin `600` dan tidak masuk git (`git status` bersih)
- [ ] Firewall hanya membuka 22 dan 443
- [ ] `pg_dump` terjadwal **dan restore-nya sudah diuji sekali**
- [ ] `N8N_ENCRYPTION_KEY` disimpan di luar server

## E. Alur trading

- [ ] Preflight (`01`) aktif dan melaporkan `healthy: true` tanpa `problems`
- [ ] Market sweep (`02`) mengisi `market_snapshot` tiap menit
- [ ] Monitor (`04`) aktif **sebelum** trading loop (`03`)
- [ ] Kill switch (`05`) **sudah diuji** dan benar-benar meratakan posisi
- [ ] `mirofish_verdict` terisi, `schema_ok = true`, `verdict_ts` segar
- [ ] `MIROFISH_FAKE=0` (mode fake menghasilkan verdict karangan)
- [ ] Satu order uji: `clientOrderId` tercatat, status `FILLED`, tidak ada duplikat
- [ ] Uji retry: matikan n8n tepat setelah order terkirim, nyalakan lagi → tidak ada
      order ganda

## F. Rekonsiliasi

- [ ] `v_posisi_terbuka` cocok dengan `GET /uapi/v1/account/positions`
- [ ] Tidak ada baris `trade_order` berstatus `SENDING` yang menggantung
- [ ] Tidak ada posisi di bursa tanpa baris di `open_position`

## G. Manusia

- [ ] Anda bisa menyebutkan jarak likuidasi pada 50x tanpa melihat dokumen
      (≈ **1,49%** dari harga entry untuk LONG)
- [ ] Anda tahu stop maksimum yang diizinkan (≈ **1,00%** — 67% dari jarak likuidasi)
- [ ] Anda tahu apa yang terjadi bila n8n mati saat posisi terbuka
      (**stop tidak dikelola** → ratakan manual)
- [ ] Perintah kill switch tersimpan di tempat yang bisa dijangkau dalam 10 detik
- [ ] `docs/09-runbook-insiden.md` sudah dibaca sekali, bukan akan dibaca saat panik
- [ ] Uang di akun adalah uang yang bila habis total tidak mengubah hidup Anda
- [ ] Anda sudah memutuskan **sekarang**, dalam keadaan tenang, pada drawdown berapa
      Anda berhenti permanen (lihat `docs/08` §4)

---

Baris terakhir bagian G adalah yang paling sering dilewati dan yang paling menentukan.
Semua pengaman di atas bekerja otomatis kecuali satu: keputusan untuk berhenti. Itu
harus dibuat sebelum pasar yang membuatnya sulit.
