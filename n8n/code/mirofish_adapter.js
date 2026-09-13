// n8n/code/mirofish_adapter.js
// Mengubah verdict mentah MiroFish menjadi envelope terstandar yang dibaca gerbang risiko.
//
// Prinsip: GAGAL = VETO. Bila apa pun tidak jelas, hasilnya schema_ok=false atau
// bias='NEUTRAL' -- tidak pernah menebak arah.
//
// Input  : { verdict?: {...}, summary?: {...}, manifest?: {...}, job_id?: string }
// Output : { run_id, verdict_ts, schema_ok, bias, confidence, event_risk,
//            horizon_hours, evidence, adapter_version, notes[] }

const ADAPTER_VERSION = '1.0.0';

// Hanya istilah yang hampir pasti tidak muncul sebagai substring kata lain.
// Kata pendek ('long','short','buy','sell') sengaja TIDAK dipakai: di teks campuran
// Indonesia/Inggris kata seperti "naik" atau "turun" bisa muncul di dalam kata lain
// ("du-a naik"? "me-nurun") dan menghasilkan bias palsu. Lebih baik NEUTRAL.
const LONG_WORDS = ['bullish', 'rally', 'uptrend', 'breakout', 'surge', 'buying pressure'];
const SHORT_WORDS = ['bearish', 'selloff', 'sell-off', 'downtrend', 'dump', 'selling pressure'];
// Bobot ini HARUS identik dengan daftar bobot di mirofish_runner/runner.py.
// tests/verdict_cases.json dijalankan oleh kedua bahasa; bila salah satu diubah
// tanpa yang lain, test paritas gagal.
const RISK_WORDS = [
  ['hack', 0.4], ['exploit', 0.4], ['rug pull', 0.4], ['depeg', 0.4],
  ['insolvent', 0.4], ['insolven', 0.4], ['black swan', 0.45], ['crash', 0.4],
  ['lawsuit', 0.3], ['gugatan', 0.3], ['delisting', 0.3], ['sanksi', 0.3],
  ['regulator', 0.25], ['panic', 0.3], ['kepanikan', 0.3], ['ban', 0.3],
  ['default', 0.3], ['outage', 0.25], ['likuidasi', 0.25], ['pengumuman', 0.2],
  ['kebijakan', 0.2], ['intervensi', 0.3], ['ketidakpastian', 0.25],
  ['volatilitas tinggi', 0.3],
];
const LOW_WORDS = ['stable', 'stabil', 'calm', 'tenang', 'normal', 'sideways'];

function clamp01(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return 0;
  return Math.min(1, Math.max(0, n));
}

function textOf(obj) {
  if (!obj) return '';
  const parts = [];
  const walk = (v, depth) => {
    if (depth > 4 || v == null) return;
    if (typeof v === 'string') { parts.push(v); return; }
    if (typeof v === 'number' || typeof v === 'boolean') { parts.push(String(v)); return; }
    if (Array.isArray(v)) { v.forEach((x) => walk(x, depth + 1)); return; }
    if (typeof v === 'object') Object.values(v).forEach((x) => walk(x, depth + 1));
  };
  walk(obj, 0);
  return parts.join('\n');
}

function detectBias(text) {
  const t = String(text || '').toLowerCase();
  let longScore = 0;
  let shortScore = 0;
  for (const w of LONG_WORDS) if (t.includes(w)) longScore += 1;
  for (const w of SHORT_WORDS) if (t.includes(w)) shortScore += 1;
  if (longScore === 0 && shortScore === 0) return 'NEUTRAL';
  // Ada sinyal dari dua arah = ambigu. Jangan menebak.
  if (longScore > 0 && shortScore > 0) return 'NEUTRAL';
  // Satu istilah saja belum cukup untuk menetapkan arah.
  if (longScore >= 2) return 'LONG';
  if (shortScore >= 2) return 'SHORT';
  return 'NEUTRAL';
}

// Kata kunci pendek (<= 4 huruf, mis. 'ban') hanya dihitung sebagai kata utuh.
// Tanpa ini 'ban' cocok di dalam 'besar' atau 'banjir' dan verdict tenang terbaca
// sebagai risiko sedang.
function hitCount(t, w) {
  if (w.length > 4) return t.includes(w) ? 1 : 0;
  const re = new RegExp(`(?<![a-z0-9])${w}(?![a-z0-9])`, 'g');
  return (t.match(re) || []).length > 0 ? 1 : 0;
}

function detectEventRisk(text) {
  const t = String(text || '').toLowerCase();
  let score = 0;
  const hits = [];
  for (const [w, weight] of RISK_WORDS) {
    if (hitCount(t, w)) { score += weight; hits.push(w); }
  }
  for (const w of LOW_WORDS) {
    if (hitCount(t, w)) { score -= 0.1; hits.push(`-${w}`); }
  }
  score = Math.max(0, Math.min(1, score));
  const level = score >= 0.66 ? 'HIGH' : score >= 0.33 ? 'MEDIUM' : 'LOW';
  return { level, score: Math.round(score * 10000) / 10000, hits };
}

function pickConfidence(verdict) {
  const candidates = [
    verdict?.confidence,
    verdict?.confidence_score,
    verdict?.prediction_confidence,
    verdict?.score,
    verdict?.probability,
    verdict?.summary?.confidence,
  ];
  for (const c of candidates) {
    // HATI-HATI: Number(undefined) === 0 dan Number(null) === 0 di JavaScript.
    // Tanpa pemeriksaan tipe di bawah, verdict tanpa confidence akan lolos sebagai
    // "confidence 0" -- yaitu persis jenis kegagalan senyap yang harus dicegah.
    if (typeof c !== 'number' && typeof c !== 'string') continue;
    if (typeof c === 'string' && c.trim() === '') continue;
    const n = Number(c);
    if (Number.isFinite(n) && n >= 0 && n <= 1) return { value: n, source: 'explicit' };
  }
  return { value: 0, source: 'missing' };
}

// Entry point utama. `raw` adalah isi respons GET /jobs/{id}/verdict.
function toEnvelope(raw) {
  const notes = [];
  const verdict = raw?.verdict || raw?.data?.verdict || null;
  const summary = raw?.summary || raw?.data?.summary || null;
  const manifest = raw?.manifest || raw?.data?.manifest || null;
  const runId = raw?.run_id || manifest?.run_id || raw?.job_id || null;

  // Timestamp dihitung LEBIH DULU dan ikut dibawa pada jalur veto.
  // Alasannya konkret: mirofish_verdict.verdict_ts NOT NULL, jadi veto tanpa
  // timestamp gagal di-INSERT dan jejak auditnya hilang -- kegagalan senyap yang
  // justru muncul saat ada yang tidak beres.
  const tsRaw = manifest?.created_at || verdict?.generated_at || verdict?.timestamp
    || raw?.created_at;
  let verdictTs = null;
  if (tsRaw) {
    const t = Date.parse(String(tsRaw).replace(' ', 'T'));
    if (!Number.isNaN(t)) verdictTs = new Date(t).toISOString().replace('.000Z', 'Z');
    else notes.push('UNPARSEABLE_TIMESTAMP');
  } else {
    notes.push('TIMESTAMP_MISSING');
  }

  const base = {
    run_id: runId,
    verdict_ts: verdictTs,
    schema_ok: false,
    bias: 'NEUTRAL',
    confidence: null,     // null, bukan 0: "tidak ada data" bukan "confidence nol"
    event_risk: 'HIGH',   // default paling konservatif
    horizon_hours: 24,
    evidence: [],
    adapter_version: ADAPTER_VERSION,
    notes,
  };

  if (!verdict || typeof verdict !== 'object') {
    notes.push('VERDICT_MISSING');
    return base;
  }

  const prediction = verdict.prediction || verdict.summary || null;
  if (typeof prediction !== 'string' || prediction.trim().length < 20) {
    notes.push('PREDICTION_MISSING_OR_TOO_SHORT');
    return base;
  }

  const conf = pickConfidence(verdict);
  if (conf.source === 'missing') {
    notes.push('CONFIDENCE_MISSING');
    return base;
  }

  const riskText = textOf([prediction, verdict.key_dynamics, verdict.signals, summary]);
  const risk = detectEventRisk(riskText);
  const bias = detectBias(prediction + '\n' + textOf(verdict.signals));

  if (!verdictTs) return base;

  return {
    run_id: runId,
    verdict_ts: verdictTs,
    schema_ok: true,
    bias,
    confidence: clamp01(conf.value),
    event_risk: risk.level,
    risk_score: risk.score,
    horizon_hours: Number(verdict.horizon_hours || 24),
    evidence: (verdict.key_dynamics || verdict.signals || []).slice(0, 10),
    adapter_version: ADAPTER_VERSION,
    notes: notes.concat(risk.hits.map((h) => `RISK_WORD:${h}`)),
  };
}

module.exports = { toEnvelope, detectBias, detectEventRisk, ADAPTER_VERSION };
