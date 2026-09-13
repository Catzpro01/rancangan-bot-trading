// tests/test_mirofish_adapter.js — adapter harus GAGAL AMAN: apa pun yang tidak jelas
// menghasilkan schema_ok=false atau event_risk='HIGH', bukan tebakan arah.
//
// Dijalankan:  node --test tests/test_mirofish_adapter.js

const test = require('node:test');
const assert = require('node:assert');
const { toEnvelope, detectBias, detectEventRisk } = require('../n8n/code/mirofish_adapter.js');

const TS = '2026-09-13T08:00:00Z';

function rawVerdict(overrides = {}, manifest = { run_id: 'run-1', created_at: TS }) {
  return {
    run_id: 'run-1',
    manifest,
    verdict: {
      prediction: 'Sentimen pasar cenderung stabil; tidak ada pemicu besar dalam 24 jam.',
      confidence: 0.72,
      key_dynamics: ['Perdagangan berlangsung normal'],
      signals: [],
      ...overrides,
    },
  };
}

test('verdict lengkap dan tenang -> schema_ok, LOW, NEUTRAL', () => {
  const e = toEnvelope(rawVerdict());
  assert.strictEqual(e.schema_ok, true, JSON.stringify(e.notes));
  assert.strictEqual(e.event_risk, 'LOW');
  assert.strictEqual(e.bias, 'NEUTRAL');
  assert.strictEqual(e.confidence, 0.72);
  assert.strictEqual(e.verdict_ts, TS);
  assert.strictEqual(e.run_id, 'run-1');
});

test('verdict hilang -> schema_ok=false dan event_risk HIGH (gagal aman)', () => {
  const e = toEnvelope({ run_id: 'x' });
  assert.strictEqual(e.schema_ok, false);
  assert.strictEqual(e.event_risk, 'HIGH');
  assert.ok(e.notes.includes('VERDICT_MISSING'));
});

test('prediction terlalu pendek -> ditolak, bukan ditebak', () => {
  const e = toEnvelope(rawVerdict({ prediction: 'naik' }));
  assert.strictEqual(e.schema_ok, false);
  assert.ok(e.notes.includes('PREDICTION_MISSING_OR_TOO_SHORT'));
});

test('confidence hilang -> ditolak (jangan mengarang angka)', () => {
  const e = toEnvelope(rawVerdict({ confidence: null }));
  assert.strictEqual(e.schema_ok, false);
  assert.ok(e.notes.includes('CONFIDENCE_MISSING'));
});

test('confidence di luar 0..1 -> ditolak', () => {
  const e = toEnvelope(rawVerdict({ confidence: 7.5 }));
  assert.strictEqual(e.schema_ok, false);
});

test('timestamp tidak bisa dibaca -> ditolak (verdict akan dianggap basi)', () => {
  const e = toEnvelope(rawVerdict({}, { run_id: 'run-1', created_at: 'kemarin sore' }));
  assert.strictEqual(e.schema_ok, false);
  assert.ok(e.notes.some((n) => n.includes('TIMESTAMP')));
});

test('kata risiko tinggi -> event_risk HIGH', () => {
  const e = toEnvelope(rawVerdict({
    prediction: 'Ada kemungkinan hack besar dan likuidasi berantai memicu crash harga.',
  }));
  assert.strictEqual(e.schema_ok, true);
  assert.strictEqual(e.event_risk, 'HIGH');
});

test('kata risiko sedang -> MEDIUM', () => {
  const e = toEnvelope(rawVerdict({
    prediction: 'Pengumuman regulator minggu ini bisa memengaruhi pasar secara terbatas.',
  }));
  assert.strictEqual(e.event_risk, 'MEDIUM');
});

test('bias hanya ditetapkan bila sinyal arah kuat', () => {
  assert.strictEqual(detectBias('rally bullish breakout uptrend'), 'LONG');
  assert.strictEqual(detectBias('selloff bearish dump downtrend'), 'SHORT');
  assert.strictEqual(detectBias('bullish tapi juga bearish'), 'NEUTRAL');
  assert.strictEqual(detectBias(''), 'NEUTRAL');
  assert.strictEqual(detectBias('bullish'), 'NEUTRAL'); // satu kata saja tidak cukup
});

test('bias dari envelope tidak mengarang arah saat ambigu', () => {
  const e = toEnvelope(rawVerdict({ prediction: 'Pasar bergerak dua arah tanpa kecenderungan jelas.' }));
  assert.strictEqual(e.bias, 'NEUTRAL');
});

test('skor risiko monoton: teks lebih berbahaya -> skor lebih tinggi', () => {
  const calm = detectEventRisk('Perdagangan normal, tidak ada agenda besar.').score;
  const scary = detectEventRisk('Risiko hack, likuidasi berantai, dan keputusan regulator.').score;
  assert.ok(scary > calm, `${scary} harus > ${calm}`);
});

test('evidence dibatasi agar tidak membengkakkan payload', () => {
  const many = Array.from({ length: 40 }, (_, i) => `dinamika-${i}`);
  const e = toEnvelope(rawVerdict({ key_dynamics: many }));
  assert.ok(e.evidence.length <= 10);
});

test('adapter_version selalu ada untuk audit', () => {
  const e = toEnvelope(rawVerdict());
  assert.match(e.adapter_version, /^\d+\.\d+\.\d+$/);
});
