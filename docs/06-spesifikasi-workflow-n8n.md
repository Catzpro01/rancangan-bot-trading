# 06 — Spesifikasi Workflow n8n

Enam workflow di `n8n/workflows/`. **Jangan disunting langsung** — semuanya dihasilkan
oleh `tools/make_n8n_workflows.py`. Ubah generatornya, jalankan
`python tools/make_n8n_workflows.py`, lalu commit hasilnya. CI menggagalkan build bila
berkas di repo tidak lagi cocok dengan keluaran generator, jadi salinan kode guard di
dalam workflow tidak bisa basi tanpa ketahuan.

## 0. Aturan yang berlaku untuk semua workflow

1. **Rahasia hanya lewat `$env`** — `PIONEX_API_KEY`, `PIONEX_API_SECRET`,
   `PIONEX_SYMBOL`, `PIONEX_BASE_URL`, `MIROFISH_RUNNER_URL`, `MIROFISH_RUNNER_TOKEN`,
   `ALERT_EMAIL`. `tools/validate_workflows.py` menolak workflow yang memuat string
   mirip kunci API, dan `tools/validate_mirofish_contract.py` memastikan setiap `$env`
   yang dipakai terdaftar di `deploy/.env.example`.
2. **Permintaan bertanda tangan dihitung saat itu juga.** Pionex menuntut timestamp
   dalam rentang ±20 detik dan HMAC atas `METHOD + path + query tersortir`. Karena itu
   setiap panggilan bertanda tangan didahului Code node yang menghasilkan
   `{ url, timestamp, signature, body }`, dan HTTP Request node memakai
   `={{ $json.url }}`. URL, query, dan tanda tangan berasal dari satu tempat — kalau
   dihitung terpisah, keduanya bisa menyimpang dan bursa menolak dengan
   *signature mismatch*, kegagalan yang sangat sulit dilacak karena kodenya terlihat
   benar.
3. **Endpoint publik tidak ditandatangani** (`/api/v1/market/*`,
   `/api/v1/common/*`). Menandatanganinya tidak berbahaya, hanya membuang waktu.
4. **Semua keputusan ditulis ke Postgres**, yang disetujui maupun yang ditolak.
   Jejak penolakan adalah alat diagnostik utama.
5. **Kode guard di-embed, bukan di-`require`.** n8n Code node tidak bisa membaca berkas
   di disk, jadi generator menempelkan isi `n8n/code/risk_guard.js` (tanpa blok
   `module.exports`) ke dalam setiap Code node yang membutuhkannya. Konsekuensinya:
   tidak ada salinan yang bisa tertinggal, dan `tests/test_workflow_code_nodes.js`
   menguji kode yang benar-benar ada di dalam JSON.

## 1. `01-preflight-watchdog` — 12 node, tiap 30 detik

Penjaga gerbang. Bot tidak boleh trading kalau setelan akun atau state-nya tidak
beres, dan workflow inilah yang menegakkannya.

| # | Node | Jenis | Tugas |
|---|---|---|---|
| 1 | Setiap 30 detik | schedule | — |
| 2 | Baca config & state | postgres | `cfg`, ekuitas awal hari, puncak, kekalahan beruntun, `halted`, heartbeat |
| 3 | Tanda tangan baca akun | code | 4 permintaan bertanda tangan: `leverage`, `isolatedMode`, `positionMode`, `balances` |
| 4 | Tanda tangan baca akun (HTTP) | http | satu node, empat item (n8n menjalankan HTTP node sekali per item) |
| 5 | Ambil risk table | http | `GET /api/v1/common/riskTable?symbol=…` (publik) |
| 6 | Gabung & hitung breaker | code | `evaluate_breaker`: `HALTED`, `DAILY_LOSS`, `DRAWDOWN`, `CONSEC_LOSSES`, `DAY_START_EQUITY_INVALID` |
| 7 | Periksa setelan akun | code | bandingkan setelan bursa dengan `cfg`; tulis `last_heartbeat` |
| 8 | Sehat? | if | `healthy === true && tripped === false` |
| 9 | Perbarui state | postgres | `peak_equity = GREATEST(peak_equity, equity)` |
| 10 | Tanda tangan cancel-all | code | HMAC untuk `DELETE /uapi/v1/trade/allOrders` |
| 11 | Batal semua order | http | cabut semua order aktif |
| 12 | Notifikasi HALT | email | SMTP |

**Kenapa `cfg` dibaca lebih dulu.** `cfg.leverage` adalah pembanding untuk leverage
yang terbaca dari bursa. Tanpa baris `bot_state`, tidak ada acuan dan seluruh
pemeriksaan tidak berarti — jadi workflow berhenti di node 2. Baris awal dibuat oleh
`db/schema.sql` dengan `halted = true`: bot lahir dalam keadaan berhenti.

**Kenapa `halted` ikut diperiksa breaker.** Kalau tidak, bot yang di-halt manual akan
menyala sendiri begitu n8n di-restart — cara paling mudah untuk kehilangan kendali
tepat setelah memutuskan untuk berhenti.

## 2. `02-market-sweep` — 9 node, tiap 1 menit

Mengumpulkan data pasar, menghitung ATR(14) dari 60 candle 1 menit, menyimpan snapshot
dan heartbeat pasar. ATR inilah yang dipakai monitor untuk trailing stop; kalau tabel
`market_snapshot` basi, monitor menutup posisi (`ANOMALY:MARKET_STALE`).

Endpoint publik: `market/klines`, `market/indexes`, `market/depth`,
`common/symbols`, `common/riskTable`. **Tidak ada satu pun angka aturan bursa yang
dihardcode** — `minNotional`, `baseStep`, `maintMarginRatio`, dan `maxLeverage` selalu
dibaca dari sini, karena angka itu bisa berubah kapan saja tanpa pemberitahuan.

## 3. `03-trading-loop` — 16 node, tiap 1 menit

Satu-satunya jalur menuju order.

| Tahap | Node |
|---|---|
| Baca | `Baca config & state`, `Baca verdict MiroFish`, `Baca posisi terbuka`, `Baca snapshot pasar` |
| Sinyal | `Bangun sinyal` (EMA 9/21 + ATR; **ganti dengan strategi Anda sendiri** — yang tidak boleh berubah adalah bentuk keluarannya, karena gerbang memvalidasi bentuk itu) |
| **Gerbang** | `GERBANG RISIKO (default DENY)` — salinan `risk_guard.js` |
| Cabang | `Disetujui?` |
| Tolak | `Catat penolakan` → `trade_decision(approved=false, reasons=…)` |
| Terima | `Bangun order + tanda tangan` → `Catat order (SENDING)` → `Kirim order` → `Catat keputusan disetujui` → `Tulis heartbeat loop` → `Tanda tangan cek idempotensi` → `Cek idempotensi` |

**Urutan `Catat order (SENDING)` sebelum `Kirim order` itu disengaja.** Baris dengan
`client_order_id` sebagai PRIMARY KEY ditulis lebih dulu dengan
`ON CONFLICT DO NOTHING`. Kalau n8n retry setelah timeout, barisnya tidak ganda — dan
`clientOrderId` yang sama membuat Pionex menolak duplikatnya. Timeout **bukan**
kegagalan: order bisa jadi sudah masuk. Karena itu `Cek idempotensi` memanggil
`GET /uapi/v1/trade/orderByClientOrderId` dan tidak pernah mengirim ulang secara
membabi buta.

## 4. `04-position-monitor` — 10 node, tiap 15 detik

Pionex tidak punya order stop bawaan untuk futures, jadi stop dikelola sendiri di sisi
klien. Itulah sebabnya rancangan ini menahan 33% jarak likuidasi sebagai buffer dan
membatasi margin 5%: stop klien bisa terlambat, dan keduanya adalah pengaman untuk
keterlambatan itu.

`Hitung trailing & exit` menghasilkan salah satu `STOP_LOSS`, `TAKE_PROFIT`,
`TIME_STOP`, `ANOMALY:*`, atau `HOLD`. Trailing stop **hanya bergerak ke arah yang
menguntungkan** (ratchet): untuk LONG kandidat stop baru hanya dipakai bila lebih
tinggi. Anomali — leverage tidak cocok, mode bukan isolated, simbol di luar whitelist —
selalu menghasilkan exit, bukan peringatan.

## 5. `05-kill-switch` — 10 node, webhook

`POST /webhook/pionex-guard/kill` → cancel-all → baca posisi → ratakan satu per satu
(`reduceOnly: true`) → `bot_state.halted = true` → email.

Urutannya penting: **batalkan order dulu, baru ratakan posisi.** Kalau dibalik, order
limit yang masih aktif bisa terisi setelah posisi diratakan dan membuka posisi baru
tanpa pengawasan.

## 6. `06-mirofish-sweep` — 12 node, 4 jam + poll 2 menit

Dua pemicu terpisah. Yang 4 jam mengirimkan job ke `mirofish_runner`; yang 2 menit
memeriksa job yang sedang berjalan dan menyimpan verdictnya.

`Adapter verdict` menormalkan verdict mentah menjadi amplop standar. Aturan tunggalnya:
**gagal = veto**. Verdict tanpa `confidence`, tanpa `prediction` yang cukup panjang,
atau tanpa timestamp menghasilkan `schema_ok: false` dan `event_risk: "HIGH"` — bot
menahan diri, bukan menebak. Amplop itu ditulis ke `mirofish_verdict` dengan
`ON CONFLICT (run_id) DO NOTHING`, dan `03-trading-loop` membaca baris terbarunya.

## 7. Memasukkan ke n8n

1. `docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d`
2. Buka n8n → **Workflows → Import from File** → pilih berkas JSON.
3. Isi kredensial Postgres dan SMTP yang masih bertanda `REPLACE_ME`.
4. Pastikan variabel `$env` tersedia untuk proses n8n (di compose sudah diteruskan).
5. Aktifkan **hanya setelah** `tools/validate_workflows.py` dan
   `tools/validate_mirofish_contract.py` lulus, dan checklist `docs/10` tercentang.

Aktifkan satu per satu, mulai dari `01`. Jangan menyalakan `03` sebelum `01`, `02`,
dan `04` terbukti berjalan: `03` mengirim uang, tiga lainnya yang menjaganya.
