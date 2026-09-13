// tests/test_workflow_code_nodes.js
// Menguji kode yang BENAR-BENAR tertanam di berkas workflow n8n: bukan salinan di
// n8n/code/, melainkan string di dalam n8n/workflows/*.json yang akan diimpor.
//
// Dijalankan:  node --test tests/test_workflow_code_nodes.js

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WF_DIR = path.join(__dirname, '..', 'n8n', 'workflows');

function loadWorkflow(file) {
  return JSON.parse(fs.readFileSync(path.join(WF_DIR, file), 'utf8'));
}

function codeNode(wf, name) {
  const n = wf.nodes.find((x) => x.name === name);
  assert.ok(n, `node "${name}" tidak ada di ${wf.name}`);
  assert.strictEqual(n.type, 'n8n-nodes-base.code', `"${name}" bukan Code node`);
  return n.parameters.jsCode;
}

// Menjalankan kode seperti n8n: di dalam fungsi dengan items/$env/$json, dan dengan
// `require` tersedia (Code node n8n adalah modul CommonJS). `new Function` tidak
// menyediakan require, jadi dipakai vm dengan sandbox berisi require.
async function runNode(code, { items = [], env = {}, json = {} } = {}) {
  const sandboxModule = { exports: {} };
  const sandbox = {
    require: (id) => require(id),
    module: sandboxModule,
    exports: sandboxModule.exports,
    console, Buffer, process, __dirname, setTimeout, clearTimeout, JSON, Math, Date,
  };
  vm.createContext(sandbox);
  const src = `(async function (items, $env, $json) {\n${code}\n})`;
  const factory = vm.runInContext(src, sandbox);
  return factory(items, env, json);
}

const CFG = JSON.parse(
  fs.readFileSync(path.join(__dirname, '..', 'config', 'risk_config.json'), 'utf8'),
);
const RULES = {
  symbol: 'BTC_USDT_PERP', base_step: 0.0001, quote_step: 0.01, min_size: 0,
  max_size: 1e9, min_notional: 5, mmr: 0.005, taker_fee: 0.0005,
};
const NOW_ISO = () => new Date().toISOString();

function goodSignal() {
  const price = 100000;
  const stopDist = 0.008;
  return {
    id: 'sig-test', direction: 'LONG', entry: price,
    stop: price * (1 - stopDist),
    take_profit: price * (1 + stopDist * 1.2),
    ts: NOW_ISO(),
    mirofish: { schema_ok: true, verdict_ts: NOW_ISO(), confidence: 0.8,
                event_risk: 'LOW', bias: 'NEUTRAL', run_id: 'r1' },
    stats: { win_rate: 0.62, avg_win_pct: 0.0096, avg_loss_pct: 0.008 },
  };
}

test('semua Code node di semua workflow bisa dikompilasi', () => {
  for (const file of fs.readdirSync(WF_DIR).filter((f) => f.endsWith('.json'))) {
    const wf = loadWorkflow(file);
    for (const n of wf.nodes.filter((x) => x.type === 'n8n-nodes-base.code')) {
      assert.doesNotThrow(() => new Function('items', '$env', '$json', n.parameters.jsCode),
        `${file} :: ${n.name}`);
    }
  }
});

test('gerbang risiko di 03-trading-loop menyetujui sinyal sehat', async () => {
  const wf = loadWorkflow('03-trading-loop.json');
  const out = await runNode(codeNode(wf, 'GERBANG RISIKO (default DENY)'), {
    items: [{ json: { signal: goodSignal(), equity: 1000, entry: 100000,
                      rules: RULES, cfg: CFG, open_positions: 0, breaker: {} } }],
  });
  assert.strictEqual(out[0].json.approved, true, JSON.stringify(out[0].json.reasons));
  assert.strictEqual(out[0].json.size, 0.0058);
  assert.ok(out[0].json.liq_distance_pct > out[0].json.stop_distance_pct);
});

test('gerbang risiko di 03-trading-loop menolak veto MiroFish', async () => {
  const wf = loadWorkflow('03-trading-loop.json');
  const sig = goodSignal();
  sig.mirofish.event_risk = 'HIGH';
  const out = await runNode(codeNode(wf, 'GERBANG RISIKO (default DENY)'), {
    items: [{ json: { signal: sig, equity: 1000, entry: 100000,
                      rules: RULES, cfg: CFG, open_positions: 0, breaker: {} } }],
  });
  assert.strictEqual(out[0].json.approved, false);
  assert.ok(out[0].json.reasons.includes('MIROFISH_HIGH_EVENT_RISK_VETO'));
  assert.strictEqual(out[0].json.size, 0);
});

test('gerbang risiko menolak stop yang melewati buffer likuidasi 50x', async () => {
  const wf = loadWorkflow('03-trading-loop.json');
  const sig = goodSignal();
  sig.stop = 98000;             // 2% -> melewati jarak likuidasi 1.499%
  sig.take_profit = 106000;
  sig.stats = { win_rate: 0.62, avg_win_pct: 0.06, avg_loss_pct: 0.02 };
  const out = await runNode(codeNode(wf, 'GERBANG RISIKO (default DENY)'), {
    items: [{ json: { signal: sig, equity: 1000, entry: 100000,
                      rules: RULES, cfg: CFG, open_positions: 0, breaker: {} } }],
  });
  assert.strictEqual(out[0].json.approved, false);
  assert.ok(out[0].json.reasons.some((r) => r.startsWith('STOP_TOO_FAR')),
    JSON.stringify(out[0].json.reasons));
});

test('pembangun order menghasilkan body valid + clientOrderId legal + tanda tangan', async () => {
  const wf = loadWorkflow('03-trading-loop.json');
  const decided = await runNode(codeNode(wf, 'GERBANG RISIKO (default DENY)'), {
    items: [{ json: { signal: goodSignal(), equity: 1000, entry: 100000,
                      rules: RULES, cfg: CFG, open_positions: 0, breaker: {} } }],
  });
  const out = await runNode(codeNode(wf, 'Bangun order + tanda tangan'), {
    items: decided, env: { PIONEX_API_SECRET: 'secret-uji' },
  });

  const j = out[0].json;
  assert.strictEqual(j.send, true);
  const body = JSON.parse(j.body);
  assert.strictEqual(body.symbol, 'BTC_USDT_PERP');
  assert.strictEqual(body.side, 'BUY');
  assert.strictEqual(body.size, '0.0058');
  assert.strictEqual(body.reduceOnly, false);
  assert.match(body.clientOrderId, /^pg-ENTRY-[0-9a-f]{24}$/);
  assert.match(j.signature, /^[0-9a-f]{64}$/);
});

test('pembangun order tidak mengirim apa pun bila gerbang menolak', async () => {
  const wf = loadWorkflow('03-trading-loop.json');
  const out = await runNode(codeNode(wf, 'Bangun order + tanda tangan'), {
    items: [{ json: { approved: false, reasons: ['DIRECTION_NONE'], signal: {}, rules: RULES } }],
    env: { PIONEX_API_SECRET: 'x' },
  });
  assert.strictEqual(out[0].json.send, false);
  assert.strictEqual(out[0].json.body, undefined);
});

test('monitor posisi: trailing stop hanya naik untuk LONG dan memicu exit', async () => {
  const wf = loadWorkflow('04-position-monitor.json');
  const code = codeNode(wf, 'Hitung trailing & exit');
  const base = {
    position: { positionId: 'p1', symbol: 'BTC_USDT_PERP', netSize: 0.0058,
                markPrice: '101000', leverage: '50', isolatedMode: 'ISOLATED_BOTH' },
    atr: 300, whitelist: ['BTC_USDT_PERP'], cfg: CFG,
    state: { stop: 99200, tp: 101640, highest: 100000, lowest: 0, opened_at: NOW_ISO() },
  };

  const firstRes = await runNode(code, { items: [{ json: structuredClone(base) }] });
  const first = firstRes[0].json;
  assert.strictEqual(first.reason, 'HOLD');
  assert.ok(first.stop > 99200, 'stop harus terangkat');

  const secondRes = await runNode(code, {
    items: [{ json: { ...structuredClone(base),
                      position: { ...base.position, markPrice: '100200' },
                      state: { ...base.state, stop: first.stop,
                               highest: first.state.highest } } }],
  });
  assert.strictEqual(secondRes[0].json.stop, first.stop, 'stop tidak boleh turun');

  const stoppedRes = await runNode(code, {
    items: [{ json: { ...structuredClone(base),
                      position: { ...base.position, markPrice: '99000' },
                      state: { ...base.state, stop: first.stop } } }],
  });
  assert.strictEqual(stoppedRes[0].json.reason, 'STOP_LOSS');
  assert.strictEqual(stoppedRes[0].json.exit, true);
});

test('monitor posisi: leverage salah atau mode bukan isolated -> ANOMALY', async () => {
  const wf = loadWorkflow('04-position-monitor.json');
  const out = await runNode(codeNode(wf, 'Hitung trailing & exit'), {
    items: [{ json: {
      position: { positionId: 'p2', symbol: 'ETH_USDT_PERP', netSize: 1,
                  markPrice: '3000', leverage: '20', isolatedMode: 'CROSS' },
      atr: 10, whitelist: ['BTC_USDT_PERP'], cfg: CFG,
      state: { stop: 2900, tp: 3200, highest: 3000, lowest: 0, opened_at: NOW_ISO() },
    } }],
  });
  const j = out[0].json;
  assert.ok(j.reason.startsWith('ANOMALY:'), j.reason);
  assert.ok(j.reason.includes('LEVERAGE_20'), j.reason);
  assert.ok(j.reason.includes('NOT_ISOLATED'), j.reason);
  assert.ok(j.reason.includes('SYMBOL_NOT_WHITELISTED'), j.reason);
  assert.strictEqual(j.exit, true);
});

test('monitor posisi: time stop setelah max_hold_hours', async () => {
  const wf = loadWorkflow('04-position-monitor.json');
  const old = new Date(Date.now() - 25 * 3600 * 1000).toISOString();
  const out = await runNode(codeNode(wf, 'Hitung trailing & exit'), {
    items: [{ json: {
      position: { positionId: 'p3', symbol: 'BTC_USDT_PERP', netSize: 0.0058,
                  markPrice: '100000', leverage: '50', isolatedMode: 'ISOLATED_BOTH' },
      atr: 0, whitelist: ['BTC_USDT_PERP'], cfg: CFG,
      state: { stop: 99200, tp: 101640, highest: 100000, lowest: 0, opened_at: old },
    } }],
  });
  assert.strictEqual(out[0].json.reason, 'TIME_STOP');
});

test('adapter verdict di 06-mirofish-sweep menolak verdict tanpa confidence', async () => {
  const wf = loadWorkflow('06-mirofish-sweep.json');
  const out = await runNode(codeNode(wf, 'Adapter verdict'), {
    items: [{ json: {
      run_id: 'r9',
      manifest: { run_id: 'r9', created_at: '2026-09-13T08:00:00Z' },
      verdict: { prediction: 'Pasar diperkirakan bergerak normal tanpa pemicu besar hari ini.',
                 key_dynamics: [], signals: [] },
    } }],
  });
  const e = out[0].json;
  assert.strictEqual(e.schema_ok, false);
  assert.strictEqual(e.event_risk, 'HIGH');
});

test('adapter verdict di 06-mirofish-sweep menerima verdict sehat', async () => {
  const wf = loadWorkflow('06-mirofish-sweep.json');
  const out = await runNode(codeNode(wf, 'Adapter verdict'), {
    items: [{ json: {
      run_id: 'r10',
      manifest: { run_id: 'r10', created_at: '2026-09-13T08:00:00Z' },
      verdict: { prediction: 'Tidak ada pemicu besar; sentimen pasar cenderung stabil.',
                 confidence: 0.66, key_dynamics: ['Volume normal'], signals: [] },
    } }],
  });
  const e = out[0].json;
  assert.strictEqual(e.schema_ok, true);
  assert.strictEqual(e.event_risk, 'LOW');
  assert.strictEqual(e.bias, 'NEUTRAL');
  assert.strictEqual(e.confidence, 0.66);
});

test('kill switch menandatangani DELETE allOrders', async () => {
  const wf = loadWorkflow('05-kill-switch.json');
  const out = await runNode(codeNode(wf, 'Tanda tangan cancel-all'), {
    items: [{}], env: { PIONEX_API_SECRET: 's', PIONEX_SYMBOL: 'BTC_USDT_PERP' },
  });
  const j = out[0].json;
  assert.strictEqual(j.path, '/uapi/v1/trade/allOrders');
  assert.match(j.signature, /^[0-9a-f]{64}$/);
  assert.strictEqual(JSON.parse(j.body).symbol, 'BTC_USDT_PERP');
  // URL yang dikirim harus memuat timestamp yang SAMA dengan yang ditandatangani.
  assert.strictEqual(j.url,
    `https://api.pionex.com/uapi/v1/trade/allOrders?timestamp=${j.timestamp}`);
});

test('preflight menandai heartbeat basi dan leverage salah', async () => {
  const wf = loadWorkflow('01-preflight-watchdog.json');
  const out = await runNode(codeNode(wf, 'Periksa setelan akun'), {
    items: [{ json: {
      cfg: CFG,
      leverage: { leverage: 20 },
      isolated_mode: { isolatedMode: 'CROSS' },
      position_mode: { positionMode: 'OPENCLOSE' },
      risk_table: { maxLeverage: 100, maintMarginRatio: 0.005 },
      last_heartbeat: new Date(Date.now() - 300000).toISOString(),
    } }],
  });
  const j = out[0].json;
  assert.strictEqual(j.healthy, false);
  assert.ok(j.problems.some((p) => p.startsWith('LEVERAGE_MISMATCH')), j.problems);
  assert.ok(j.problems.some((p) => p.startsWith('MARGIN_MODE')), j.problems);
  assert.ok(j.problems.some((p) => p.startsWith('POSITION_MODE')), j.problems);
  assert.ok(j.problems.some((p) => p.startsWith('HEARTBEAT_STALE')), j.problems);
});

test('preflight sehat bila setelan cocok dan heartbeat segar', async () => {
  const wf = loadWorkflow('01-preflight-watchdog.json');
  const out = await runNode(codeNode(wf, 'Periksa setelan akun'), {
    items: [{ json: {
      cfg: CFG,
      leverage: { leverage: 50 },
      isolated_mode: { isolatedMode: 'ISOLATED_BOTH' },
      position_mode: { positionMode: 'BUYSELL' },
      risk_table: { maxLeverage: 100, maintMarginRatio: 0.005 },
      last_heartbeat: new Date().toISOString(),
    } }],
  });
  assert.strictEqual(out[0].json.healthy, true, JSON.stringify(out[0].json.problems));
});

test('breaker di preflight trip pada rugi harian 3%', async () => {
  const wf = loadWorkflow('01-preflight-watchdog.json');
  const out = await runNode(codeNode(wf, 'Gabung & hitung breaker'), {
    items: [{ json: { cfg: CFG, day_start_equity: 1000, equity: 965,
                      peak_equity: 1000, consecutive_losses: 1 } }],
  });
  assert.strictEqual(out[0].json.tripped, true);
  assert.ok(out[0].json.reason.startsWith('DAILY_LOSS'));
});

test('permintaan baca akun ditandatangani per-permintaan, bukan dari env statis', async () => {
  const wf = loadWorkflow('01-preflight-watchdog.json');
  const sign = codeNode(wf, 'Tanda tangan baca akun');
  assert.ok(!sign.includes('PIONEX_STATIC_READ_SIGNATURE'),
    'tanda tangan statis tidak boleh ada: kedaluwarsa dalam 20 detik');

  const out = await runNode(sign, {
    items: [{}], env: { PIONEX_API_SECRET: 'secret-uji', PIONEX_SYMBOL: 'BTC_USDT_PERP' },
  });
  const reqs = Array.from(out.map((x) => x.json.req));
  assert.deepStrictEqual(reqs, ['leverage', 'isolated_mode', 'position_mode', 'balances']);

  for (const { json: j } of out) {
    assert.match(j.signature, /^[0-9a-f]{64}$/, `${j.req}: tanda tangan harus HMAC-SHA256 hex`);
    assert.match(j.url, /^https:\/\/api\.pionex\.com\/uapi\/v1\//, j.url);
    assert.ok(j.url.includes(`timestamp=${j.timestamp}`),
      `${j.req}: timestamp yang ditandatangani harus sama dengan yang dikirim`);
    assert.ok(Date.now() - j.timestamp < 60000, 'timestamp harus baru');
    // query tersortir alfabetis, sesuai spec autentikasi Pionex
    const qs = Array.from(j.url.split('?')[1].split('&').map((kv) => kv.split('=')[0]));
    assert.deepStrictEqual(qs, Array.from(qs).sort(), `${j.req}: query harus tersortir`);
  }
});

test('tidak ada workflow yang memakai tanda tangan statis atau template rusak', () => {
  for (const file of fs.readdirSync(WF_DIR).filter((f) => f.endsWith('.json'))) {
    const raw = fs.readFileSync(path.join(WF_DIR, file), 'utf8');
    assert.ok(!raw.includes('PIONEX_STATIC_READ_SIGNATURE'), file);
    assert.ok(!raw.includes('timestamp=='), `${file}: 'timestamp==' adalah typo query`);
    // '{ $json.x }' (satu kurung kurawal) tidak dievaluasi n8n; '{{ $json.x }}' sah.
    assert.ok(!/(?<!\{)\{ \$json\./.test(raw),
      `${file}: '{ $json.x }' satu kurung kurawal tidak dievaluasi n8n`);
    assert.ok(!/(?<!\{)\{ \$env\./.test(raw),
      `${file}: '{ $env.x }' satu kurung kurawal tidak dievaluasi n8n`);
    // URL node HTTP: template n8n di dalamnya harus diapit dua kurung kurawal.
    const wf = JSON.parse(raw);
    for (const n of wf.nodes.filter((x) => x.type === 'n8n-nodes-base.httpRequest')) {
      const url = n.parameters.url || '';
      assert.ok(!/(?<!\{)\{[^{]*\$json\.[^}]*\}(?!\})/.test(url),
        `${file} :: ${n.name}: URL memakai template satu kurung kurawal -> ${url}`);
      assert.ok(!url.includes('=='), `${file} :: ${n.name}: '==' di URL adalah typo query`);
    }
  }
});

test('preflight tetap halt setelah restart bila bot di-halt manual', async () => {
  const wf = loadWorkflow('01-preflight-watchdog.json');
  const out = await runNode(codeNode(wf, 'Gabung & hitung breaker'), {
    items: [{ json: { cfg: CFG, halted: true, halt_reason: 'MANUAL_KILL',
                      day_start_equity: 1000, equity: 1000,
                      peak_equity: 1000, consecutive_losses: 0 } }],
  });
  assert.strictEqual(out[0].json.tripped, true);
  assert.ok(out[0].json.reason.startsWith('HALTED'), out[0].json.reason);
});

test('cek idempotensi ditandatangani dengan clientOrderId yang sama', async () => {
  const wf = loadWorkflow('03-trading-loop.json');
  const out = await runNode(codeNode(wf, 'Tanda tangan cek idempotensi'), {
    items: [{ json: { clientOrderId: 'pg-ENTRY-0123456789abcdef01234567',
                      signal: { id: 'sig-1' } } }],
    env: { PIONEX_API_SECRET: 's', PIONEX_SYMBOL: 'BTC_USDT_PERP' },
  });
  const j = out[0].json;
  assert.match(j.signature, /^[0-9a-f]{64}$/);
  assert.ok(j.url.includes('clientOrderId=pg-ENTRY-0123456789abcdef01234567'), j.url);
  assert.ok(j.url.includes('symbol=BTC_USDT_PERP'), j.url);
});
