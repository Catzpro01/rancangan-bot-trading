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
| **GDELT DOC 2.0** | `api.gdeltproject.org/api/v2/doc/doc` | Gratis, **tanpa kunci API** | ✅ Saya panggil `?query=bitcoin&mode=ArtList&format=json&timespan=1d` dan menerima 3 artikel nyata bertanggal `20260912T220000Z` dengan judul, domain, bahasa, dan negara sumber |
| **Fear & Greed Index** | `api.alternative.me/fng/?limit=14` | Gratis, tanpa kunci | ✅ Mengembalikan `value: "61", value_classification: "Greed"` beserta stempel waktu |
| **Aliran stablecoin** | `stablecoins.llama.fi/stablecoincharts/all?stablecoin=1` | Gratis, tanpa kunci | ✅ Mengembalikan deret harian USDT (`circulating`, `circulatingPrevDay`, `circulatingPrevWeek`) |
| **Funding & indeks** | `api.pionex.com/api/v1/market/indexes` | Gratis (publik) | ✅ Endpoint yang sama sudah dipakai workflow `02` |

Mode GDELT yang dipakai workflow `07`:

- `mode=TimelineTone` — rata-rata nada pemberitaan per hari (negatif/positif)
- `mode=TimelineVol` — jumlah artikel per hari, untuk mendeteksi **lonjakan** pemberitaan
- `mode=ArtList` + `tone<-5` — daftar artikel paling negatif 2 hari terakhir

## 3. Batasan tiap sumber — baca sebelum percaya

**GDELT**
- Dibatasi **1 permintaan per 5 detik**; lebih dari itu dibalas HTTP 429. Workflow `07`
  hanya memanggil 3 kali per 15 menit, jadi aman.
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
