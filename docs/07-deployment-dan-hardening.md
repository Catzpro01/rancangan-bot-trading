# 07 — Deployment & Hardening

Dokumen ini mengasumsikan satu VPS Linux (Ubuntu 22.04/24.04) dengan Docker. Semua
perintah dijalankan sebagai pengguna non-root.

## 1. Bentuk tumpukan

```
                 internet
                    │  (hanya 443, lewat reverse proxy + TLS)
                    ▼
        ┌───────────────────────┐
        │  reverse proxy (TLS)  │
        └──────────┬────────────┘
                   ▼
   ┌──────────────────────────────┐   jaringan internal Docker saja
   │  n8n  :5678 (127.0.0.1)      │◄──────────────┐
   └───────┬──────────────┬───────┘               │
           │              │                       │
           ▼              ▼                       │
   ┌──────────────┐  ┌────────────────────┐       │
   │ Postgres 16  │  │ mirofish-runner    │───────┘
   │ (tidak dibuka)│  │ :8100 (tidak dibuka)│
   └──────────────┘  └────────────────────┘
                             │
                             ▼
                     artefak run MiroFish
```

Tiga hal yang tidak boleh berubah dari gambar ini: **Postgres tidak punya port yang
dipublikasikan**, **mirofish-runner tidak punya port yang dipublikasikan**, dan
**n8n hanya terikat ke 127.0.0.1**.

## 2. Persiapan host

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin ufw fail2ban
sudo usermod -aG docker "$USER"          # lalu login ulang
sudo ufw default deny incoming
sudo ufw allow 22/tcp                    # lebih baik: pindah ke port lain + kunci kunci-SSH
sudo ufw allow 443/tcp
sudo ufw enable
```

Jangan pernah `ufw allow 5678` atau `5432`. Kalau perlu akses n8n dari luar, pakai
WireGuard/Tailscale, bukan port terbuka.

## 3. Reverse proxy + TLS

Contoh Caddy (paling sedikit baris):

```
n8n.example.com {
    reverse_proxy 127.0.0.1:5678
}
```

Atau nginx + certbot. Yang penting: TLS wajib, dan **tambahkan lapisan autentikasi
kedua**. n8n punya basic auth (`N8N_BASIC_AUTH_*`), tetapi itu satu-satunya lapisan
antara internet dan kunci API Pionex Anda — pasang juga IP allowlist atau VPN.

## 4. Kunci API Pionex — bagian paling penting di dokumen ini

Di aplikasi Pionex: **API Management → Create API Key**.

| Izin | Diaktifkan? | Alasan |
|---|---|---|
| Reading | **Ya** | saldo, posisi, leverage |
| Trading | **Ya** | kirim order |
| Transfer / Withdraw | **TIDAK PERNAH** | kalau kunci bocor, kerugian maksimal adalah posisi yang buruk — bukan saldo yang ditarik keluar |

- **Batasi ke IP server** bila tersedia. Ini mengubah kunci yang bocor menjadi kunci
  yang tidak berguna.
- Simpan kunci di `.env` dengan izin `600`, bukan di riwayat shell atau di chat.
- Putar kunci setiap 90 hari, dan segera setelah ada orang lain yang punya akses ke
  server.
- Buat **dua** kunci: satu trading (dipakai bot), satu reading-only (dipakai Anda untuk
  memantau). Kalau kunci trading harus dicabut, pemantauan tetap jalan.

Kunci disimpan di `deploy/.env`, yang **tidak** di-commit (`.gitignore` sudah
mengecualikannya). `tools/validate_workflows.py` menggagalkan CI bila ada string mirip
kunci API masuk ke berkas workflow.

Setelan akun yang dibaca preflight — leverage 50x, margin **isolated**, mode posisi
**one-way (BUYSELL)** — diatur **manual sekali** di aplikasi. Bot hanya membacanya dan
menolak berjalan bila tidak cocok. Ini disengaja: keputusan sebesar itu tidak boleh
diubah oleh kode.

## 5. Menjalankan tumpukan

```bash
cd deploy
cp .env.example .env && chmod 600 .env
# isi semua nilai; jangan tinggalkan yang kosong
docker compose --env-file .env -f docker-compose.yml up -d --build
docker compose ps          # ketiga layanan harus "healthy"
```

Verifikasi:

```bash
psql "$DATABASE_URL" -c "SELECT * FROM bot_state;"        # satu baris, halted=true
curl -s localhost:5678/healthz                            # n8n hidup
docker compose exec mirofish-runner python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8100/health').read())"
```

**mirofish-runner dan CLI MiroFish.** `deploy/Dockerfile.mirofish-runner` hanya
menjalankan lapisan HTTP-nya. Untuk produksi, bangun image di atas image MiroFish yang
sudah berisi CLI beserta dependensinya (`camel-ai`/OASIS itu berat dan pin versinya
ketat). Selama CLI belum terpasang, setel `MIROFISH_FAKE=1` — runner menghasilkan
artefak berbentuk benar sehingga seluruh rantai (runner → adapter → `mirofish_verdict`
→ gerbang) bisa diuji ujung ke ujung. **Jangan** menjalankan `MIROFISH_FAKE=1` dengan
uang sungguhan: verdictnya karangan.

## 6. Backup

Yang tidak bisa direkonstruksi dari bursa adalah **riwayat keputusan** Anda:

```bash
pg_dump "$DATABASE_URL" | gzip > backup-$(date +%F).sql.gz
```

Jadwalkan harian, simpan di luar server, dan **uji restore**-nya sekali. Backup yang
belum pernah di-restore bukan backup.

Volume n8n (`n8n_data`) menyimpan kredensial terenkripsi dan riwayat eksekusi;
`N8N_ENCRYPTION_KEY` adalah kunci untuk membukanya. Simpan kunci itu di luar server —
tanpa dia, backup volume n8n tidak bisa dipakai.

## 7. Pemantauan minimum

Tanpa ini Anda baru tahu ada masalah dari saldo, bukan dari peringatan.

| Yang dipantau | Ambang | Aksi |
|---|---|---|
| `bot_state.last_heartbeat` | > 90 detik | preflight HALT otomatis |
| `bot_state.last_market_beat` | > 3 menit | data pasar basi |
| `mirofish_verdict.verdict_ts` | > 6 jam | gerbang menolak semua (`MIROFISH_STALE`) |
| `bot_state.halted` | `true` | email sudah dikirim; periksa `halt_reason` |
| Tingkat penolakan (`v_ringkasan_harian`) | melonjak tiba-tiba | biasanya ada gerbang yang mati, bukan pasar yang berubah |
| Disk, memori, `docker compose ps` | — | kegagalan infrastruktur |

`db/schema.sql` menyediakan pandangan `v_posisi_terbuka` dan `v_ringkasan_harian` untuk
ini.

## 8. Yang sengaja tidak ada di rancangan ini

- **Tidak ada testnet futures Pionex.** Karena itu fase paper trading di `docs/08`
  memakai ekuitas simulasi, bukan lingkungan bursa.
- **Tidak ada penarikan/transfer otomatis.** Tidak ada kode di repo ini yang menyentuh
  izin transfer, dan kuncinya pun tidak boleh punya izin itu.
- **Tidak ada penskalaan otomatis (auto-scale).** Leverage dan ukuran dibatasi config,
  bukan oleh performa terakhir.
- **Tidak ada UI kustom.** n8n sudah punya riwayat eksekusi; membuat UI sendiri
  menambah permukaan kegagalan tanpa menambah keamanan.
