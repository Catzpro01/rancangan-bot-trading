# 11 — Sumber Data Pasar: yang Terverifikasi dan yang Tidak

Dokumen ini menjawab satu pertanyaan: **dari mana bot mendapat informasi, dan seberapa
bisa informasi itu dipercaya.**

Tanggal verifikasi: **13 September 2026**. Setiap baris bertanda ✅ di bawah ini saya
panggil langsung saat menulis dokumen ini dan mencatat jawabannya. Sumber yang bisa
berubah tanpa pemberitahuan — dan semuanya bisa — jadi periksa ulang sebelum live.

## 1. Jawaban jujur untuk "setara Bloomberg"

**Tidak ada yang gratis dan setara Bloomberg.** Bloomberg Terminal berharga sekitar
$24.000 per kursi per tahun, dan yang Anda bayar bukan kelengkapan datanya melainkan
**kecepatannya**: berita masuk ke terminal sebelum harga bergerak.

Sumber gratis memberi *keluasan*, bukan *kecepatan*. Untuk rancangan ini itu cukup,
karena peran informasi di sini adalah **rem** (menahan bot saat ada peristiwa
berbahaya), bukan **keunggulan** (masuk lebih dulu daripada orang lain). Kalau Anda
mengharapkan yang kedua, tidak ada sumber gratis yang bisa memberikannya dan saya
tidak akan berpura-pura ada.

## 2. Sumber yang dipakai, dan bukti verifikasinya

| Sumber | Endpoint | Biaya | Verifikasi |
|---|---|---|---|
| **GDELT DOC 2.0** | `api.gdeltproject.org/api/v2/doc/doc` | Gratis, **tanpa kunci API** | ✅ `mode=TimelineTone` mengembalikan deret nada **per jam** sampai `20260913T090000Z` (hari ini); `mode=ArtList&timespan=1d` mengembalikan 3 artikel nyata bertanggal `20260912T220000Z` |
| **Fear & Greed Index** | `api.alternative.me/fng/?limit=14` | Gratis, tanpa kunci | ✅ Mengembalikan `value: "61", value_classification: "Greed"` beserta stempel waktu |
| **Aliran stablecoin** | `stablecoins.llama.fi/stablecoincharts/all?stablecoin=1` | Gratis, tanpa kunci | ✅ Mengembalikan deret harian USDT (`circulating`, `circulatingPrevDay`, `circulatingPrevWeek`) |
| **Funding & indeks** | `api.pionex.com/api/v1/market/indexes` | Gratis (publik) | ✅ Endpoint yang sama sudah dipakai workflow `02` |

Mode GDELT yang dipakai workflow `07`:

- `mode=TimelineTone` — rata-rata nada pemberitaan per hari (negatif/positif)
- `mode=TimelineVol` — jumlah artikel per hari, untuk mendeteksi **lonjakan** pemberitaan
- `mode=ArtList` + `tone<-5` — daftar artikel paling negatif 2 hari terakhir

## 3. Batasan tiap sumber — baca sebelum percaya

**GDELT** — tiga jebakan yang saya temukan dengan memanggilnya langsung:

- **Query dengan `OR` wajib dibungkus tanda kurung.** Tanpa kurung, GDELT membalas
  `Queries containing OR'd terms must be surrounded by ().` — dan membalasnya dengan
  **HTTP 200**, bukan kode kesalahan. Query yang benar:
  `?query=%28bitcoin%20OR%20crypto%29&mode=TimelineTone&format=json&timespan=7d`.
  Bentuk tanpa kurung adalah bug yang *permanen*, bukan sesekali.
- **Teguran batas laju juga dikirim sebagai HTTP 200 + teks polos**, bukan HTTP 429:
  `Please limit requests to one every 5 seconds...`. Node HTTP n8n akan menganggapnya
  sukses, lalu teks itu masuk ke cache dan dikirim ke agen LLM sebagai "data nada
  berita". Karena itu workflow `07` memasang `Wait` 6 detik antar panggilan GDELT
  **dan** satu Code node penjaga yang menolak respons yang tidak punya `timeline`
  atau `articles`.
- Dibatasi **1 permintaan per 5 detik**. Tiga panggilan sekaligus berarti dua di
  antaranya pasti gagal.
- Jendela data **3 bulan bergulir**. Tidak bisa untuk riset historis panjang.
- **Tidak memberi isi artikel** — hanya judul, URL, domain, negara, bahasa. Jadi
  "intelijen" yang masuk ke simulasi agen adalah *judul dan nada*, bukan analisis
  mendalam. Ini batasan yang paling penting untuk disadari.
- Nada (tone) dihitung otomatis oleh GDELT, bukan oleh manusia. Untuk berita kripto
  yang penuh ironi dan slang, akurasinya terbatas.

**Fear & Greed**
- Diperbarui **sekali sehari**. Untuk bot yang memutuskan tiap menit, ini indikator
  rezim, bukan sinyal.
- Rumusnya campuran volatilitas, momentum, dominasi BTC, dan sentimen sosial — bukan
  pengukuran ketakutan yang sebenarnya.

**Aliran stablecoin**
- Proksi, bukan pengukuran langsung. Pasokan USDT naik *biasa* berarti uang masuk ke
  kripto, tapi bisa juga berarti penerbitan untuk keperluan lain.
- Payload mentahnya ratusan KB. Workflow `07` hanya mengambil **30 titik terakhir**;
  menelan seluruhnya akan memboroskan cache dan token LLM.

## 4. Yang saya periksa dan TIDAK saya rekomendasikan

**`nirholas/free-crypto-news` (cryptocurrency.cv)** — README GitHub-nya menulis
*"No API key required, no rate-limit paperwork, free"*. Saya buka `llms.txt` resminya
dan isinya berbeda:

> "All API calls require x402 micropayment. Prices: $0.001–$0.20 per request."
> (USDC di Arbitrum)

Artinya setiap panggilan berbayar lewat micropayment kripto. Murah, tapi **tidak
gratis**, dan butuh dompet USDC — sesuatu yang tidak bisa dilakukan node n8n tanpa
komponen tambahan. Saya tidak memasukkannya ke rancangan. Kalau Anda tetap mau
memakainya, perlakukan klaim di README sebagai tidak akurat dan baca `llms.txt`-nya.

**CryptoPanic RSS** — URL yang saya coba (`cryptopanic.com/news/rss/`) mengembalikan
halaman HTML, bukan feed XML. Belum terverifikasi; tidak saya pakai.

## 5. Sumber yang layak dipertimbangkan bila Anda punya kunci API

Belum saya pasang, karena semuanya butuh pendaftaran:

| Sumber | Untuk apa | Catatan |
|---|---|---|
| **FRED** (`api.stlouisfed.org`) | Data makro AS: CPI, suku bunga, M2 | Gratis dengan kunci API. Perlu `docs/07` diperbarui untuk menyimpan kuncinya |
| **Finnhub** | Berita perusahaan + sentimen | Gratis 60 panggilan/menit |
| **Marketaux** | Berita keuangan dengan skor sentimen | Ada tingkat gratis |
| **CryptoPanic API** | Agregasi berita kripto + PanicScore | Berbayar; tingkat gratis terbatas |
| **Glassnode / CryptoQuant** | Data on-chain | Tingkat gratis sangat terbatas |

Menambah sumber baru tidak memerlukan perubahan arsitektur: tambahkan satu cabang
`HTTP Request → Edit Fields → Redis Set` di workflow `07`, lalu daftarkan field-nya di
node `Gabung semua sumber`. Adapter dan gerbang risiko tidak perlu disentuh.

## 6. Kenapa semua ini masuk cache, bukan dibaca langsung

Workflow `08` (simulasi agen) membaca dari cache KV, bukan memanggil sumber-sumber itu
sendiri. Tiga alasan:

1. **Batas laju.** GDELT menolak lebih dari 1 permintaan per 5 detik. Kalau simulasi
   dipanggil ulang karena retry, ia tidak akan menghantam GDELT lagi.
2. **Sumber mati tidak menghentikan bot.** Bila GDELT down, cache masih berisi data
   15 menit terakhir dan workflow `09` mengirim peringatan. Bot tetap punya rem, hanya
   rem yang sedikit lebih tua.
3. **Biaya LLM terkendali.** Konteks yang dikirim ke agen berasal dari satu dokumen
   cache yang ukurannya sudah dibatasi, bukan dari respons API yang besarnya berubah-ubah.

Konsekuensinya: **cache yang basi lebih berbahaya daripada cache yang kosong**, karena
yang kosong jelas-jelas ditolak sedangkan yang basi terlihat sah. Karena itu setiap
kunci punya TTL (15 menit–2 jam) dan workflow `09` memeriksa umur dokumen induk tiap
10 menit.

## 7. Apa yang TIDAK boleh disimpulkan dari data ini

- GDELT + Fear & Greed **tidak memberi keunggulan waktu**. Kalau sebuah berita sudah
  masuk GDELT, harga kemungkinan besar sudah bergerak.
- Simulasi agen di workflow `08` menyimpulkan **dari data di atas**, jadi kualitasnya
  dibatasi oleh kualitas data itu. Ia tidak bisa "tahu" hal yang tidak ada di cache.
- Confidence yang dihasilkan simulasi adalah **opini model bahasa**, bukan probabilitas
  terukur. Karena itu perannya tetap veto, dan `docs/04` membatasi confidence maksimum
  yang bisa memengaruhi keputusan.

## 8. Menguji rantai LLM tanpa LLM

`tools/mock_llm_server.py` adalah server tiruan yang meniru `POST /chat/completions`
ala OpenAI. Ia tidak meniru kecerdasan model bahasa — itu tidak mungkin dan tidak
perlu — melainkan **bentuk percakapannya**, sehingga kode yang mengirim permintaan dan
mem-parse jawaban bisa diuji apa adanya: 401 tanpa Bearer, 400 untuk badan rusak, dan
`choices[0].message.content` berupa JSON bila prompt menuntutnya.

`tests/test_simulation_integration.js` (13 test) menjalankannya dan menguji **rantai
penuh** workflow 08: bangun panggilan agen → HTTP nyata → parse → sintesis → HTTP
nyata → bungkus → adapter. Termasuk jalur gagalnya: LLM menjawab teks bebas, JSON
terpotong, `content` null, server mati, dan cache kosong — semuanya harus berakhir
dengan **veto**, bukan terkaan.

Jalankan sendiri:

```bash
node --test tests/test_simulation_integration.js
```

Dua pelajaran yang hanya muncul dari uji ujung-ke-ujung ini:

1. **Aturan keselamatan tidak boleh hidup di prompt.** Aturan "bila ada satu agen
   menyebut risiko peristiwa, event_risk tidak boleh LOW" awalnya hanya tertulis di
   prompt sintesis. Pada uji dengan berita exploit, agen risiko menjawab HIGH tetapi
   sintesis menurunkannya jadi MEDIUM karena empat agen lain menjawab LOW. Lantainya
   sekarang dihitung di kode dan ditegakkan di node pembungkus.
2. **Adapter mengabaikan `verdict.event_risk`.** Ia menghitung risiko dari pemindaian
   kata kunci di teks. Sisi baiknya, LLM tidak bisa menurunkan risiko hanya dengan
   menulis "LOW". Sisi buruknya, risiko yang dilaporkan agen ikut terbuang. Sekarang
   kedua bukti dipakai dan **yang paling konservatif yang menang**; penaikannya
   dicatat sebagai `EVENT_RISK_DINYATAKAN:<tingkat>` di kolom `notes`.

Server tiruan ini **bukan bagian dari runtime bot**. Ia hanya alat uji.
