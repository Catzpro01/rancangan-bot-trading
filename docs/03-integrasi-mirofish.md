# 03 — Integrasi MiroFish

## 1. Apa itu MiroFish, dan apa batasannya

MiroFish adalah mesin prediksi multi-agent: Anda memasukkan materi seed (berita, laporan,
dokumen), mendeskripsikan apa yang ingin diprediksi dalam bahasa alami, lalu sistem
membangun knowledge graph (GraphRAG), membuat ratusan–ribuan persona agen, menjalankan
simulasi interaksi sosial berbasis OASIS (dari CAMEL-AI), dan menghasilkan laporan.

Dua varian yang relevan:

| Varian | Antarmuka | Output mesin | LLM |
|---|---|---|---|
| `666ghj/MiroFish` (upstream) | Web: Vue di :3000 + Flask di :5001, rute `/api/graph`, `/api/simulation`, `/api/report`, `/health` | Laporan Markdown kualitatif | API OpenAI-compatible (mis. Qwen) + Zep Cloud |
| `amadad/mirofish-cli` (fork) | CLI headless: `mirofish run`, `mirofish runs list/status/export` | `uploads/runs/<run_id>/report/verdict.json` + `summary.json` + `report.md` | **hanya** `claude-cli` atau `codex-cli`; nilai lain ditolak saat startup |

**Rancangan ini memakai fork CLI**, dibungkus job-runner HTTP kecil
(`mirofish_runner/`), karena headless lebih mudah dijadwalkan n8n dan artefaknya
immutable per run.

### Batasan yang wajib dibaca sebelum menulis satu baris kode pun

Laporan upstream MiroFish sendiri (bagian FAQ, isu #725) menyatakan secara eksplisit
bahwa **ReportAgent menghasilkan laporan Markdown kualitatif dan tidak menjamin kontrak
`verdict.json`, `miro_signal`, `p_yes`, `confidence`, atau `action` yang stabil maupun
confidence yang terkalibrasi untuk perdagangan otomatis.**

Fork CLI memang menambah `verdict.json` (isinya: `prediction`, `confidence` 0–1,
`key_dynamics`, array `signals`) — tetapi:

1. Skemanya **bukan kontrak publik**. Ia bisa berubah antar rilis.
2. `confidence`-nya adalah keluaran LLM, **bukan** probabilitas terkalibrasi. Angka 0.8
   tidak berarti "80% kemungkinan benar".
3. MiroFish mensimulasikan **reaksi sosial agen terhadap materi yang Anda beri**. Ia
   tidak melihat order book, tidak melihat funding, tidak tahu harga besok.

### Karena itu, peran MiroFish di rancangan ini dibatasi

| Boleh | Tidak boleh |
|---|---|
| Menjawab "apakah ada peristiwa dalam 24 jam ke depan yang membuat trade ini berbahaya?" | Menjawab "beli atau jual sekarang" |
| Memberi **veto keras** saat `event_risk = HIGH` | Memberi arah posisi satu-satunya |
| Menandai bias sentimen sebagai *filter tambahan* | Menentukan ukuran posisi |
| Menjadi alasan untuk **tidak** trading | Menjadi alasan untuk melewati gerbang lain |

Arah posisi tetap harus datang dari aturan teknikal yang bisa di-backtest. MiroFish
hanya bisa mengurangi jumlah trade, tidak pernah menambahnya.

## 2. Job-runner

`mirofish_runner/` adalah FastAPI tipis dengan tiga endpoint:

| Endpoint | Fungsi |
|---|---|
| `POST /jobs` | Body: `{files: [...], requirement, platform, max_rounds}` → mulai simulasi di background, balas `{job_id}` |
| `GET /jobs/{id}` | Status: `PENDING\|RUNNING\|COMPLETED\|FAILED`, plus `run_id` bila selesai |
| `GET /jobs/{id}/verdict` | Isi `verdict.json` mentah + `summary.json` |
| `GET /health` | Untuk watchdog |

Runner **tidak** menafsirkan verdict. Penafsiran ada di adapter (n8n Code node
`mirofish_adapter.js`) supaya logika penafsiran ikut terevisi bersama workflow.

Alasan runner dibutuhkan: CLI MiroFish adalah proses panjang (menit–puluhan menit) yang
men-spawn subprocess OASIS. n8n tidak cocok menahan request selama itu; pola
submit-then-poll lebih tahan terhadap restart n8n.

## 3. Adapter: dari verdict mentah ke envelope

`n8n/code/mirofish_adapter.js` melakukan:

1. **Validasi skema.** Wajib ada `prediction` (string) dan `confidence` (number 0–1).
   Bila tidak → `schema_ok=false`.
2. **Normalisasi arah.** Cari kata kunci arah pada `prediction`/`signals`
   (`bullish/bearish/long/short/rally/selloff/...`) → `bias ∈ {LONG, SHORT, NEUTRAL}`.
   Bila ambigu → `NEUTRAL` (bukan menebak).
3. **Klasifikasi risiko peristiwa.** Hitung dari `key_dynamics` + `signals`: sebutan
   tentang likuidasi, intervensi regulator, hack, depeg, ETF decision, FOMC, CPI,
   expiry options besar → skor. `≥ 0.66 → HIGH`, `≥ 0.33 → MEDIUM`, selain itu `LOW`.
4. **Penetapan umur.** `verdict_ts` diambil dari manifest run (`created_at`), bukan dari
   jam server n8n.
5. **Pengawetan bukti.** `run_id` + path artefak disimpan supaya setiap keputusan bisa
   diaudit kembali ke simulasi yang menghasilkannya.

Envelope keluaran (kontrak yang dibaca `validate_signal`):

```json
{
  "run_id": "20260913-114500-a91f",
  "verdict_ts": "2026-09-13T11:47:12Z",
  "schema_ok": true,
  "bias": "NEUTRAL",
  "confidence": 0.72,
  "event_risk": "LOW",
  "horizon_hours": 24,
  "evidence": ["..."],
  "adapter_version": "1.0.0"
}
```

### Aturan "gagal = veto"

| Kondisi | Hasil |
|---|---|
| Runner tidak bisa dihubungi | verdict lama jadi basi → `MIROFISH_STALE` → tidak trade |
| `verdict.json` tidak ada / rusak | `schema_ok=false` → `MIROFISH_SCHEMA_INVALID` |
| `confidence` di bawah 0.60 | `MIROFISH_LOW_CONFIDENCE` |
| `event_risk = HIGH` | `MIROFISH_HIGH_EVENT_RISK_VETO` (keras, tak bisa di-override config) |
| `bias` berlawanan dengan arah sinyal | `MIROFISH_BIAS_CONFLICT` |
| `bias = NEUTRAL` | **boleh** trade (netral bukan larangan) |

Perhatikan baris terakhir: MiroFish yang netral tidak menghalangi trade. Yang
menghalangi adalah MiroFish yang *tidak ada*, *basi*, *rusak*, atau *melihat bahaya*.

## 4. Materi seed dan requirement

Kualitas simulasi dibatasi oleh kualitas seed. Template yang dipakai rancangan ini:

**Seed (disusun otomatis tiap sweep):**
- Ringkasan 24 jam berita untuk aset yang diperdagangkan (sumber Anda sendiri; adapter
  tidak mengambil berita sendiri agar tidak menambah dependensi tak teraudit).
- Kalender ekonomi 7 hari ke depan (FOMC, CPI, NFP, keputusan ETF).
- Snapshot posisi & funding saat ini.

**Requirement (tetap, jangan diubah-ubah per trade):**

```
Dalam {horizon} jam ke depan, untuk aset {symbol}: peristiwa apa yang paling mungkin
mengguncang harga secara tiba-tiba? Nilai tingkat risiko peristiwa sebagai LOW,
MEDIUM, atau HIGH, dan sebutkan bukti serta pemicu waktu spesifiknya.
```

Requirement dibuat tetap supaya verdict antar waktu bisa dibandingkan. Mengubah
requirement per trade membuat hasilnya tidak bisa diaudit.

## 5. Jadwal dan biaya

| Parameter | Nilai | Alasan |
|---|---|---|
| Frekuensi sweep | tiap 4 jam + trigger berita besar | menyeimbangkan kesegaran dan biaya token |
| `max_rounds` | 10 (default CLI) | mulai kecil; dokumentasi MiroFish menyarankan mencoba < 40 ronde |
| `platform` | `parallel` | Twitter + Reddit sekaligus |
| Masa berlaku verdict | 6 jam | sedikit lebih panjang dari periode sweep, agar satu sweep gagal tidak langsung mematikan bot |

Biaya LLM adalah biaya nyata dan berulang. Catat token per run di `mirofish_run_log`;
bila biaya per sinyal yang *berguna* (yang benar-benar memveto sesuatu) terlalu tinggi,
turunkan frekuensi, jangan turunkan kualitas seed.

## 6. Yang harus diuji sebelum mengandalkan MiroFish

1. **Uji skema.** Jalankan 3–5 simulasi, simpan `verdict.json`-nya, dan jalankan adapter
   terhadap berkas-berkas itu. Pastikan `schema_ok=true` dan `event_risk` masuk akal.
   Bila skema fork berubah, adapter gagal dengan `schema_ok=false` — bukan dengan
   interpretasi salah. Itu perilaku yang diinginkan.
2. **Uji veto.** Masukkan seed tentang peristiwa berisiko tinggi yang sudah terjadi
   (misalnya berita hack besar). Verifikasi `event_risk=HIGH` dan bot menolak trade.
3. **Uji staleness.** Matikan runner, tunggu 6 jam (atau set sementara
   `max_verdict_age_sec=60`), dan pastikan bot berhenti trading dengan alasan
   `MIROFISH_STALE`.
4. **Uji netral.** Pastikan verdict netral **tidak** menghalangi trade yang lolos semua
   gerbang lain — kalau iya, adapter terlalu agresif.

Keempat uji ini ada di `docs/10` sebagai checklist wajib.

## 7. Alternatif bila MiroFish terlalu mahal/lambat

Karena perannya hanya veto, ia bisa diganti tanpa mengubah arsitektur: apa pun yang
menghasilkan envelope yang sama (`schema_ok`, `bias`, `confidence`, `event_risk`,
`verdict_ts`) bisa menempati posisi itu — kalender ekonomi sederhana, feed berita dengan
klasifikasi kata kunci, atau bahkan sumber manual. Yang tidak boleh berubah adalah
kontrak envelope dan aturan "gagal = veto".

---

## Catatan: dua jalur yang tersedia

Rancangan ini sekarang menyediakan **dua** cara mengisi `mirofish_verdict`, dan keduanya
boleh dipakai bergantian karena bentuk keluarannya sama:

| | Jalur A — MiroFish asli | Jalur B — simulasi agen di n8n |
|---|---|---|
| Workflow | `06-mirofish-sweep` | `07` + `08` (+ `09` penjaga cache) |
| Mesin | CLI MiroFish (OASIS/CAMEL-AI) lewat `mirofish_runner` | HTTP Request ke LLM, seluruhnya di n8n |
| Komponen tambahan | satu layanan Python + dependensi berat | tidak ada |
| Sumber informasi | dokumen/berkas yang Anda unggah | cache pasar: GDELT, Fear & Greed, stablecoin, funding |
| Status | opsional | **default** |

Yang **tidak berubah** di kedua jalur: peran MiroFish tetap **veto**, adapter tetap
berlaku "gagal = veto", dan tabel `mirofish_verdict` tetap sama. Lihat
`docs/11-sumber-data-pasar.md` untuk sumber data jalur B beserta bukti verifikasinya.

Jujur soal jalur B: ia **terinspirasi** MiroFish (banyak agen, peran berbeda, beberapa
sudut pandang, satu kesimpulan), tetapi bukan MiroFish. Ia tidak memakai OASIS, tidak
membangun graf sosial, dan tidak mensimulasikan penyebaran informasi antar-agen. Yang
ia lakukan adalah meminta lima peran berbeda menilai data yang sama lalu menyimpulkan —
struktur yang sama, mekanisme yang jauh lebih sederhana.
