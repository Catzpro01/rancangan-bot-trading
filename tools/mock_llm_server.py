#!/usr/bin/env python3
"""Server LLM tiruan untuk menguji workflow 08 tanpa biaya dan tanpa API nyata.

Meniru `POST /chat/completions` ala OpenAI. Tujuannya bukan meniru kecerdasan model
bahasa -- itu tidak mungkin dan tidak perlu -- melainkan meniru BENTUK percakapannya,
sehingga kode yang mengirim permintaan dan mem-parse jawaban bisa diuji apa adanya.

Yang ditiru:
  * permintaan tanpa `Authorization: Bearer ...` -> 401
  * badan permintaan bukan JSON atau tanpa `messages` -> 400
  * jawaban berbentuk {choices:[{message:{role,content}}], usage:{...}}
  * `content` berupa JSON bila prompt menuntutnya, seperti model sungguhan

Perilaku ditentukan oleh `X-Mock-Mode` (header) atau `?mode=` (query):
  normal      jawaban JSON yang sah, sesuai peran agen yang terdeteksi
  sampah      jawaban teks bebas yang BUKAN JSON  -> adapter harus memveto
  jsonrusak   JSON yang terpotong di tengah       -> adapter harus memveto
  kosong      `content` null                      -> adapter harus memveto
  http500     server gagal                        -> rantai harus bertahan

Peran agen dikenali dari kata kunci di prompt sistem, sama seperti cara manusia
membacanya. Bila tidak dikenali, permintaan dianggap sebagai langkah sintesis.

Dipakai oleh tests/test_simulation_integration.js. Bukan bagian dari runtime bot.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("MOCK_LLM_TOKEN", "token-uji")
PORT = int(os.environ.get("MOCK_LLM_PORT", "8099"))

PERAN = (
    ("risiko_ekstrem", "risiko peristiwa"),
    ("kontrarian", "melawan konsensus"),
    ("arus_modal", "aliran modal"),
    ("mikrostruktur", "mikrostruktur"),
    ("makro", "Analis makro"),
)


def kenali_peran(prompt: str) -> str:
    for nama, kata in PERAN:
        if kata.lower() in prompt.lower():
            return nama
    return "sintesis"


def jawab_agen(peran: str, konteks: dict) -> str:
    """Jawaban yang masuk akal untuk tiap peran. Deterministik, tanpa angka acak."""
    fear = konteks.get("fear_greed") or []
    nilai = int(fear[0].get("value", 50)) if fear and isinstance(fear[0], dict) else 50
    negatif = konteks.get("berita_negatif") or []
    ada_berita = bool(negatif)
    judul = negatif[0].get("title", "") if ada_berita and isinstance(negatif[0], dict) else ""

    if peran == "risiko_ekstrem":
        # Peran ini yang paling menentukan: hanya ia yang boleh menaikkan event_risk.
        return json.dumps({
            "stance": "NEUTRAL",
            "confidence": 0.75 if ada_berita else 0.45,
            "event_risk": "HIGH" if ada_berita else "LOW",
            "alasan": ("Ada pemberitaan negatif yang perlu diwaspadai."
                       if ada_berita else
                       "Tidak ditemukan pemicu peristiwa dalam data yang tersedia."),
            "bukti": [judul] if ada_berita else ["tidak ada berita negatif pada jendela 2 hari"],
        }, ensure_ascii=False)

    if peran == "kontrarian":
        arah = "bearish" if nilai < 45 else "bullish"
        return json.dumps({
            "stance": "SHORT" if nilai >= 55 else "LONG",
            "confidence": 0.4,
            "event_risk": "MEDIUM",
            "alasan": f"Konsensus terlihat {arah}, dan itu justru alasan untuk berhati-hati.",
            "bukti": [f"Fear & Greed {nilai} sering menandai puncak lokal, bukan dasar"],
        }, ensure_ascii=False)

    if peran == "arus_modal":
        masuk = nilai >= 50
        return json.dumps({
            "stance": "LONG" if masuk else "SHORT",
            "confidence": 0.55,
            "event_risk": "LOW",
            "alasan": ("Fear & Greed di atas 50 menunjukkan modal masih masuk."
                       if masuk else "Fear & Greed di bawah 50 menunjukkan modal keluar."),
            "bukti": [f"Fear & Greed = {nilai}"],
        }, ensure_ascii=False)

    if peran == "mikrostruktur":
        return json.dumps({
            "stance": "NEUTRAL",
            "confidence": 0.35,
            "event_risk": "LOW",
            "alasan": "Data funding belum menunjukkan kepadatan posisi yang ekstrem.",
            "bukti": ["funding rate dalam rentang normal"],
        }, ensure_ascii=False)

    if peran == "makro":
        return json.dumps({
            "stance": "NEUTRAL",
            "confidence": 0.4,
            "event_risk": "MEDIUM",
            "alasan": "Tidak ada jadwal kebijakan bank sentral dalam jendela pendek ini.",
            "bukti": ["kalender makro kosong pada jendela 24 jam"],
        }, ensure_ascii=False)

    # sintesis: menyimpulkan kelima pendapat di atas
    jumlah = konteks.get("jumlah_agen", 0)
    gagal = konteks.get("agen_gagal", 0)
    return json.dumps({
        # Adapter menolak prediction di bawah 20 kata (PREDICTION_MISSING_OR_TOO_SHORT).
        # Prediksi tiruan ini dulu 18 kata dan ditolak -- adapter yang benar, mock yang
        # kurang. Angka 20 kata itu ada supaya LLM tidak lolos dengan satu kalimat hampa.
        "prediction": ("Tidak ditemukan pemicu peristiwa besar dalam data yang tersedia "
                       "pada jendela pengamatan; pasar berada dalam rezim normal dengan "
                       "sentimen netral yang cenderung hati-hati, volume pemberitaan "
                       "stabil, dan aliran stablecoin tidak menunjukkan tekanan keluar, "
                       "sehingga tidak ada alasan untuk menahan perdagangan pada saat ini."),
        "confidence": 0.62 if gagal == 0 else 0.3,
        "event_risk": "LOW" if gagal == 0 and jumlah >= 5 else "MEDIUM",
        "key_dynamics": [f"{jumlah} agen melaporkan, {gagal} gagal",
                         f"Fear & Greed {nilai}"],
        "signals": ["tidak ada sinyal veto"],
    }, ensure_ascii=False)


def konteks_dari(messages: list) -> dict:
    """Ambil konteks dari pesan pengguna, bila memang JSON."""
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            try:
                return json.loads(c)
            except (ValueError, TypeError):
                return {}
    return {}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 dengan koneksi ditutup tiap permintaan. HTTP/1.1 keep-alive ternyata
    # membuat BaseHTTPRequestHandler salah membaca metode pada permintaan berikutnya
    # bila klien mengirim beberapa permintaan cepat di satu koneksi -- gejalanya 501
    # "Unsupported method" yang tidak ada hubungannya dengan kode di bawah.
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:  # diam di log
        pass

    def _kirim(self, kode: int, muatan: dict | str) -> None:
        badan = muatan if isinstance(muatan, str) else json.dumps(muatan)
        data = badan.encode("utf-8")
        self.send_response(kode)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except Exception as exc:  # pragma: no cover - hanya agar penyebabnya terlihat
            sys.stderr.write(f"[mock-llm] {type(exc).__name__}: {exc}\n")
            sys.stderr.flush()
            raise

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            self._kirim(200, {"status": "ok"})
        else:
            self._kirim(404, {"error": "tidak ditemukan"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0].rstrip("/") != "/chat/completions":
            self._kirim(404, {"error": "tidak ditemukan"})
            return

        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {TOKEN}":
            self._kirim(401, {"error": {"message": "kunci API tidak sah", "type": "auth"}})
            return

        panjang = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(panjang).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._kirim(400, {"error": {"message": "badan bukan JSON", "type": "invalid"}})
            return
        if not isinstance(req.get("messages"), list) or not req["messages"]:
            self._kirim(400, {"error": {"message": "messages kosong", "type": "invalid"}})
            return

        mode = self.headers.get("X-Mock-Mode") or ""
        if not mode:
            cocok = re.search(r"[?&]mode=([a-z0-9]+)", self.path)
            mode = cocok.group(1) if cocok else "normal"

        if mode == "http500":
            self._kirim(500, {"error": {"message": "server tiruan gagal", "type": "server"}})
            return

        prompt = " ".join(str(m.get("content", "")) for m in req["messages"]
                          if m.get("role") == "system")
        # Header lebih dulu. Menebak peran dari isi prompt ternyata RAPUH: prompt
        # sintesis memuat frasa "risiko peristiwa" di dalam aturannya sendiri, jadi
        # pencocokan kata kunci salah mengenalinya sebagai agen risiko.
        peran = self.headers.get("X-Pg-Role") or kenali_peran(prompt)
        konteks = konteks_dari(req["messages"])

        if mode == "sampah":
            isi = "Maaf, saya tidak dapat memproses permintaan tersebut saat ini."
        elif mode == "jsonrusak":
            isi = '{"prediction": "terpotong di sini", "confidence": 0.'
        elif mode == "kosong":
            isi = None
        else:
            isi = jawab_agen(peran, konteks)

        self._kirim(200, {
            "id": f"mock-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model", "mock"),
            "mock": {"peran": peran, "mode": mode or "normal"},
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": isi},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": len(prompt) // 4,
                      "completion_tokens": len(isi or "") // 4,
                      "total_tokens": (len(prompt) + len(isi or "")) // 4},
        })


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mock LLM di 127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
