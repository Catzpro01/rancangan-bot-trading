"""Generator workflow n8n.

    python tools/make_n8n_workflows.py     # tulis n8n/workflows/*.json
    python tools/validate_workflows.py     # periksa hasilnya

Alasan dibuat generator (bukan JSON tulis tangan):
  * Kode guard di dalam Code node diambil LANGSUNG dari `n8n/code/risk_guard.js`,
    sehingga tidak ada salinan yang bisa basi.
  * Struktur node/koneksi seragam dan bisa divalidasi ulang setiap kali berubah.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "n8n" / "workflows"

GUARD_SRC = (ROOT / "n8n" / "code" / "risk_guard.js").read_text(encoding="utf-8")
ADAPTER_SRC = (ROOT / "n8n" / "code" / "mirofish_adapter.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# potongan kode untuk Code node
# --------------------------------------------------------------------------------------


def guard_body(entry: str) -> str:
    """Isi `risk_guard.js` tanpa header/ekspor, ditambah blok entry n8n."""
    lines = GUARD_SRC.splitlines()
    # buang blok module.exports {...}; sampai baris penutup
    start = next(i for i, l in enumerate(lines) if l.startswith("module.exports"))
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "};")
    body = "\n".join(lines[:start] + lines[end + 1 :]).rstrip()
    # buang komentar penutup tentang n8n agar tidak dobel
    return body + "\n\n" + entry.strip() + "\n"


ENTRY_VALIDATE = """
// ---- entry point n8n: validasi satu sinyal ------------------------------------------
const out = [];
for (const item of items) {
  const j = item.json;
  const decision = validateSignal(
    j.signal, Number(j.equity), Number(j.entry), j.rules, j.cfg,
    { openPositions: Number(j.open_positions || 0), breaker: j.breaker || {}, now: Date.now() },
  );
  out.push({ json: { ...decision, signal: j.signal, rules: j.rules, cfg: j.cfg,
                     equity: Number(j.equity), entry: Number(j.entry) } });
}
return out;
"""

ENTRY_BUILD_ORDER = """
// ---- entry point n8n: bangun body order + tanda tangan ------------------------------
const out = [];
for (const item of items) {
  const j = item.json;
  if (!j.approved) { out.push({ json: { ...j, send: false } }); continue; }

  const side = j.signal.direction === 'LONG' ? 'BUY' : 'SELL';
  const clientOrderId = buildClientOrderId(j.signal.id || j.signal.ts, j.rules.symbol, 'ENTRY');
  const body = JSON.stringify({
    clientOrderId,
    symbol: j.rules.symbol,
    positionSide: 'BOTH',
    side,
    type: 'LIMIT',
    size: String(roundStep(j.size, j.rules.base_step)),
    price: String(roundStep(j.entry, j.rules.quote_step)),
    reduceOnly: false,
  });
  const path = '/uapi/v1/trade/order';
  const timestamp = Date.now();
  const signature = signPionex('POST', path, { timestamp }, body, $env.PIONEX_API_SECRET);
  const base = $env.PIONEX_BASE_URL || 'https://api.pionex.com';

  out.push({ json: { ...j, send: true, clientOrderId, body, timestamp, signature,
                     url: `${base}${path}?timestamp=${timestamp}`,
                     request_hash: require('crypto').createHash('sha256').update(body).digest('hex') } });
}
return out;
"""

ENTRY_MONITOR = """
// ---- entry point n8n: trailing stop + keputusan exit --------------------------------
// Dua input: posisi dari bursa (satu respons berisi banyak posisi) dan baris
// open_position dari DB. Keduanya digabung di sini berdasarkan bentuknya, dan
// setiap posisi diproses sendiri-sendiri.
const DEFAULT_CFG = { leverage: 50, max_hold_hours: 24 };

const merged = { positions: [], stateByPos: {}, cfg: DEFAULT_CFG, atr: 0, whitelist: [] };
for (const item of items) {
  const j = item.json || {};
  if (j.__node) continue;

  if (j.position) {                       // mode siap-pakai: satu record per posisi
    merged.positions.push({ position: j.position, state: j.state || {},
                            atr: Number(j.atr || 0), cfg: j.cfg || merged.cfg,
                            whitelist: j.whitelist || merged.whitelist });
    continue;
  }
  const arr = j?.data?.positions;
  if (Array.isArray(arr)) {               // respons GET /uapi/v1/account/positions
    for (const p of arr) merged.positions.push({ position: p });
    continue;
  }
  if (j.position_id !== undefined) {      // baris open_position dari DB
    merged.stateByPos[j.position_id] = j.state || {};
    continue;
  }
  if (j.payload !== undefined) {          // snapshot pasar (atr, whitelist)
    merged.atr = Number(j.payload.atr_1m || 0);
    if (Array.isArray(j.payload.whitelist)) merged.whitelist = j.payload.whitelist;
    continue;
  }
  if (j.cfg) merged.cfg = j.cfg;
  if (Array.isArray(j.whitelist)) merged.whitelist = j.whitelist;
}
if (!merged.whitelist.length && $env.PIONEX_SYMBOL) merged.whitelist = [$env.PIONEX_SYMBOL];

const out = [];
for (const rec of merged.positions) {
  const pos = rec.position;
  const mark = Number(pos.markPrice);
  const atr = Number(rec.atr || merged.atr || 0);
  const cfg = rec.cfg || merged.cfg;
  const whitelist = rec.whitelist && rec.whitelist.length ? rec.whitelist : merged.whitelist;
  const side = Number(pos.netSize) >= 0 ? 'LONG' : 'SHORT';
  const state = rec.state && Object.keys(rec.state).length
    ? rec.state : (merged.stateByPos[pos.positionId] || {});

  let stop = Number(state.stop);
  // Trailing hanya boleh MENGGERAKKAN stop yang sudah ada. Bila state posisi tidak
  // ditemukan, menghitung stop baru berarti mengarang pengaman yang tidak pernah
  // ditetapkan gerbang risiko -- posisi seperti itu harus diratakan, bukan dijaga.
  if (atr > 0 && Number.isFinite(stop)) {
    if (side === 'LONG') {
      const highest = Math.max(Number(state.highest || mark), mark);
      const candidate = highest - atr * 2;
      if (Number.isNaN(stop) || candidate > stop) stop = candidate;
      state.highest = highest;
    } else {
      const lowest = Number(state.lowest) > 0 ? Math.min(Number(state.lowest), mark) : mark;
      const candidate = lowest + atr * 2;
      if (candidate > 0 && (Number.isNaN(stop) || candidate < stop)) stop = candidate;
      state.lowest = lowest;
    }
  }

  const heldH = state.opened_at ? (Date.now() - Date.parse(state.opened_at)) / 3600000 : 0;
  let reason = 'HOLD';
  if (side === 'LONG') {
    if (mark <= stop) reason = 'STOP_LOSS';
    else if (mark >= Number(state.tp)) reason = 'TAKE_PROFIT';
  } else {
    if (mark >= stop) reason = 'STOP_LOSS';
    else if (mark <= Number(state.tp)) reason = 'TAKE_PROFIT';
  }
  if (reason === 'HOLD' && heldH >= Number(cfg.max_hold_hours)) reason = 'TIME_STOP';

  // Anomali = ratakan, bukan peringatkan. Setelan yang tidak cocok berarti posisi ini
  // tidak berada di bawah asumsi risiko yang dihitung gerbang.
  const anomalies = [];
  if (Number(pos.leverage) !== Number(cfg.leverage)) anomalies.push(`LEVERAGE_${pos.leverage}`);
  if (!String(pos.isolatedMode || '').startsWith('ISOLATED')) anomalies.push('NOT_ISOLATED');
  if (whitelist.length && !whitelist.includes(pos.symbol)) anomalies.push('SYMBOL_NOT_WHITELISTED');
  if (Number.isNaN(stop)) anomalies.push('STOP_UNKNOWN');
  if (anomalies.length) reason = 'ANOMALY:' + anomalies.join(',');

  out.push({ json: { ...rec, position: pos, cfg, atr, whitelist, state,
                     side, mark, stop: Number.isFinite(stop) ? stop : 0,
                     reason, exit: reason !== 'HOLD' } });
}
return out;
"""

ENTRY_KILLSWITCH = """
// ---- entry point n8n: tanda tangan pembatalan semua order ---------------------------
const timestamp = Date.now();
const path = '/uapi/v1/trade/allOrders';
const body = JSON.stringify({ symbol: $env.PIONEX_SYMBOL || 'BTC_USDT_PERP' });
const signature = signPionex('DELETE', path, { timestamp }, body, $env.PIONEX_API_SECRET);
const base = $env.PIONEX_BASE_URL || 'https://api.pionex.com';
return [{ json: { path, timestamp, body, signature, url: `${base}${path}?timestamp=${timestamp}`,
                  killed_at: new Date().toISOString() } }];
"""

ENTRY_SIGN_CHECK = """
// ---- entry point n8n: tanda tangan pemeriksaan idempotensi --------------------------
// Setelah POST order, kebenaran TIDAK boleh diasumsikan dari timeout. Order dicek
// ulang lewat clientOrderId; permintaan itu juga bertanda tangan, jadi dihitung di sini.
const out = [];
for (const item of items) {
  const j = item.json;
  if (!j.clientOrderId) continue;
  const path = '/uapi/v1/trade/orderByClientOrderId';
  const query = {
    clientOrderId: j.clientOrderId,
    symbol: $env.PIONEX_SYMBOL || (j.signal && j.signal.symbol) || '',
    timestamp: Date.now(),
  };
  const signature = signPionex('GET', path, query, null, $env.PIONEX_API_SECRET);
  const qs = Object.keys(query).sort().map((k) => `${k}=${query[k]}`).join('&');
  const base = $env.PIONEX_BASE_URL || 'https://api.pionex.com';
  out.push({ json: { ...j, path, query, url: `${base}${path}?${qs}`, signature } });
}
return out;
"""

ENTRY_ADAPTER = """
// ---- entry point n8n: verdict mentah -> envelope terstandar --------------------------
const out = [];
for (const item of items) {
  out.push({ json: toEnvelope(item.json) });
}
return out;
"""

ENTRY_BREAKER = """
// ---- entry point n8n: gabung cabang + circuit breaker -----------------------------
// Node ini punya TIGA input: baris bot_state, empat respons akun, dan risk table.
// Cara n8n menggabungkan beberapa cabang menjadi satu item tidak diasumsikan di sini:
// setiap cabang dikenali dari bentuk datanya, lalu digabung sendiri. Alasannya
// praktis -- bila asumsi penggabungan salah, node ini akan membaca equity 0 dan
// berhenti dengan DAY_START_EQUITY_INVALID, yaitu kegagalan yang terlihat seperti
// "data belum ada" padahal penyebabnya struktural.
const out = [];
let merged = {};

function pickEquity(json) {
  if (Number.isFinite(Number(json.equity)) && Number(json.equity) > 0) return Number(json.equity);
  const arr = json?.data?.balances || json?.balances;
  if (!Array.isArray(arr)) return 0;
  let total = 0;
  for (const b of arr) {
    const type = String(b.type || '').toUpperCase();
    if (type && type !== 'PERP' && type !== 'FUTURES') continue;
    total += Number(b.availableBalance ?? b.balance ?? b.equity ?? 0);
  }
  return total;
}

let equity = 0;
for (const item of items) {
  const j = item.json || {};
  if (j.__node) continue;                      // metadata internal n8n

  if (j.req) {                                 // respons akun bertanda tangan
    merged[j.req] = j.data !== undefined ? j.data : j;
    if (j.req === 'balances') equity = pickEquity(merged.balances);
  } else if (j.day_start_equity !== undefined || j.cfg !== undefined || j.halted !== undefined) {
    // Baris bot_state.
    Object.assign(merged, j);
    // Ekuitas dibaca SETELAH Object.assign: baris bot_state tidak punya kolom equity,
    // jadi item yang membawa equity sekaligus cfg (bentuk masukan langsung) tetap
    // utuh, dan saldo dari cabang 'balances' tidak tertimpa.
    if (Number.isFinite(Number(j.equity)) && Number(j.equity) > 0) equity = Number(j.equity);
  } else if (j.data && (j.data.length !== undefined || j.data.rows)) {
    merged.risk_table = (j.data.rows || j.data)[0] || {};
  } else if (j.maxLeverage || j.maintMarginRatio) {
    merged.risk_table = j;
  }
  // cabang lain (heartbeat, dsb.) sengaja tidak ikut: bot_state adalah sumbernya.
}

const cfg = merged.cfg || {};
merged.equity = equity;
const dayStart = Number(merged.day_start_equity || 0);
const peak = Number(merged.peak_equity || equity);
const consec = Number(merged.consecutive_losses || 0);

if (!(dayStart > 0)) {
  out.push({ json: { ...merged, tripped: true, reason: 'DAY_START_EQUITY_INVALID' } });
} else if (!(equity > 0)) {
  out.push({ json: { ...merged, tripped: true, reason: 'EQUITY_READ_FAILED' } });
} else {
  const dayPnl = ((equity - dayStart) / dayStart) * 100;
  const dd = peak > 0 ? ((peak - equity) / peak) * 100 : 0;

  let tripped = false, reason = '';
  if (merged.halted === true || merged.halted === 't') {
    tripped = true; reason = `HALTED ${merged.halt_reason || 'UNKNOWN'}`;
  } else if (dayPnl <= -cfg.max_daily_loss_pct) {
    tripped = true; reason = `DAILY_LOSS ${dayPnl.toFixed(2)}%`;
  } else if (dd >= cfg.max_drawdown_pct) {
    tripped = true; reason = `DRAWDOWN ${dd.toFixed(2)}%`;
  } else if (consec >= cfg.max_consecutive_losses) {
    tripped = true; reason = `CONSEC_LOSSES ${consec}`;
  }

  out.push({ json: { ...merged, tripped, reason, day_pnl_pct: dayPnl, drawdown_pct: dd,
                     cooldown_until: tripped ? Date.now() + cfg.cooldown_minutes_after_trip * 60000 : null } });
}
return out;
"""

ENTRY_PREFLIGHT_CHECK = """
// ---- entry point n8n: verifikasi setelan akun + kesegaran heartbeat -------------------
const out = [];
for (const item of items) {
  const j = item.json;
  const problems = [];

  const lev = Number(j.leverage?.leverage ?? j.leverage);
  if (lev !== Number(j.cfg.leverage)) problems.push(`LEVERAGE_MISMATCH ${lev}!=${j.cfg.leverage}`);

  const mode = String(j.isolated_mode?.isolatedMode ?? j.isolated_mode ?? '');
  if (!mode.startsWith('ISOLATED')) problems.push(`MARGIN_MODE ${mode || 'UNKNOWN'}`);

  const posMode = String(j.position_mode?.positionMode ?? j.position_mode ?? '');
  if (posMode && posMode !== 'BUYSELL') problems.push(`POSITION_MODE ${posMode}`);

  const heartbeatAge = j.last_heartbeat ? (Date.now() - Date.parse(j.last_heartbeat)) / 1000 : Infinity;
  if (heartbeatAge > 90) problems.push(`HEARTBEAT_STALE ${heartbeatAge.toFixed(0)}s`);

  const riskTable = j.risk_table || {};
  if (Number(riskTable.maxLeverage || 0) < Number(j.cfg.leverage)) {
    problems.push(`RISK_TABLE_MAX_LEVERAGE ${riskTable.maxLeverage}`);
  }
  if (!riskTable.maintMarginRatio) problems.push('RISK_TABLE_MISSING_MMR');

  out.push({ json: { ...j, healthy: problems.length === 0, problems } });
}
return out;
"""


def adapter_body() -> str:
    # Buang baris module.exports (satu baris) dan semua komentar penutup setelahnya.
    lines = ADAPTER_SRC.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("module.exports"))
    body = "\n".join(lines[:start]).rstrip()
    return body + "\n\n" + ENTRY_ADAPTER.strip() + "\n"


# --------------------------------------------------------------------------------------
# pembangun node
# --------------------------------------------------------------------------------------


class Flow:
    """Kumpulan node + koneksi.

    ID node dibuat DETERMINISTIK dari (nama workflow, nama node) memakai sha256, bukan
    uuid4 acak. Alasannya: CI memverifikasi bahwa berkas di repo masih cocok dengan
    keluaran generator; ID acak akan membuat pemeriksaan itu selalu gagal.
    """

    def __init__(self, name: str):
        self.name = name
        self.nodes: list[dict] = []
        self.connections: dict[str, dict] = {}

    def node_id(self, node_name: str, kind: str = "node") -> str:
        digest = hashlib.sha256(f"{self.name}|{kind}|{node_name}".encode()).hexdigest()
        return str(uuid.UUID(digest[:32]))

    def add(self, node: dict) -> str:
        node["id"] = self.node_id(node["name"])
        if "webhookId" in node:
            node["webhookId"] = self.node_id(node["name"], "webhook")
        self.nodes.append(node)
        return node["name"]

    def link(self, src: str, dst: str, src_type: str = "main", src_index: int = 0):
        self.connections.setdefault(src, {}).setdefault(src_type, [])
        while len(self.connections[src][src_type]) <= src_index:
            self.connections[src][src_type].append([])
        self.connections[src][src_type][src_index].append({"node": dst, "type": src_type, "index": 0})

    def json(self) -> dict:
        return {
            "name": self.name,
            "nodes": self.nodes,
            "connections": self.connections,
            "active": False,
            "settings": {"executionOrder": "v1", "timezone": "UTC", "saveExecutionProgress": True},
            "pinData": {},
            "tags": [{"name": "pionex-guard-50x"}],
        }


def schedule(name: str, rule: dict) -> dict:
    return {
        "parameters": {"rule": {"interval": [rule]}},
        "id": str(uuid.uuid4()), "name": name, "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2, "position": [-900, 0],
    }


def webhook(name: str, path: str, method: str = "POST") -> dict:
    return {
        "parameters": {"httpMethod": method, "path": path, "responseMode": "onReceived"},
        "id": str(uuid.uuid4()), "name": name, "type": "n8n-nodes-base.webhook",
        "typeVersion": 2, "position": [-900, 0], "webhookId": str(uuid.uuid4()),
    }


def code(name: str, js: str, position=(-400, 0)) -> dict:
    return {
        "parameters": {"mode": "runOnceForAllItems", "jsCode": js},
        "id": str(uuid.uuid4()), "name": name, "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": list(position),
    }


def http(name: str, method: str, url: str, position=(0, 0), body: str | None = None,
         headers: dict | None = None, on_error: str = "continueRegularOutput") -> dict:
    params: dict = {
        "method": method,
        "url": url,
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": k, "value": v} for k, v in (headers or {}).items()]},
        "options": {"response": {"response": {"neverError": True}}, "timeout": 10000},
        "onError": on_error,
    }
    if body is not None:
        params["sendBody"] = True
        params["specifyBody"] = "json"
        params["jsonBody"] = body
    return {
        "parameters": params, "id": str(uuid.uuid4()), "name": name,
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": list(position),
    }


def postgres(name: str, operation: str, query: str, position=(200, 0)) -> dict:
    return {
        "parameters": {
            "operation": operation,
            "query": query,
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": name, "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.5, "position": list(position),
        "credentials": {"postgres": {"id": "REPLACE_ME", "name": "Pionex Guard - Postgres"}},
    }


def if_node(name: str, conditions: list[dict], position=(400, 0)) -> dict:
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "typeValidation": "strict", "version": 2},
                "conditions": conditions,
                "combinator": "and",
            },
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": name, "type": "n8n-nodes-base.if",
        "typeVersion": 2.2, "position": list(position),
    }


def cond(var: str, op: str, value) -> dict:
    t = "boolean" if isinstance(value, bool) else ("number" if isinstance(value, (int, float)) else "string")
    return {"leftValue": var, "rightValue": value, "operator": {"type": t, "operation": op}}


def noop(name: str, position=(700, 0)) -> dict:
    return {
        "parameters": {}, "id": str(uuid.uuid4()), "name": name,
        "type": "n8n-nodes-base.noOp", "typeVersion": 1, "position": list(position),
    }


def notify(name: str, position=(700, 200)) -> dict:
    return {
        "parameters": {
            "sendTo": "={{ $env.ALERT_EMAIL }}",
            "subject": "=Pionex Guard: {{ $json.reason || $json.problems || 'alert' }}",
            "text": "={{ JSON.stringify($json) }}",
            "options": {},
        },
        "id": str(uuid.uuid4()), "name": name, "type": "n8n-nodes-base.emailSend",
        "typeVersion": 2.1, "position": list(position),
        "credentials": {"smtp": {"id": "REPLACE_ME", "name": "Pionex Guard - SMTP"}},
        "onError": "continueRegularOutput",
    }


# ---------------------------------------------------------------------------
# Node asli n8n. Dipakai sebanyak mungkin agar jumlah Code node tetap sedikit:
# semakin banyak kode yang ditulis tangan, semakin banyak tempat untuk salah.
# ---------------------------------------------------------------------------


def redis_set(name: str, key: str, value: str, ttl: int | None = None,
              position=(0, 0)) -> dict:
    params: dict = {"operation": "set", "key": key, "value": value,
                    "keyType": "automatic", "options": {}}
    if ttl:
        params["options"] = {"expire": True, "ttl": ttl}
    return {"parameters": params, "name": name, "type": "n8n-nodes-base.redis",
            "typeVersion": 1, "position": list(position),
            "credentials": {"redis": {"id": "REPLACE_ME", "name": "Pionex Guard - Redis"}}}


def redis_get(name: str, key: str, prop: str = "cache", position=(0, 0)) -> dict:
    return {"parameters": {"operation": "get", "key": key, "propertyName": prop,
                           "keyType": "automatic", "options": {}},
            "name": name, "type": "n8n-nodes-base.redis", "typeVersion": 1,
            "position": list(position),
            "credentials": {"redis": {"id": "REPLACE_ME", "name": "Pionex Guard - Redis"}}}


def rss(name: str, url: str, position=(0, 0)) -> dict:
    return {"parameters": {"url": url, "options": {}}, "name": name,
            "type": "n8n-nodes-base.rssFeedRead", "typeVersion": 1,
            "position": list(position), "onError": "continueRegularOutput"}


def aggregate(name: str, field: str = "", out_field: str = "data",
              position=(0, 0)) -> dict:
    params: dict = {"aggregate": "aggregateAllItemData", "options": {}}
    if field:
        params = {"aggregate": "aggregateIndividualFields",
                  "fieldsToAggregate": {"fieldToAggregate": [{"field": field}]},
                  "options": {}}
    params["outputField"] = out_field if out_field != "data" else params.get("outputField", "data")
    return {"parameters": params, "name": name, "type": "n8n-nodes-base.aggregate",
            "typeVersion": 1, "position": list(position)}


def set_fields(name: str, assignments: list[tuple[str, str]], position=(0, 0)) -> dict:
    return {"parameters": {
                "mode": "manual",
                "duplicateItem": False,
                "assignments": {"assignments": [
                    {"id": f"set-{i}", "name": k, "value": v,
                     "type": "string"} for i, (k, v) in enumerate(assignments)]},
                "options": {}},
            "name": name, "type": "n8n-nodes-base.set", "typeVersion": 3.4,
            "position": list(position)}


def limit_node(name: str, count: int, position=(0, 0)) -> dict:
    return {"parameters": {"maxItems": count}, "name": name,
            "type": "n8n-nodes-base.limit", "typeVersion": 1,
            "position": list(position)}


def dedupe(name: str, field: str, position=(0, 0)) -> dict:
    return {"parameters": {
                "operation": "removeDuplicates",
                "compare": "selectedFields",
                "fieldsToCompare": {"fields": [{"fieldName": field}]},
                "options": {}},
            "name": name, "type": "n8n-nodes-base.removeDuplicates",
            "typeVersion": 2, "position": list(position)}


def sort_node(name: str, field: str, position=(0, 0)) -> dict:
    return {"parameters": {
                "sortFieldsUi": {"sortField": [
                    {"fieldName": field, "order": "descending"}]}},
            "name": name, "type": "n8n-nodes-base.sort", "typeVersion": 1,
            "position": list(position)}


def merge_node(name: str, mode: str = "combine", position=(0, 0)) -> dict:
    params: dict = {"mode": mode, "options": {}}
    if mode == "combine":
        params["combineBy"] = "combineByPosition"
    return {"parameters": params, "name": name, "type": "n8n-nodes-base.merge",
            "typeVersion": 3, "position": list(position)}


def filter_node(name: str, conditions: list[dict], position=(0, 0)) -> dict:
    return {"parameters": {
                "conditions": {"options": {"caseSensitive": True,
                                           "typeValidation": "loose", "version": 2},
                               "conditions": conditions, "combinator": "and"},
                "options": {}},
            "name": name, "type": "n8n-nodes-base.filter", "typeVersion": 2.2,
            "position": list(position)}


def wait_node(name: str, seconds: int, position=(0, 0)) -> dict:
    return {"parameters": {"amount": seconds, "unit": "seconds"}, "name": name,
            "type": "n8n-nodes-base.wait", "typeVersion": 1.1,
            "position": list(position), "webhookId": ""}


BASE = "https://api.pionex.com"

# Template URL untuk HTTP Request node. PENTING: URL, timestamp, dan tanda tangan
# semuanya berasal dari SATU Code node di hulunya. Kalau URL dibangun di node HTTP
# sementara tanda tangan dihitung di tempat lain, keduanya bisa menyimpang dan bursa
# menolak dengan signature mismatch -- kegagalan yang sulit dilacak karena "kode
# terlihat benar".
URL_FROM_ITEM = "={{ $json.url }}"
SIG_HEADERS = {
    "PIONEX-KEY": "={{ $env.PIONEX_API_KEY }}",
    "PIONEX-SIGNATURE": "={{ $json.signature }}",
    "Content-Type": "application/json",
}
READ_HEADERS = {
    "PIONEX-KEY": "={{ $env.PIONEX_API_KEY }}",
    "PIONEX-SIGNATURE": "={{ $json.signature }}",
}

# n8n Code node "Run Once for All Items" menerima setiap item dari node sebelumnya.
# Jadi satu Code node penanda tangan + satu HTTP node bisa melayani N endpoint.
SIGNED_READS = """
// ---- entry point n8n: tanda tangan permintaan baca ------------------------------
// Pionex menuntut timestamp dalam rentang +-20 detik dan tanda tangan HMAC atas
// METHOD + path + query tersortir. Keduanya harus dihitung saat permintaan dibuat,
// tidak bisa disimpan di environment: tanda tangan statis akan kedaluwarsa dalam
// 20 detik dan setiap panggilan akan ditolak.
const PATHS = __PATHS__;
const out = [];
for (const p of PATHS) {
  const query = Object.assign({ timestamp: Date.now() }, p.query || {});
  const signature = signPionex(p.method || 'GET', p.path, query, null, $env.PIONEX_API_SECRET);
  const qs = Object.keys(query).sort().map((k) => `${k}=${query[k]}`).join('&');
  out.push({ json: {
    req: p.req, method: p.method || 'GET', path: p.path, query, timestamp: query.timestamp,
    url: `${$env.PIONEX_BASE_URL || 'https://api.pionex.com'}${p.path}?${qs}`,
    signature,
  } });
}
return out;
"""


def signed_reads(name: str, paths: list[tuple[str, str, dict | None]], position=(-600, 0)) -> tuple[dict, dict]:
    """Bangun pasangan (Code node penanda tangan, HTTP Request node)."""
    import json as _json
    literal = _json.dumps([{"req": r, "path": p, "query": q, "method": "GET"}
                           for r, p, q in paths], ensure_ascii=False)
    return (code(name, guard_body(SIGNED_READS.replace("__PATHS__", literal)), position),
            http(f"{name} (HTTP)", "GET", URL_FROM_ITEM, (position[0] + 220, position[1]),
                 headers=READ_HEADERS))


# --------------------------------------------------------------------------------------
# 1. Preflight & Watchdog
# --------------------------------------------------------------------------------------


def build_preflight() -> dict:
    f = Flow("Pionex Guard — 01 Preflight & Watchdog")
    f.add(schedule("Setiap 30 detik", {"field": "seconds", "secondsInterval": 30}))

    # Config + state dibaca LEBIH DULU: cfg.leverage adalah pembanding untuk setelan
    # akun, dan tanpa baris bot_state seluruh pemeriksaan tidak punya acuan.
    f.add(postgres("Baca config & state", "executeQuery",
                   "SELECT cfg, day_start_equity, peak_equity, consecutive_losses, halted, "
                   "halt_reason, last_heartbeat, last_market_beat "
                   "FROM bot_state ORDER BY id DESC LIMIT 1;", (-900, 0)))

    sign_node, http_node = signed_reads("Tanda tangan baca akun", [
        ("leverage", "/uapi/v1/account/leverage", None),
        ("isolated_mode", "/uapi/v1/trade/isolatedMode", None),
        ("position_mode", "/uapi/v1/account/positionMode", None),
        ("balances", "/uapi/v1/account/balances", None),
    ], (-700, 0))
    f.add(sign_node)
    f.add(http_node)

    # Endpoint publik: tidak perlu tanda tangan.
    f.add(http("Ambil risk table", "GET",
               "{{ $env.PIONEX_BASE_URL || 'https://api.pionex.com' }}/api/v1/common/riskTable"
               "?symbol={{ $env.PIONEX_SYMBOL }}", (-700, 300)))

    f.add(code("Gabung & hitung breaker", guard_body(ENTRY_BREAKER), (-400, 0)))
    f.add(code("Periksa setelan akun", guard_body(ENTRY_PREFLIGHT_CHECK), (-150, 0)))
    f.add(if_node("Sehat?", [cond("={{ $json.healthy }}", "true", True),
                             cond("={{ $json.tripped }}", "false", False)]))

    f.add(postgres("Perbarui state", "executeQuery",
                   "UPDATE bot_state SET peak_equity = GREATEST(peak_equity, {{ $json.equity }}), "
                   "updated_at = now() WHERE id = (SELECT max(id) FROM bot_state);", (700, -140)))

    # Jalur HALT: cancel-all adalah permintaan BERTANDA TANGAN, jadi butuh Code node
    # yang menghitung HMAC-nya saat itu juga.
    f.add(code("Tanda tangan cancel-all", guard_body(ENTRY_KILLSWITCH), (450, 200)))
    f.add(http("Batal semua order", "DELETE", URL_FROM_ITEM, (700, 200),
               body="={{ $json.body }}", headers=SIG_HEADERS))
    f.add(notify("Notifikasi HALT", (950, 200)))

    f.link("Setiap 30 detik", "Baca config & state")
    f.link("Baca config & state", "Tanda tangan baca akun")
    f.link("Tanda tangan baca akun", "Tanda tangan baca akun (HTTP)")
    f.link("Setiap 30 detik", "Ambil risk table")
    f.link("Tanda tangan baca akun (HTTP)", "Gabung & hitung breaker")
    f.link("Ambil risk table", "Gabung & hitung breaker")
    f.link("Baca config & state", "Gabung & hitung breaker")
    f.link("Gabung & hitung breaker", "Periksa setelan akun")
    f.link("Periksa setelan akun", "Sehat?")
    f.link("Sehat?", "Perbarui state", "main", 0)
    f.link("Sehat?", "Tanda tangan cancel-all", "main", 1)
    f.link("Tanda tangan cancel-all", "Batal semua order")
    f.link("Batal semua order", "Notifikasi HALT")
    return f.json()


# --------------------------------------------------------------------------------------
# 2. Market sweep
# --------------------------------------------------------------------------------------


def build_market() -> dict:
    f = Flow("Pionex Guard — 02 Market Sweep")
    f.add(schedule("Setiap 1 menit", {"field": "minutes", "minutesInterval": 1}))
    sym = "{{ $env.PIONEX_SYMBOL }}"
    f.add(http("Klines 1m", "GET", f"{BASE}/api/v1/market/klines?symbol={sym}&interval=1m&limit=60", (-700, -200)))
    f.add(http("Index & funding", "GET", f"{BASE}/api/v1/market/indexes?symbol={sym}", (-700, -60)))
    f.add(http("Depth", "GET", f"{BASE}/api/v1/market/depth?symbol={sym}&limit=200", (-700, 80)))
    f.add(http("Symbols", "GET", f"{BASE}/api/v1/common/symbols?symbols={sym}", (-700, 220)))
    f.add(http("Risk table", "GET", f"{BASE}/api/v1/common/riskTable?symbol={sym}", (-700, 360)))
    f.add(code("Susun snapshot", """
const out = [];
const byName = {};
for (const item of items) byName[item.json.__node || 'x'] = item.json;
// n8n memberi item per input; kumpulkan sederhana lalu hitung ATR(14) dari klines
const klines = items.flatMap(i => (i.json?.data?.klines) || []);
let atr = 0;
if (klines.length > 15) {
  let sum = 0;
  for (let i = klines.length - 14; i < klines.length; i++) {
    const h = Number(klines[i].high), l = Number(klines[i].low), pc = Number(klines[i - 1].close);
    sum += Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
  }
  atr = sum / 14;
}
const sym = $env.PIONEX_SYMBOL;
out.push({ json: {
  symbol: sym,
  ts: new Date().toISOString(),
  atr_1m: atr,
  klines: klines.slice(-60),
  raw: items.map(i => i.json),
}});
return out;
""", (-300, 0)))
    f.add(postgres("Simpan snapshot", "executeQuery",
                   "INSERT INTO market_snapshot (symbol, ts, payload) "
                   "VALUES ('{{ $json.symbol }}', now(), '{{ JSON.stringify($json) }}'::jsonb);", (0, 0)))
    f.add(postgres("Tulis heartbeat", "executeQuery",
                   "UPDATE bot_state SET last_market_beat = now() "
                   "WHERE id = (SELECT max(id) FROM bot_state);", (0, 140)))

    for n in ("Klines 1m", "Index & funding", "Depth", "Symbols", "Risk table"):
        f.link("Setiap 1 menit", n)
        f.link(n, "Susun snapshot")
    f.link("Susun snapshot", "Simpan snapshot")
    f.link("Susun snapshot", "Tulis heartbeat")
    return f.json()


# --------------------------------------------------------------------------------------
# 3. Trading loop
# --------------------------------------------------------------------------------------


def build_trading() -> dict:
    f = Flow("Pionex Guard — 03 Trading Loop")
    f.add(schedule("Setiap 1 menit", {"field": "minutes", "minutesInterval": 1}))

    f.add(postgres("Baca config & state", "executeQuery",
                   "SELECT cfg, day_start_equity, peak_equity, consecutive_losses, last_heartbeat "
                   "FROM bot_state ORDER BY id DESC LIMIT 1;", (-700, -200)))
    f.add(postgres("Baca verdict MiroFish", "executeQuery",
                   "SELECT * FROM mirofish_verdict ORDER BY verdict_ts DESC LIMIT 1;", (-700, -60)))
    f.add(postgres("Baca posisi terbuka", "executeQuery",
                   "SELECT count(*)::int AS n FROM open_position WHERE closed_at IS NULL;", (-700, 80)))
    f.add(postgres("Baca snapshot pasar", "executeQuery",
                   "SELECT payload FROM market_snapshot ORDER BY ts DESC LIMIT 1;", (-700, 220)))

    f.add(code("Bangun sinyal", """
// ---- entry point n8n: gabung 4 cabang lalu bangun sinyal ---------------------------
// Input datang dari empat node Postgres terpisah. Penggabungan dilakukan di sini,
// berdasarkan bentuk data tiap cabang, bukan mengandalkan cara n8n menggabungkannya.
const byKey = {};
for (const item of items) {
  const j = item.json || {};
  if (j.cfg !== undefined || j.day_start_equity !== undefined) byKey.state = j;
  else if (j.verdict_ts !== undefined || j.schema_ok !== undefined) byKey.verdict = j;
  else if (j.n !== undefined) byKey.count = j;
  else if (j.payload !== undefined) byKey.snapshot = j;
}

const state = byKey.state || {};
const cfg = state.cfg || {};
const snap = byKey.snapshot?.payload || {};
const verdict = byKey.verdict || null;
const openPositions = Number(byKey.count?.n || 0);

// Sinyal teknikal SEDERHANA dan sengaja transparan: EMA 9/21 + filter ATR.
// Ganti dengan strategi yang sudah Anda backtest sendiri; yang tidak boleh diubah
// adalah BENTUK keluarannya, karena gerbang risiko memvalidasi bentuk itu.
const klines = Array.isArray(snap.klines) ? snap.klines : [];
const closes = klines.map((k) => Number(k.close));
const ema = (arr, n) => {
  if (arr.length < n) return null;
  const k = 2 / (n + 1);
  let e = arr.slice(0, n).reduce((a, b) => a + Number(b), 0) / n;
  for (let i = n; i < arr.length; i++) e = arr[i] * k + e * (1 - k);
  return e;
};
const fast = ema(closes, 9), slow = ema(closes, 21);
const price = closes.length ? Number(closes[closes.length - 1]) : 0;
const atr = Number(snap.atr_1m || 0);

let direction = 'NONE';
if (fast && slow && price) {
  if (fast > slow && price > fast) direction = 'LONG';
  else if (fast < slow && price < fast) direction = 'SHORT';
}

// Jarak stop dibatasi plafon likuidasi; RR mengikuti min_rr_ratio config.
const room = 1 / Number(cfg.leverage || 50) - 0.005 - 0.0005;
const dLiq = (room + 0.0005) / 1.0005;
const maxStop = dLiq * (1 - Number(cfg.liq_buffer_pct || 33) / 100);
const stopDist = Math.min(Math.max((atr / (price || 1)) * 1.5, 0.002), maxStop * 0.9);
const tpDist = stopDist * Number(cfg.min_rr_ratio || 1.1);

const stop = direction === 'LONG' ? price * (1 - stopDist) : price * (1 + stopDist);
const tp = direction === 'LONG' ? price * (1 + tpDist) : price * (1 - tpDist);

// Statistik track record. Bila belum ada, semuanya nol dan gerbang akan menolak
// dengan NO_TRACK_RECORD. Itu disengaja: tanpa bukti historis, tidak ada dasar
// untuk menyatakan EV positif, dan "default DENY" berarti tidak menebak.
const stats = snap.stats || { win_rate: 0, avg_win_pct: 0, avg_loss_pct: 0 };

return [{ json: {
  signal: {
    id: `${$env.PIONEX_SYMBOL}-${new Date().toISOString().slice(0, 16)}`,
    direction, entry: price, stop, take_profit: tp,
    ts: new Date().toISOString(),
    // Baris mirofish_verdict sudah dalam bentuk amplop (schema_ok, confidence,
    // event_risk, bias, verdict_ts) -- persis yang dibaca gerbang.
    mirofish: verdict || { schema_ok: false },
    stats,
  },
  cfg, equity: Number(state.equity || snap.equity || 0), entry: price,
  rules: {
    symbol: $env.PIONEX_SYMBOL,
    base_step: Number(snap.instrument?.base_step || 0.0001),
    quote_step: Number(snap.instrument?.quote_step || 0.01),
    min_size: Number(snap.instrument?.min_size || 0),
    max_size: Number(snap.instrument?.max_size || 1e9),
    min_notional: Number(snap.instrument?.min_notional || 5),
    mmr: Number(snap.risk_table?.maint_margin_ratio || snap.risk_table?.maintMarginRatio || 0.005),
    taker_fee: 0.0005,
  },
  open_positions: openPositions,
  breaker: state.breaker || {},
}}];
""", (-400, 0)))

    f.add(code("GERBANG RISIKO (default DENY)", guard_body(ENTRY_VALIDATE), (-150, 0)))
    f.add(if_node("Disetujui?", [cond("={{ $json.approved }}", "true", True)]))

    f.add(postgres("Catat penolakan", "executeQuery",
                   "INSERT INTO trade_decision (signal_id, approved, reasons, warnings, payload) "
                   "VALUES ('{{ $json.signal.id }}', false, "
                   "'{{ JSON.stringify($json.reasons) }}'::jsonb, "
                   "'{{ JSON.stringify($json.warnings) }}'::jsonb, "
                   "'{{ JSON.stringify($json) }}'::jsonb);", (400, -160)))

    f.add(code("Bangun order + tanda tangan", guard_body(ENTRY_BUILD_ORDER), (400, 160)))
    f.add(postgres("Catat order (SENDING)", "executeQuery",
                   "INSERT INTO trade_order (client_order_id, signal_id, status, request_hash, body) "
                   "VALUES ('{{ $json.clientOrderId }}', '{{ $json.signal.id }}', 'SENDING', "
                   "'{{ $json.request_hash }}', '{{ $json.body }}'::jsonb) "
                   "ON CONFLICT (client_order_id) DO NOTHING;", (650, 60)))
    f.add(http("Kirim order", "POST", URL_FROM_ITEM, (900, 160),
               body="={{ $json.body }}", headers=SIG_HEADERS))
    f.add(code("Tanda tangan cek idempotensi", guard_body(ENTRY_SIGN_CHECK), (1150, 300)))
    f.add(http("Cek idempotensi", "GET", URL_FROM_ITEM, (1400, 300), headers=READ_HEADERS))
    f.add(postgres("Catat keputusan disetujui", "executeQuery",
                   "INSERT INTO trade_decision (signal_id, approved, reasons, warnings, payload) "
                   "VALUES ('{{ $json.signal.id }}', true, '[]'::jsonb, "
                   "'{{ JSON.stringify($json.warnings) }}'::jsonb, "
                   "'{{ JSON.stringify($json) }}'::jsonb);", (1150, 60)))
    f.add(postgres("Tulis heartbeat loop", "executeQuery",
                   "UPDATE bot_state SET last_heartbeat = now() "
                   "WHERE id = (SELECT max(id) FROM bot_state);", (1150, -60)))

    for n in ("Baca config & state", "Baca verdict MiroFish", "Baca posisi terbuka", "Baca snapshot pasar"):
        f.link("Setiap 1 menit", n)
        f.link(n, "Bangun sinyal")
    f.link("Bangun sinyal", "GERBANG RISIKO (default DENY)")
    f.link("GERBANG RISIKO (default DENY)", "Disetujui?")
    f.link("Disetujui?", "Catat penolakan", "main", 1)
    f.link("Disetujui?", "Bangun order + tanda tangan", "main", 0)
    f.link("Bangun order + tanda tangan", "Catat order (SENDING)")
    f.link("Catat order (SENDING)", "Kirim order")
    f.link("Kirim order", "Catat keputusan disetujui")
    f.link("Catat keputusan disetujui", "Tulis heartbeat loop")
    f.link("Tulis heartbeat loop", "Tanda tangan cek idempotensi")
    f.link("Tanda tangan cek idempotensi", "Cek idempotensi")
    return f.json()


# --------------------------------------------------------------------------------------
# 4. Position monitor
# --------------------------------------------------------------------------------------


def build_monitor() -> dict:
    f = Flow("Pionex Guard — 04 Position Monitor")
    f.add(schedule("Setiap 15 detik", {"field": "seconds", "secondsInterval": 15}))
    _sn, _hn = signed_reads("Tanda tangan baca posisi", [
        ("positions", "/uapi/v1/account/positions", None),
    ], (-900, 0))
    f.add(_sn)
    f.add(_hn)
    f.add(postgres("Baca state posisi", "executeQuery",
                   "SELECT * FROM open_position WHERE closed_at IS NULL;", (-700, 140)))
    f.add(code("Hitung trailing & exit", guard_body(ENTRY_MONITOR), (-350, 0)))
    f.add(if_node("Perlu exit?", [cond("={{ $json.exit }}", "true", True)]))
    f.add(code("Bangun order exit", guard_body("""
const out = [];
for (const item of items) {
  const j = item.json;
  const side = j.side === 'LONG' ? 'SELL' : 'BUY';
  const clientOrderId = buildClientOrderId(j.position.positionId || j.position.symbol,
                                           j.position.symbol, 'EXIT');
  const body = JSON.stringify({
    clientOrderId, symbol: j.position.symbol, positionSide: 'BOTH', side,
    type: 'MARKET_QTY', size: String(Math.abs(Number(j.position.netSize))), reduceOnly: true,
  });
  const timestamp = Date.now();
  out.push({ json: { ...j, clientOrderId, body, timestamp,
                     signature: signPionex('POST', '/uapi/v1/trade/order', { timestamp }, body,
                                           $env.PIONEX_API_SECRET) } });
}
return out;
"""), (400, 160)))
    f.add(http("Kirim exit", "POST", URL_FROM_ITEM, (650, 160),
               body="={{ $json.body }}", headers=SIG_HEADERS))
    f.add(postgres("Tutup posisi", "executeQuery",
                   "UPDATE open_position SET closed_at = now(), close_reason = '{{ $json.reason }}' "
                   "WHERE position_id = '{{ $json.position.positionId }}';", (900, 160)))
    f.add(postgres("Simpan stop terbaru", "executeQuery",
                   "UPDATE open_position SET stop_price = {{ $json.stop }}, "
                   "state = '{{ JSON.stringify($json.state) }}'::jsonb "
                   "WHERE position_id = '{{ $json.position.positionId }}';", (400, -140)))

    f.link("Setiap 15 detik", "Tanda tangan baca posisi")
    f.link("Tanda tangan baca posisi", "Tanda tangan baca posisi (HTTP)")
    f.link("Setiap 15 detik", "Baca state posisi")
    f.link("Tanda tangan baca posisi (HTTP)", "Hitung trailing & exit")
    f.link("Baca state posisi", "Hitung trailing & exit")
    f.link("Hitung trailing & exit", "Perlu exit?")
    f.link("Perlu exit?", "Bangun order exit", "main", 0)
    f.link("Perlu exit?", "Simpan stop terbaru", "main", 1)
    f.link("Bangun order exit", "Kirim exit")
    f.link("Kirim exit", "Tutup posisi")
    return f.json()


# --------------------------------------------------------------------------------------
# 5. Kill switch
# --------------------------------------------------------------------------------------


def build_killswitch() -> dict:
    f = Flow("Pionex Guard — 05 Kill Switch")
    f.add(webhook("Webhook kill", "pionex-guard/kill", "POST"))
    f.add(code("Tanda tangan cancel-all", guard_body(ENTRY_KILLSWITCH), (-500, 0)))
    f.add(http("Batal semua order", "DELETE", URL_FROM_ITEM, (-250, 0),
               body="={{ $json.body }}", headers=SIG_HEADERS))
    _sn2, _hn2 = signed_reads("Tanda tangan baca posisi", [
        ("positions", "/uapi/v1/account/positions", None),
    ], (-100, 0))
    f.add(_sn2)
    f.add(_hn2)
    f.add(code("Bangun order perataan", guard_body("""
const out = [];
const positions = (items[0]?.json?.data?.positions) || [];
for (const p of positions) {
  const size = Number(p.netSize);
  if (!size) continue;
  const side = size > 0 ? 'SELL' : 'BUY';
  const clientOrderId = buildClientOrderId(p.positionId || p.symbol, p.symbol, 'FLATTEN');
  const body = JSON.stringify({
    clientOrderId, symbol: p.symbol, positionSide: 'BOTH', side,
    type: 'MARKET_QTY', size: String(Math.abs(size)), reduceOnly: true,
  });
  const timestamp = Date.now();
  out.push({ json: { body, timestamp, symbol: p.symbol,
                     signature: signPionex('POST', '/uapi/v1/trade/order', { timestamp }, body,
                                           $env.PIONEX_API_SECRET) } });
}
if (!out.length) out.push({ json: { body: null, skip: true } });
return out;
"""), (250, 0)))
    f.add(if_node("Ada posisi?", [cond("={{ $json.skip }}", "false", False)]))
    f.add(http("Ratakan posisi", "POST", URL_FROM_ITEM, (500, -100),
               body="={{ $json.body }}", headers=SIG_HEADERS))
    f.add(postgres("Set HALTED", "executeQuery",
                   "UPDATE bot_state SET halted = true, halt_reason = 'MANUAL_KILL', "
                   "halted_at = now() WHERE id = (SELECT max(id) FROM bot_state);", (500, 100)))
    f.add(notify("Notifikasi kill", (750, 100)))

    f.link("Webhook kill", "Tanda tangan cancel-all")
    f.link("Tanda tangan cancel-all", "Batal semua order")
    f.link("Batal semua order", "Tanda tangan baca posisi")
    f.link("Tanda tangan baca posisi", "Tanda tangan baca posisi (HTTP)")
    f.link("Tanda tangan baca posisi (HTTP)", "Bangun order perataan")
    f.link("Bangun order perataan", "Ada posisi?")
    f.link("Ada posisi?", "Ratakan posisi", "main", 0)
    f.link("Ada posisi?", "Set HALTED", "main", 1)
    f.link("Ratakan posisi", "Set HALTED")
    f.link("Set HALTED", "Notifikasi kill")
    return f.json()


# --------------------------------------------------------------------------------------
# 6. MiroFish sweep
# --------------------------------------------------------------------------------------


def build_mirofish() -> dict:
    f = Flow("Pionex Guard — 06 MiroFish Sweep")
    f.add(schedule("Setiap 4 jam", {"field": "hours", "hoursInterval": 4}))
    runner = "={{ $env.MIROFISH_RUNNER_URL }}"
    f.add(code("Susun seed & requirement", """
const symbol = $env.PIONEX_SYMBOL;
return [{ json: {
  requirement: `Dalam 24 jam ke depan, untuk aset ${symbol}: peristiwa apa yang paling `
    + `mungkin mengguncang harga secara tiba-tiba? Nilai tingkat risiko peristiwa sebagai `
    + `LOW, MEDIUM, atau HIGH, dan sebutkan bukti serta pemicu waktu spesifiknya.`,
  files: [],
  platform: 'parallel',
  max_rounds: 10,
  symbol,
}}];
""", (-600, 0)))
    f.add(http("Kirim job", "POST", f"{runner}/jobs", (-350, 0),
               body='={{ JSON.stringify({ requirement: $json.requirement, files: $json.files, '
                    'platform: $json.platform, max_rounds: $json.max_rounds }) }}',
               headers={"Content-Type": "application/json",
                        "Authorization": "=Bearer {{ $env.MIROFISH_RUNNER_TOKEN }}"}))
    f.add(postgres("Catat job", "executeQuery",
                   "INSERT INTO mirofish_run_log (job_id, symbol, status, started_at) "
                   "VALUES ('{{ $json.data.job_id || $json.job_id }}', '{{ $env.PIONEX_SYMBOL }}', "
                   "'RUNNING', now());", (-100, 0)))
    f.add(schedule("Poll tiap 2 menit", {"field": "minutes", "minutesInterval": 2}))
    f.add(postgres("Ambil job berjalan", "executeQuery",
                   "SELECT job_id FROM mirofish_run_log WHERE status = 'RUNNING' "
                   "ORDER BY started_at LIMIT 1;", (-600, 300)))
    f.add(http("Cek status", "GET", f"{runner}/jobs/{{{{ $json.job_id }}}}", (-350, 300),
               headers={"Authorization": "=Bearer {{ $env.MIROFISH_RUNNER_TOKEN }}"}))
    f.add(if_node("Selesai?", [cond("={{ $json.status }}", "equals", "SUCCEEDED")]))
    f.add(http("Ambil verdict", "GET",
               f"{runner}/jobs/{{{{ $json.job_id }}}}/verdict", (150, 200),
               headers={"Authorization": "=Bearer {{ $env.MIROFISH_RUNNER_TOKEN }}"}))
    f.add(code("Adapter verdict", adapter_body(), (400, 200)))
    f.add(postgres("Simpan verdict", "executeQuery",
                   "INSERT INTO mirofish_verdict (run_id, verdict_ts, schema_ok, bias, confidence, "
                   "event_risk, horizon_hours, risk_score, evidence, adapter_version, raw) "
                   "VALUES ('{{ $json.run_id }}', '{{ $json.verdict_ts }}', {{ $json.schema_ok }}, "
                   "'{{ $json.bias }}', {{ $json.confidence }}, '{{ $json.event_risk }}', "
                   "{{ $json.horizon_hours }}, "
                   "{{ $json.risk_score === undefined || $json.risk_score === null ? 1 : $json.risk_score }}, "
                   "'{{ JSON.stringify($json.evidence) }}'::jsonb, "
                   "'{{ $json.adapter_version }}', '{{ JSON.stringify($json) }}'::jsonb) "
                   "ON CONFLICT (run_id) DO NOTHING;", (650, 200)))
    f.add(notify("Notifikasi veto", (650, 380)))

    f.link("Setiap 4 jam", "Susun seed & requirement")
    f.link("Susun seed & requirement", "Kirim job")
    f.link("Kirim job", "Catat job")
    f.link("Poll tiap 2 menit", "Ambil job berjalan")
    f.link("Ambil job berjalan", "Cek status")
    f.link("Cek status", "Selesai?")
    f.link("Selesai?", "Ambil verdict", "main", 0)
    f.link("Ambil verdict", "Adapter verdict")
    f.link("Adapter verdict", "Simpan verdict")
    f.link("Simpan verdict", "Notifikasi veto")
    return f.json()


# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# 7. Riset pasar & cache  —  node asli n8n saja, NOL Code node
# --------------------------------------------------------------------------------------

# PENTING: query dengan OR harus dibungkus tanda kurung. Tanpa kurung GDELT membalas
# "Queries containing OR'd terms must be surrounded by ()." -- dengan HTTP 200, bukan
# kode kesalahan, dan bukan JSON.
GDELT_Q = "%28bitcoin%20OR%20crypto%29"
GDELT_TONE = (f"https://api.gdeltproject.org/api/v2/doc/doc?query={GDELT_Q}"
              "&mode=TimelineTone&format=json&timespan=7d")
GDELT_VOL = (f"https://api.gdeltproject.org/api/v2/doc/doc?query={GDELT_Q}"
             "&mode=TimelineVol&format=json&timespan=7d")
GDELT_NEG = ("https://api.gdeltproject.org/api/v2/doc/doc?query=bitcoin%20tone%3C-5"
             "&mode=ArtList&format=json&maxrecords=25&sort=DateDesc&timespan=2d")
FNG = "https://api.alternative.me/fng/?limit=14"
STABLE_FLOW = "https://stablecoins.llama.fi/stablecoincharts/all?stablecoin=1"



# Penjaga bentuk respons GDELT. GDELT menolak permintaan dengan HTTP 200 + teks polos
# ("Please limit requests to one every 5 seconds...", "Queries containing OR'd terms..."),
# jadi status sukses TIDAK berarti datanya ada. Tanpa penjaga ini, teks teguran masuk ke
# cache sebagai "data nada berita" lalu dikirim ke agen LLM seolah-olah pasar.
GDELT_GUARD = """
// ---- entry point n8n: tolak respons GDELT yang bukan data --------------------------
const j = (items[0] && items[0].json) || {};
const sah = Array.isArray(j.timeline) || Array.isArray(j.articles);
return [{ json: {
  sah,
  data: sah ? j : null,
  galat: sah ? null : String(j).slice(0, 200),
} }];
"""

def build_research() -> dict:
    """Kumpulkan intelijen pasar -> simpan sebagai satu dokumen di cache KV.

    NOL Code node. Urutannya penting: tiap cabang menamai field-nya lebih dulu lewat
    Edit Fields, baru semuanya digabung satu node Merge. Tanpa penamaan itu, Merge
    akan menumpuk enam item terpisah dan dokumen induknya kosong -- kesalahan yang
    tidak menimbulkan error, hanya data yang hilang diam-diam.
    """
    f = Flow("Pionex Guard — 07 Riset Pasar & Cache")
    f.add(schedule("Setiap 15 menit", {"field": "minutes", "minutesInterval": 15}))
    sym = "{{ $env.PIONEX_SYMBOL }}"

    # --- 1. nada berita global (GDELT TimelineTone: rata-rata sentimen per hari) ---
    f.add(http("GDELT nada berita", "GET", GDELT_TONE, (-1000, -300)))
    f.add(code("Sah: nada berita", guard_body(GDELT_GUARD), (-820, -300)))
    f.add(set_fields("Beri nama: tone", [("tone", "={{ $json }}")], (-640, -300)))
    f.add(redis_set("Cache nada berita", "pg:research:tone",
                    "={{ JSON.stringify($json.tone) }}", 3600, (-460, -300)))

    # --- 2. volume berita (untuk mendeteksi lonjakan pemberitaan) ---
    # Jeda 6 detik: GDELT menolak lebih dari 1 permintaan per 5 detik, dan menolak
    # dengan HTTP 200 sehingga kegagalan ini tidak terlihat sebagai kegagalan.
    f.add(wait_node("Jeda GDELT 1", 6, (-1000, -180)))
    f.add(http("GDELT volume berita", "GET", GDELT_VOL, (-820, -180)))
    f.add(code("Sah: volume berita", guard_body(GDELT_GUARD), (-640, -180)))
    f.add(set_fields("Beri nama: volume", [("volume", "={{ $json }}")], (-460, -180)))
    f.add(redis_set("Cache volume berita", "pg:research:volume",
                    "={{ JSON.stringify($json.volume) }}", 3600, (-280, -180)))

    # --- 3. berita bernada paling negatif 2 hari terakhir ---
    f.add(wait_node("Jeda GDELT 2", 6, (-1000, -60)))
    f.add(http("GDELT berita negatif", "GET", GDELT_NEG, (-820, -60)))
    f.add(code("Sah: berita negatif", guard_body(GDELT_GUARD), (-640, -60)))
    f.add(set_fields("Beri nama: negative",
                     [("negative", "={{ ($json.data && $json.data.articles) || [] }}")],
                     (-460, -60)))
    f.add(limit_node("Batasi 15 berita", 15, (-320, -60)))
    f.add(redis_set("Cache berita negatif", "pg:research:negative",
                    "={{ JSON.stringify($json.negative) }}", 3600, (-180, -60)))

    # --- 4. Fear & Greed 14 hari ---
    f.add(http("Fear & Greed", "GET", FNG, (-900, 60)))
    f.add(set_fields("Beri nama: fear_greed", [("fear_greed", "={{ $json.data || [] }}")],
                     (-700, 60)))
    f.add(redis_set("Cache fear greed", "pg:research:fear_greed",
                    "={{ JSON.stringify($json.fear_greed) }}", 3600, (-500, 60)))

    # --- 5. aliran stablecoin: proksi likuiditas masuk/keluar kripto ---
    # Payload mentahnya ratusan KB, jadi diambil 30 titik terakhir saja.
    f.add(http("Aliran stablecoin", "GET", STABLE_FLOW, (-900, 180)))
    f.add(aggregate("Rangkai deret stablecoin", "", "series", (-760, 180)))
    f.add(set_fields("Ambil 30 hari terakhir",
                     [("stablecoin", "={{ ($json.series || []).slice(-30) }}")],
                     (-560, 180)))
    f.add(redis_set("Cache aliran stablecoin", "pg:research:stablecoin",
                    "={{ JSON.stringify($json.stablecoin) }}", 3600, (-360, 180)))

    # --- 6. funding & indeks dari bursa ---
    f.add(http("Funding & indeks", "GET",
               f"{BASE}/api/v1/market/indexes?symbol={sym}", (-900, 300)))
    f.add(set_fields("Beri nama: funding", [("funding", "={{ $json }}")], (-700, 300)))
    f.add(redis_set("Cache funding", "pg:research:funding",
                    "={{ JSON.stringify($json.funding) }}", 900, (-500, 300)))

    # --- gabung enam cabang menjadi SATU item berisi enam field bernama ---
    f.add(merge_node("Gabung semua sumber", "combine", (0, 0)))
    f.add(set_fields("Dokumen riset", [
        ("symbol", "={{ $env.PIONEX_SYMBOL }}"),
        ("collected_at", "={{ new Date().toISOString() }}"),
    ], (220, 0)))
    f.add(redis_set("Cache induk (kv)", "pg:research:latest",
                    "={{ JSON.stringify($json) }}", 7200, (440, 0)))

    for src in ("GDELT nada berita", "Fear & Greed", "Aliran stablecoin",
                "Funding & indeks"):
        f.link("Setiap 15 menit", src)
    f.link("GDELT nada berita", "Sah: nada berita")
    f.link("Sah: nada berita", "Beri nama: tone")
    f.link("Beri nama: tone", "Cache nada berita")
    f.link("Setiap 15 menit", "Jeda GDELT 1")
    f.link("Jeda GDELT 1", "GDELT volume berita")
    f.link("GDELT volume berita", "Sah: volume berita")
    f.link("Sah: volume berita", "Beri nama: volume")
    f.link("Beri nama: volume", "Cache volume berita")
    f.link("Setiap 15 menit", "Jeda GDELT 2")
    f.link("Jeda GDELT 2", "GDELT berita negatif")
    f.link("GDELT berita negatif", "Sah: berita negatif")
    f.link("Sah: berita negatif", "Beri nama: negative")
    f.link("Beri nama: negative", "Batasi 15 berita")
    f.link("Batasi 15 berita", "Cache berita negatif")
    f.link("Fear & Greed", "Beri nama: fear_greed")
    f.link("Beri nama: fear_greed", "Cache fear greed")
    f.link("Aliran stablecoin", "Rangkai deret stablecoin")
    f.link("Rangkai deret stablecoin", "Ambil 30 hari terakhir")
    f.link("Ambil 30 hari terakhir", "Cache aliran stablecoin")
    f.link("Funding & indeks", "Beri nama: funding")
    f.link("Beri nama: funding", "Cache funding")
    for src in ("Cache nada berita", "Cache volume berita", "Cache berita negatif",
                "Cache fear greed", "Cache aliran stablecoin", "Cache funding"):
        f.link(src, "Gabung semua sumber")
    f.link("Gabung semua sumber", "Dokumen riset")
    f.link("Dokumen riset", "Cache induk (kv)")
    return f.json()


# --------------------------------------------------------------------------------------
# 8. Simulasi agen pasar (gaya MiroFish) — satu-satunya bagian yang memakai LLM
# --------------------------------------------------------------------------------------

AGENTS = [
    ("makro",
     "Analis makro. Anda hanya peduli pada likuiditas global, kebijakan bank sentral, "
     "data inflasi, dan aliran stablecoin. Abaikan grafik harga."),
    ("arus_modal",
     "Analis aliran modal. Anda membaca Fear & Greed, pasokan stablecoin, dan funding "
     "rate untuk menyimpulkan apakah uang sedang masuk atau keluar dari kripto."),
    ("risiko_ekstrem",
     "Analis risiko peristiwa. Tugas Anda HANYA mencari hal yang bisa mengguncang harga "
     "tiba-tiba: peretasan, kebangkrutan, tindakan regulator, depeg, gangguan bursa. "
     "Bila tidak ada, katakan tidak ada. Jangan mengarang risiko."),
    ("kontrarian",
     "Anda wajib berargumen melawan konsensus. Bila semua sinyal bullish, jelaskan "
     "kenapa itu bisa salah. Bila bearish, jelaskan sisi sebaliknya. Tugas Anda "
     "menguji, bukan menyenangkan."),
    ("mikrostruktur",
     "Analis mikrostruktur pasar. Anda membaca funding rate dan volatilitas untuk "
     "menyimpulkan apakah posisi pasar sudah terlalu padat ke satu arah."),
]

ENTRY_BUILD_AGENT_CALLS = """
// ---- entry point n8n: bangun satu panggilan LLM per agen ---------------------------
// Code node karena tidak ada node asli yang bisa merangkai daftar peran menjadi
// beberapa item dengan prompt berbeda. Selebihnya aliran ini memakai node asli.
//
// PENTING: di mode "Run Once for All Items", $json TIDAK dijamin terisi. Membaca
// $json di sini menghasilkan {} dan seluruh konteks pasar hilang tanpa error --
// prompt terkirim kosong dan para agen menjawab dari imajinasi. Karena itu masukan
// selalu diambil dari `items` secara eksplisit.
const cache = (items[0] && items[0].json && items[0].json.cache) || {};
const context = JSON.stringify({
  symbol: cache.symbol || $env.PIONEX_SYMBOL,
  collected_at: cache.collected_at || null,
  fear_greed: cache.fear_greed || null,
  tone_berita: cache.tone || null,
  volume_berita: cache.volume || null,
  berita_negatif: (cache.negative || []).slice(0, 15),
  aliran_stablecoin: cache.stablecoin || null,
  funding: cache.funding || null,
});

const AGENTS = __AGENTS__;
const model = $env.LLM_MODEL || 'gpt-4o-mini';

return AGENTS.map((pair) => ({ json: {
  agent: pair[0],
  body: JSON.stringify({
    model,
    temperature: 0.2,
    response_format: { type: 'json_object' },
    messages: [
      { role: 'system', content:
          'Anda adalah ' + pair[1] + ' Jawab HANYA dalam JSON dengan kunci: '
        + '"stance" (LONG|SHORT|NEUTRAL), "confidence" (0 sampai 1), '
        + '"event_risk" (LOW|MEDIUM|HIGH), "alasan" (maks 2 kalimat), '
        + '"bukti" (array string, sebutkan angka atau judul berita yang Anda pakai). '
        + 'Bila data tidak cukup, gunakan confidence rendah dan stance NEUTRAL. '
        + 'Jangan mengarang data yang tidak ada di konteks.' },
      { role: 'user', content: context },
    ],
  }),
} }));
"""

EXTRACT_JSON_HELPER = """
// Mengambil objek JSON pertama dari teks LLM.
// Regex seperti pola "buang semua setelah kurung kurawal terakhir" TIDAK boleh dipakai
// di sini: pola semacam itu memotong objek valid tepat di kurung kurawal pertama, jadi
// JSON yang benar justru terbaca sebagai gagal. Yang dipakai adalah pemindaian
// berbasis kedalaman kurung, yang berhenti hanya bila kurung benar-benar seimbang.
function extractJsonObject(text) {
  if (typeof text !== 'string') return null;
  const start = text.indexOf('{');
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < text.length; i += 1) {
    const c = text[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (c === String.fromCharCode(92)) escaped = true;
      else if (c === '"') inString = false;
      continue;
    }
    if (c === '"') inString = true;
    else if (c === '{') depth += 1;
    else if (c === '}') {
      depth -= 1;
      if (depth === 0) {
        try { return JSON.parse(text.slice(start, i + 1)); } catch (e) { return null; }
      }
    }
  }
  return null;
}
"""


ENTRY_SYNTHESIZE = """
// ---- entry point n8n: simpulkan pendapat para agen --------------------------------
// Code node ini melakukan dua hal yang tidak bisa dilakukan node asli:
//   1. menggabungkan N jawaban agen menjadi satu konteks,
//   2. mem-PARSE JSON keluaran LLM. Parser ini sengaja ketat: LLM bisa membalas teks
//      di luar JSON, dan aturan sistem ini adalah "gagal = veto", bukan "tebak".
const model = $env.LLM_MODEL || 'gpt-4o-mini';
const opinions = [];
for (const item of items) {
  const j = item.json || {};
  const raw = (j.choices && j.choices[0] && j.choices[0].message
               && j.choices[0].message.content) || null;
  const parsed = extractJsonObject(raw);
  opinions.push({ agen: j.agent || 'tidak_diketahui', mentah: raw || null, parsed });

}

const gagal = opinions.filter((o) => !o.parsed).length;

// LANTAI KERAS untuk event_risk.
// Aturan "bila ada satu agen menyebut risiko peristiwa, event_risk tidak boleh LOW"
// sebelumnya hanya tertulis di PROMPT sintesis. Ternyata tidak cukup: pada uji dengan
// berita exploit, agen risiko menjawab HIGH tetapi sintesis menurunkannya jadi MEDIUM
// karena empat agen lain menjawab LOW -- dan prompt tidak bisa memaksa model.
// Karena itu lantainya dihitung DI SINI, di kode, dan ditegakkan di pembungkus verdict.
// Aturan keselamatan yang bergantung pada kepatuhan model bukan aturan.
const RANK = { LOW: 0, MEDIUM: 1, HIGH: 2 };
let lantai = 'LOW';
for (const o of opinions) {
  const r = o.parsed && typeof o.parsed.event_risk === 'string'
    ? o.parsed.event_risk.toUpperCase() : null;
  if (r && RANK[r] !== undefined && RANK[r] > RANK[lantai]) lantai = r;
}

const body = JSON.stringify({
  model,
  temperature: 0.1,
  response_format: { type: 'json_object' },
  messages: [
    { role: 'system', content:
        'Anda menyimpulkan diskusi beberapa analis. Jawab HANYA dalam JSON dengan kunci: '
      + '"prediction" (minimal 20 kata, ringkasan kesimpulan), "confidence" (0 sampai 1), '
      + '"event_risk" (LOW|MEDIUM|HIGH), "key_dynamics" (array string), '
      + '"signals" (array string). Aturan keras: bila analis tidak sepakat, confidence '
      + 'harus di bawah 0.6 dan event_risk tidak boleh LOW. Bila ada satu saja analis '
      + 'menyebut risiko peristiwa konkret, event_risk minimal MEDIUM. '
      + 'Confidence maksimum 0.85: Anda menyimpulkan opini, bukan mengukur probabilitas.' },
    { role: 'user', content: JSON.stringify({ jumlah_agen: opinions.length,
                                              agen_gagal: gagal, pendapat: opinions }) },
  ],
});

return [{ json: { agent: 'sintesis', agen_gagal: gagal, lantai_event_risk: lantai,
                  jumlah_agen: opinions.length, body } }];
"""

ENTRY_WRAP_VERDICT = """
// ---- entry point n8n: bentuk keluaran LLM menjadi verdict + manifest ---------------
// Adapter di node berikutnya menuntut bentuk { verdict, manifest, run_id }. Bentuk itu
// KONTRAK yang sama dengan yang dulu dihasilkan mirofish_runner, sehingga adapter,
// tabel mirofish_verdict, dan gerbang risiko tidak perlu diubah sama sekali.
const j = $json || {};
const raw = (j.choices && j.choices[0] && j.choices[0].message
             && j.choices[0].message.content) || null;
const verdict = extractJsonObject(raw);

// Tegakkan lantai yang dihitung di node sintesis. Sintesis boleh menaikkan risiko,
// tidak boleh menurunkannya di bawah yang dilaporkan agen mana pun.
const RANK = { LOW: 0, MEDIUM: 1, HIGH: 2 };
const lantai = typeof j.lantai_event_risk === 'string'
  ? j.lantai_event_risk.toUpperCase() : 'LOW';
if (verdict && RANK[lantai] !== undefined) {
  const kini = typeof verdict.event_risk === 'string'
    ? verdict.event_risk.toUpperCase() : 'LOW';
  if (RANK[kini] === undefined || RANK[kini] < RANK[lantai]) verdict.event_risk = lantai;
}

const now = new Date().toISOString();
const runId = 'n8n-sim-' + Date.now();
return [{ json: {
  run_id: runId, job_id: runId, job_status: 'SUCCEEDED', created_at: now,
  verdict: verdict,               // null bila LLM tidak mengembalikan JSON sah
  summary: null,
  manifest: { run_id: runId, created_at: now, agen_gagal: j.agen_gagal || 0,
              jumlah_agen: j.jumlah_agen || 0 },
} }];
"""


def build_simulation() -> dict:
    f = Flow("Pionex Guard — 08 Simulasi Agen Pasar")
    f.add(schedule("Setiap 2 jam", {"field": "hours", "hoursInterval": 2}))

    f.add(redis_get("Baca cache riset", "pg:research:latest", "cache", (-800, 0)))
    agents_literal = "[" + ", ".join(
        "[" + json.dumps(a) + ", " + json.dumps(b) + "]" for a, b in AGENTS) + "]"
    f.add(code("Bangun panggilan agen",
               guard_body(ENTRY_BUILD_AGENT_CALLS.replace("__AGENTS__", agents_literal)),
               (-600, 0)))
    # Header X-Pg-Role bukan bagian dari protokol OpenAI; penyedia mana pun akan
    # mengabaikannya. Fungsinya observasi: di log penyedia terlihat agen mana yang
    # menghabiskan token. Sekaligus membuat server tiruan tidak perlu menebak peran
    # dari isi prompt -- menebak dari teks terbukti rapuh.
    f.add(http("LLM paralel (5 agen)", "POST",
               "={{ $env.LLM_API_BASE }}/chat/completions", (-350, 0),
               body="={{ $json.body }}",
               headers={"Authorization": "=Bearer {{ $env.LLM_API_KEY }}",
                        "Content-Type": "application/json",
                        "X-Pg-Role": "={{ $json.agent }}"}))
    f.add(code("Simpulkan pendapat agen",
               guard_body(EXTRACT_JSON_HELPER + ENTRY_SYNTHESIZE), (-100, 0)))
    f.add(http("LLM sintesis", "POST",
               "={{ $env.LLM_API_BASE }}/chat/completions", (150, 0),
               body="={{ $json.body }}",
               headers={"Authorization": "=Bearer {{ $env.LLM_API_KEY }}",
                        "Content-Type": "application/json",
                        "X-Pg-Role": "=sintesis"}))
    f.add(code("Bungkus jadi verdict",
               guard_body(EXTRACT_JSON_HELPER + ENTRY_WRAP_VERDICT), (400, 0)))
    f.add(code("Adapter verdict", adapter_body(), (650, 0)))
    f.add(postgres("Simpan verdict", "executeQuery",
                   "INSERT INTO mirofish_verdict (run_id, verdict_ts, schema_ok, bias, confidence, "
                   "event_risk, horizon_hours, risk_score, evidence, adapter_version, raw) "
                   "VALUES ('{{ $json.run_id }}', '{{ $json.verdict_ts }}', {{ $json.schema_ok }}, "
                   "'{{ $json.bias }}', {{ $json.confidence }}, '{{ $json.event_risk }}', "
                   "{{ $json.horizon_hours }}, "
                   "{{ $json.risk_score === undefined || $json.risk_score === null ? 1 : $json.risk_score }}, "
                   "'{{ JSON.stringify($json.evidence) }}'::jsonb, "
                   "'{{ $json.adapter_version }}', '{{ JSON.stringify($json) }}'::jsonb) "
                   "ON CONFLICT (run_id) DO NOTHING;", (900, 0)))
    f.add(if_node("Veto?", [cond("={{ $json.event_risk }}", "equals", "HIGH")], (1150, 0)))
    f.add(notify("Notifikasi veto", (1400, 140)))

    f.link("Setiap 2 jam", "Baca cache riset")
    f.link("Baca cache riset", "Bangun panggilan agen")
    f.link("Bangun panggilan agen", "LLM paralel (5 agen)")
    f.link("LLM paralel (5 agen)", "Simpulkan pendapat agen")
    f.link("Simpulkan pendapat agen", "LLM sintesis")
    f.link("LLM sintesis", "Bungkus jadi verdict")
    f.link("Bungkus jadi verdict", "Adapter verdict")
    f.link("Adapter verdict", "Simpan verdict")
    f.link("Simpan verdict", "Veto?")
    f.link("Veto?", "Notifikasi veto", "main", 0)
    return f.json()


# --------------------------------------------------------------------------------------
# 9. Penjaga kesegaran cache
# --------------------------------------------------------------------------------------


def build_cache_watchdog() -> dict:
    f = Flow("Pionex Guard — 09 Penjaga Cache")
    f.add(schedule("Setiap 10 menit", {"field": "minutes", "minutesInterval": 10}))
    f.add(redis_get("Baca cache riset", "pg:research:latest", "cache", (-600, 0)))
    f.add(set_fields("Hitung umur", [
        ("age_sec", "={{ $json.cache && $json.cache.collected_at "
                    "? (Date.now() - Date.parse($json.cache.collected_at)) / 1000 : 999999 }}"),
        ("collected_at", "={{ $json.cache ? $json.cache.collected_at : null }}"),
    ], (-350, 0)))
    f.add(if_node("Basi?", [cond("={{ $json.age_sec }}", "gt", 3600)], (-100, 0)))
    f.add(notify("Notifikasi cache basi", (200, 140)))

    f.link("Setiap 10 menit", "Baca cache riset")
    f.link("Baca cache riset", "Hitung umur")
    f.link("Hitung umur", "Basi?")
    f.link("Basi?", "Notifikasi cache basi", "main", 0)
    return f.json()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    builders = [
        ("01-preflight-watchdog.json", build_preflight),
        ("02-market-sweep.json", build_market),
        ("03-trading-loop.json", build_trading),
        ("04-position-monitor.json", build_monitor),
        ("05-kill-switch.json", build_killswitch),
        ("06-mirofish-sweep.json", build_mirofish),
        ("07-market-research-cache.json", build_research),
        ("08-market-intel-simulation.json", build_simulation),
        ("09-cache-watchdog.json", build_cache_watchdog),
    ]
    for fname, fn in builders:
        path = OUT / fname
        path.write_text(json.dumps(fn(), indent=2) + "\n", encoding="utf-8")
        print(f"ditulis: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
