// tests/test_mirofish_verdict_parity.js
// Adapter MiroFish (JavaScript, berjalan di n8n) diuji terhadap fixture yang SAMA
// dengan tests/test_mirofish_runner.py (Python). Kalau keduanya menyimpang, salah
// satu test ini gagal — penyimpangan diam-diam antar-bahasa adalah jenis bug yang
// paling mahal karena tidak ada satu pun sisi yang "salah" secara lokal.
//
// Dijalankan: node --test tests/test_mirofish_verdict_parity.js

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { toEnvelope, ADAPTER_VERSION } = require('../n8n/code/mirofish_adapter.js');

const FIXTURE = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'verdict_cases.json'), 'utf8'),
);

// Respons runner dibungkus: adapter membaca `verdict`, `manifest`, dan `run_id`.
function wrap(verdict, runId = FIXTURE.run_id) {
  return {
    run_id: runId,
    job_id: 'job-parity',
    job_status: 'SUCCEEDED',
    created_at: FIXTURE.verdict_ts,
    verdict: verdict || null,
    summary: null,
    manifest: { run_id: runId, created_at: FIXTURE.verdict_ts },
    envelope: null,
  };
}

for (const c of FIXTURE.cases) {
  test(`paritas: ${c.name}`, () => {
    const env = toEnvelope(wrap(c.verdict));
    for (const [key, expected] of Object.entries(c.expect)) {
      assert.strictEqual(env[key], expected,
        `${key}: ${JSON.stringify(env[key])} != ${JSON.stringify(expected)}`);
    }
    assert.strictEqual(env.adapter_version, ADAPTER_VERSION);
  });
}

test('verdict rusak tetap membawa verdict_ts dan default paling konservatif', () => {
  const env = toEnvelope(wrap(null));
  assert.strictEqual(env.schema_ok, false);
  assert.strictEqual(env.event_risk, 'HIGH');
  assert.ok(env.notes.includes('VERDICT_MISSING'));
  assert.strictEqual(env.verdict_ts, FIXTURE.verdict_ts);
});

test('respons tanpa sumber timestamp sama sekali -> veto', () => {
  const raw = wrap(FIXTURE.cases[0].verdict);
  delete raw.manifest;
  delete raw.created_at;      // manifest.created_at dan raw.created_at keduanya absen
  const env = toEnvelope(raw);
  assert.strictEqual(env.schema_ok, false, 'tanpa timestamp tidak boleh dianggap sah');
  assert.strictEqual(env.verdict_ts, null);
  assert.ok(env.notes.includes('TIMESTAMP_MISSING'));
});

test('veto tetap membawa verdict_ts agar INSERT ke mirofish_verdict tidak gagal', () => {
  // mirofish_verdict.verdict_ts NOT NULL. Veto tanpa timestamp tidak akan tersimpan,
  // jadi jejak audit hilang tepat saat ada yang tidak beres.
  for (const c of FIXTURE.cases) {
    const env = toEnvelope(wrap(c.verdict));
    assert.strictEqual(env.verdict_ts, FIXTURE.verdict_ts,
      `${c.name}: verdict_ts harus selalu terisi dari manifest`);
  }
});

test('confidence null (bukan 0) pada setiap jalur veto', () => {
  for (const c of FIXTURE.cases.filter((x) => x.expect.schema_ok === false)) {
    const env = toEnvelope(wrap(c.verdict));
    assert.strictEqual(env.confidence, null,
      `${c.name}: confidence harus null, bukan 0`);
  }
});

test('semua kasus fixture menghasilkan kunci amplop yang lengkap', () => {
  const wajib = ['run_id', 'verdict_ts', 'schema_ok', 'bias', 'confidence',
                 'event_risk', 'horizon_hours', 'evidence', 'adapter_version', 'notes'];
  for (const c of FIXTURE.cases) {
    const env = toEnvelope(wrap(c.verdict));
    for (const k of wajib) assert.ok(k in env, `${c.name}: kunci ${k} hilang`);
  }
});
