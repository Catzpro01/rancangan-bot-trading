// tests/test_simulation_integration.js
// Uji UJUNG-KE-UJUNG workflow 08 melawan server LLM tiruan.
//
// Bedanya dengan test_workflow_code_nodes.js: di sana tiap Code node diuji terpisah
// dengan masukan karangan. Di sini permintaan yang benar-benar dihasilkan node
// dikirim lewat HTTP ke server tiruan, dan JAWABAN server dimasukkan kembali ke node
// berikutnya. Jadi yang diuji adalah rantainya, termasuk bentuk permintaan, header
// Authorization, dan cara rantai bertahan saat LLM menjawab tidak masuk akal.
//
// Jalankan: node --test tests/test_simulation_integration.js

const assert = require('node:assert');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const PORT = 8099;
const BASE = `http://127.0.0.1:${PORT}`;
const TOKEN = 'token-uji';
const PY = process.env.PYTHON_BIN || '.venv/bin/python';

function loadWorkflow(nama) {
  return JSON.parse(fs.readFileSync(path.join(ROOT, 'n8n/workflows', nama), 'utf8'));
}

// Meniru n8n: $json = json item masukan pertama. Lihat catatan di
// test_workflow_code_nodes.js soal kenapa ini belum diverifikasi di n8n sungguhan.
async function runNode(code, { items = [], env = {}, json } = {}) {
  const mod = { exports: {} };
  const sandbox = {
    require: (id) => require(id), module: mod, exports: mod.exports,
    console, Buffer, process, __dirname, setTimeout, clearTimeout, JSON, Math, Date,
  };
  vm.createContext(sandbox);
  const f = vm.runInContext(`(async function (items, $env, $json) {\n${code}\n})`, sandbox);
  return f(items, env, json !== undefined ? json : ((items[0] && items[0].json) || {}));
}

async function tidur(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function panggil(body, mode, role) {
  const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${TOKEN}` };
  if (mode) headers['X-Mock-Mode'] = mode;
  if (role) headers['X-Pg-Role'] = role;
  const res = await fetch(`${BASE}/chat/completions`, {
    method: 'POST', headers, body: JSON.stringify(body),
  });
  return { status: res.status, json: await res.json().catch(() => null) };
}

const WF = loadWorkflow('08-market-intel-simulation.json');
const N_BANGUN = WF.nodes.find((n) => n.name === 'Bangun panggilan agen').parameters.jsCode;
const N_SINTESIS = WF.nodes.find((n) => n.name === 'Simpulkan pendapat agen').parameters.jsCode;
const N_BUNGKUS = WF.nodes.find((n) => n.name === 'Bungkus jadi verdict').parameters.jsCode;
const N_ADAPTER = WF.nodes.find((n) => n.name === 'Adapter verdict').parameters.jsCode;

const ENV = { LLM_API_BASE: BASE, LLM_API_KEY: TOKEN, LLM_MODEL: 'mock-1',
              PIONEX_SYMBOL: 'BTC_USDT_PERP' };

// Cache seperti yang benar-benar ditulis workflow 07 dari sumber yang sudah diverifikasi.
// Bentuk cache PERSIS seperti yang ditulis workflow 07: node "Beri nama: X" menaruh
// nilai mentah di field bernama X, jadi `cache.negative` adalah ARRAY, bukan objek.
// Fixture ini dulu salah membungkus satu lapis lagi, dan kesalahannya baru terlihat
// di sini -- bukan di test per-node -- karena hanya rantai penuh yang membaca cache.
const CACHE_AMAN = {
  symbol: 'BTC_USDT_PERP',
  collected_at: new Date().toISOString(),
  tone: { timeline: [{ series: 'Average Tone',
    data: [{ date: '20260913T090000Z', value: 1.6048 }] }] },
  volume: { timeline: [{ series: 'Article Count',
    data: [{ date: '20260913T090000Z', value: 412 }] }] },
  negative: [],
  fear_greed: [{ value: '61', value_classification: 'Greed' }],
  stablecoin: [{ totalCirculatingUSD: 183427244504 }],
  funding: { indexPrice: 108000, fundingRate: 0.0001 },
};

const CACHE_BERITA = {
  ...CACHE_AMAN,
  negative: [{ title: 'Exchange X halts withdrawals after exploit',
               domain: 'coindesk.com', seendate: '20260913T020000Z' }],
};

let server = null;

test.before(async () => {
  server = spawn(PY, [path.join(ROOT, 'tools/mock_llm_server.py')], {
    env: { ...process.env, MOCK_LLM_PORT: String(PORT), MOCK_LLM_TOKEN: TOKEN },
    cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'],
  });
  for (let i = 0; i < 100; i += 1) {
    try {
      const r = await fetch(`${BASE}/health`);
      if (r.ok) return;
    } catch (e) { /* belum siap */ }
    await tidur(100);
  }
  throw new Error('server LLM tiruan tidak kunjung siap');
});

test.after(() => { if (server) server.kill('SIGTERM'); });

// ------------------------------------------------------------------ server tiruan

test('server tiruan menolak permintaan tanpa Bearer', async () => {
  const res = await fetch(`${BASE}/chat/completions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages: [{ role: 'user', content: 'hai' }] }),
  });
  assert.strictEqual(res.status, 401);
});

test('server tiruan menolak badan yang bukan JSON', async () => {
  const res = await fetch(`${BASE}/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain', Authorization: `Bearer ${TOKEN}` },
    body: 'ini bukan json',
  });
  assert.strictEqual(res.status, 400);
});

// ------------------------------------------------- permintaan workflow -> server

test('permintaan yang dihasilkan workflow diterima server dan dikenali perannya', async () => {
  const out = await runNode(N_BANGUN, { env: ENV, items: [{ json: { cache: CACHE_AMAN } }] });
  assert.strictEqual(out.length, 5);

  const peran = [];
  for (const { json: j } of out) {
    const { status, json: res } = await panggil(JSON.parse(j.body), null, j.agent);
    assert.strictEqual(status, 200, `agen ${j.agent} ditolak`);
    assert.strictEqual(res.mock.peran, j.agent,
      `server mengenali ${res.mock.peran}, workflow mengirim ${j.agent}`);
    peran.push(res.mock.peran);
  }
  assert.deepStrictEqual(new Set(peran).size, 5, 'kelima peran harus berbeda');
});

test('konteks cache benar-benar sampai ke server dan memengaruhi jawaban', async () => {
  const bangun = async (cache) => runNode(N_BANGUN, { env: ENV, items: [{ json: { cache } }] });

  const aman = await bangun(CACHE_AMAN);
  const berita = await bangun(CACHE_BERITA);
  const cari = (arr) => arr.find((x) => x.json.agent === 'risiko_ekstrem');

  const a = await panggil(JSON.parse(cari(aman).json.body), null, 'risiko_ekstrem');
  const b = await panggil(JSON.parse(cari(berita).json.body), null, 'risiko_ekstrem');
  const ja = JSON.parse(a.json.choices[0].message.content);
  const jb = JSON.parse(b.json.choices[0].message.content);

  assert.strictEqual(ja.event_risk, 'LOW', 'tanpa berita negatif harus LOW');
  assert.strictEqual(jb.event_risk, 'HIGH', 'ada berita exploit harus HIGH');
  assert.ok(jb.bukti[0].includes('Exchange X'), 'judul berita harus dikutip sebagai bukti');
});

// ------------------------------------------------------- rantai penuh, jalur aman

async function rantai(cache, mode) {
  const panggilan = await runNode(N_BANGUN, { env: ENV, items: [{ json: { cache } }] });
  const jawaban = [];
  for (const { json: j } of panggilan) {
    const res = await panggil(JSON.parse(j.body), mode, j.agent);
    jawaban.push({ json: { agent: j.agent, ...(res.json || {}) } });
  }
  const sintesis = await runNode(N_SINTESIS, { env: ENV, items: jawaban });
  const resSintesis = await panggil(JSON.parse(sintesis[0].json.body), mode, 'sintesis');
  const bungkus = await runNode(N_BUNGKUS, {
    env: ENV,
    items: [{ json: { ...(resSintesis.json || {}),
                      agen_gagal: sintesis[0].json.agen_gagal,
                      lantai_event_risk: sintesis[0].json.lantai_event_risk,
                      jumlah_agen: sintesis[0].json.jumlah_agen } }],
  });
  const env = await runNode(N_ADAPTER, { env: ENV, items: bungkus });
  return { panggilan, jawaban, sintesis, bungkus, env };
}

test('rantai penuh dengan pasar tenang: verdict sah, tidak ada veto', async () => {
  const r = await rantai(CACHE_AMAN);

  assert.strictEqual(r.sintesis[0].json.agen_gagal, 0,
    'kelima agen menjawab JSON sah, tidak boleh ada yang dihitung gagal');
  assert.strictEqual(r.env[0].json.schema_ok, true, r.env[0].json.notes);
  // Yang menentukan bukan nilainya LOW, melainkan TIDAK ADA VETO. Agen makro dan
  // kontrarian sengaja menjawab MEDIUM, jadi lantai yang benar di sini memang MEDIUM.
  assert.notStrictEqual(r.env[0].json.event_risk, 'HIGH',
    'pasar tenang tidak boleh memicu veto');
  assert.ok(r.env[0].json.run_id.startsWith('n8n-sim-'), r.env[0].json.run_id);
  // amplop harus punya semua kunci yang dibaca gerbang risiko
  for (const k of ['run_id', 'verdict_ts', 'schema_ok', 'bias', 'confidence',
                   'event_risk', 'evidence', 'adapter_version']) {
    assert.ok(k in r.env[0].json, `kunci ${k} hilang dari amplop`);
  }
});

test('rantai penuh saat ada berita exploit: veto HIGH', async () => {
  const r = await rantai(CACHE_BERITA);
  assert.strictEqual(r.env[0].json.event_risk, 'HIGH');
  assert.ok(r.env[0].json.evidence.some((e) => String(e).includes('Exchange X'))
            || r.env[0].json.notes.length > 0,
    'veto harus menyertakan jejak penyebabnya');
});

// -------------------------------------------------- rantai penuh, jalur gagal

test('LLM menjawab teks bebas: rantai bertahan dan MEMVETO, bukan menebak', async () => {
  const r = await rantai(CACHE_AMAN, 'sampah');
  assert.strictEqual(r.sintesis[0].json.agen_gagal, 5, 'kelima jawaban harus ditandai gagal');
  assert.strictEqual(r.bungkus[0].json.verdict, null);
  assert.strictEqual(r.env[0].json.schema_ok, false);
  assert.strictEqual(r.env[0].json.event_risk, 'HIGH');
  assert.strictEqual(r.env[0].json.bias, 'NEUTRAL');
  assert.strictEqual(r.env[0].json.confidence, null);
});

test('LLM menjawab JSON terpotong: tetap veto', async () => {
  const r = await rantai(CACHE_AMAN, 'jsonrusak');
  assert.strictEqual(r.env[0].json.schema_ok, false);
  assert.strictEqual(r.env[0].json.event_risk, 'HIGH');
});

test('LLM menjawab content null: tetap veto', async () => {
  const r = await rantai(CACHE_AMAN, 'kosong');
  assert.strictEqual(r.env[0].json.schema_ok, false);
  assert.strictEqual(r.env[0].json.event_risk, 'HIGH');
});

test('server LLM mati total: rantai tetap menghasilkan veto, bukan crash', async () => {
  // Panggil endpoint yang tidak ada -> 404, meniru LLM yang tidak terjangkau.
  const panggilan = await runNode(N_BANGUN, { env: ENV, items: [{ json: { cache: CACHE_AMAN } }] });
  const jawaban = [];
  for (const { json: j } of panggilan) {
    const res = await fetch(`${BASE}/tidak-ada`, {
      method: 'POST', headers: { 'Content-Type': 'application/json',
                                 Authorization: `Bearer ${TOKEN}` },
      body: JSON.stringify(JSON.parse(j.body)),
    });
    const badan = await res.json().catch(() => null);
    if (res.ok) jawaban.push({ json: { agent: j.agent, ...(badan || {}) } });
  }
  assert.strictEqual(jawaban.length, 0, 'tidak ada jawaban yang layak dipakai');

  const sintesis = await runNode(N_SINTESIS, { env: ENV, items: jawaban });
  assert.strictEqual(sintesis[0].json.jumlah_agen, 0);
  const bungkus = await runNode(N_BUNGKUS, {
    env: ENV, items: [{ json: { agen_gagal: 0, jumlah_agen: 0 } }],
  });
  const env = await runNode(N_ADAPTER, { env: ENV, items: bungkus });
  assert.strictEqual(env[0].json.schema_ok, false);
  assert.strictEqual(env[0].json.event_risk, 'HIGH',
    'tanpa verdict apa pun, satu-satunya jawaban aman adalah veto');
});

test('cache kosong: rantai tetap jalan dan memveto', async () => {
  const r = await rantai({}, 'normal');
  assert.strictEqual(r.panggilan.length, 5, 'agen tetap dipanggil walau cache kosong');
  assert.strictEqual(r.env[0].json.schema_ok, true);
  // tanpa data, sintesis tidak boleh mengaku yakin
  assert.ok(r.env[0].json.confidence === null || r.env[0].json.confidence <= 0.62,
    `confidence ${r.env[0].json.confidence} terlalu tinggi untuk data kosong`);
});

test('lantai event_risk ditegakkan oleh kode, bukan oleh prompt', async () => {
  // Sintesis yang BANDAL: agen risiko bilang HIGH, sintesis bilang LOW.
  const r = await rantai(CACHE_BERITA);
  if (process.env.DEBUG_RANTAI) {
    console.log(JSON.stringify(r.jawaban.map((x) => ({
      agen: x.json.agent, peran_server: x.json.mock && x.json.mock.peran,
      isi: (x.json.choices && x.json.choices[0].message.content || '').slice(0, 50),
    })), null, 1));
  }
  if (process.env.DEBUG_RANTAI) {
    console.log('SINTESIS JSON:', JSON.stringify(Object.fromEntries(
      Object.entries(r.sintesis[0].json).filter(([k]) => k !== 'body'))));
  }
  assert.strictEqual(r.sintesis[0].json.lantai_event_risk, 'HIGH',
    'sintesis harus menghitung lantai HIGH dari agen risiko');

  const bandal = await runNode(N_BUNGKUS, {
    env: ENV,
    items: [{ json: {
      choices: [{ message: { content: JSON.stringify({
        prediction: 'Sintesis ini sengaja menurunkan risiko agar terlihat aman dan '
          + 'meyakinkan, padahal satu agen sudah melaporkan adanya peristiwa serius.',
        confidence: 0.7, event_risk: 'LOW', key_dynamics: [], signals: [],
      }) } }],
      agen_gagal: 0, lantai_event_risk: 'HIGH', jumlah_agen: 5,
    } }],
  });
  if (process.env.DEBUG_RANTAI) {
    console.log('N_BUNGKUS punya blok lantai:', N_BUNGKUS.includes('Tegakkan lantai'));
    console.log('verdict bandal:', JSON.stringify(bandal[0].json.verdict));
  }
  assert.strictEqual(bandal[0].json.verdict.event_risk, 'HIGH',
    'kode harus menaikkan kembali risiko yang diturunkan sintesis');

  const env = await runNode(N_ADAPTER, { env: ENV, items: bandal });
  assert.strictEqual(env[0].json.event_risk, 'HIGH');
  assert.strictEqual(env[0].json.schema_ok, true);
});

test('sintesis boleh MENAIKKAN risiko di atas lantai', async () => {
  const naik = await runNode(N_BUNGKUS, {
    env: ENV,
    items: [{ json: {
      choices: [{ message: { content: JSON.stringify({
        prediction: 'Sintesis menyimpulkan ada risiko sistemik yang lebih besar dari '
          + 'yang dilaporkan agen mana pun, sehingga perdagangan perlu ditahan.',
        confidence: 0.5, event_risk: 'HIGH', key_dynamics: [], signals: [],
      }) } }],
      agen_gagal: 0, lantai_event_risk: 'MEDIUM', jumlah_agen: 5,
    } }],
  });
  assert.strictEqual(naik[0].json.verdict.event_risk, 'HIGH',
    'lantai adalah batas bawah, bukan plafon');
});
