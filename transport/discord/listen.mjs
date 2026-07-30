// listen.mjs — a Discord Gateway listener that knows nothing about the kit.
//
// It opens a real Gateway websocket, handles identify/heartbeat/reconnect, and
// prints ONE JSON object per message in the table channel:
//
//     {"ts":1753900000.0,"author":"Rowan","content":"I go in","id":"..."}
//
// Pipe it into the ingester, which owns all schema knowledge:
//
//     node transport/discord/listen.mjs table.json | python3 -m tablekit.ingest
//
// Two things here were paid for the hard way.
//
// 1. **Intents fall back.** MESSAGE_CONTENT requires a toggle in the developer
//    portal that new bots do not have. Rather than fail at connect with an
//    opaque 4014, this drops to basic intents and fetches content over REST.
//    Slower, works today, no portal round-trip in the middle of a session.
//
// 2. **The listener is the cursor owner.** Nothing else may write
//    watch-cursor. Two writers to one cursor file will eventually ack past an
//    unread message, and the symptom — a player's turn that everyone swears
//    they sent — costs an evening to diagnose.
//
// Reconnection is unconditional and logged. A listener that dies quietly is
// the single most expensive failure in this system: play continues, nothing
// errors, and the record simply stops.

import { execSync } from 'node:child_process';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';

const cfgPath = resolve(process.argv[2] || process.env.TABLE_CONFIG || 'table.json');
const cfg = JSON.parse(readFileSync(cfgPath, 'utf8'));
const t = cfg.transport || {};
const CHANNEL = t.channel_id;
const ME = t.bot_user_id;
const TOKEN_ENV = t.token_env || 'TABLE_BOT_TOKEN';
const TOKEN = process.env[TOKEN_ENV];

if (!CHANNEL) { console.error(`[listen] transport.channel_id missing from ${cfgPath}`); process.exit(2); }
if (!TOKEN) { console.error(`[listen] $${TOKEN_ENV} is not set (tokens are never read from config)`); process.exit(2); }

const dataDir = resolve(dirname(cfgPath), cfg.data_dir || './table-data');
mkdirSync(dataDir, { recursive: true });
const CURSOR = join(dataDir, 'watch-cursor');

const INTENT_FULL = 512 | 32768;  // GUILD_MESSAGES | MESSAGE_CONTENT
const INTENT_BASIC = 512;
let intents = INTENT_FULL;
let backoff = 1000;

const api = (path) => fetch(`https://discord.com/api/v10${path}`, {
  headers: { Authorization: `Bot ${TOKEN}`, 'User-Agent': 'DiscordBot (table-kit, 0.1)' },
});

async function restContent(id) {
  try {
    const r = await api(`/channels/${CHANNEL}/messages/${id}`);
    return r.ok ? (await r.json()).content : '';
  } catch { return ''; }
}

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function connect() {
  const ws = new WebSocket('wss://gateway.discord.gg/?v=10&encoding=json');
  let hb = null, seq = null;

  ws.onmessage = async (ev) => {
    const p = JSON.parse(ev.data);
    if (p.s) seq = p.s;

    if (p.op === 10) {
      hb = setInterval(() => ws.send(JSON.stringify({ op: 1, d: seq })), p.d.heartbeat_interval);
      ws.send(JSON.stringify({
        op: 2,
        d: { token: TOKEN, intents, properties: { os: process.platform, browser: 'table-kit', device: 'table-kit' } },
      }));
    } else if (p.op === 0 && p.t === 'READY') {
      backoff = 1000;
      console.error(`[listen] connected as ${p.d.user.username} (${intents === INTENT_FULL ? 'full intents' : 'basic intents + REST content'})`);
    } else if (p.op === 0 && p.t === 'MESSAGE_CREATE') {
      const m = p.d;
      if (m.channel_id !== CHANNEL) return;
      if (ME && m.author?.id === ME) return;   // our own posts; already recorded
      let content = m.content || '';
      if (!content && intents === INTENT_BASIC) content = await restContent(m.id);
      try { writeFileSync(CURSOR, m.id); } catch { /* cursor is a convenience, not a dependency */ }
      emit({
        ts: Date.parse(m.timestamp) / 1000,
        id: m.id,
        author: m.author?.username || 'unknown',
        author_id: m.author?.id,
        is_bot: Boolean(m.author?.bot),
        content,
      });
    } else if (p.op === 7 || p.op === 9) {
      ws.close();
    }
  };

  ws.onclose = (ev) => {
    clearInterval(hb);
    if (ev.code === 4014 && intents === INTENT_FULL) {
      console.error('[listen] MESSAGE_CONTENT intent not enabled in the developer portal — falling back to basic intents + REST');
      intents = INTENT_BASIC;
      setTimeout(connect, 500);
      return;
    }
    console.error(`[listen] disconnected (${ev.code}) — reconnecting in ${backoff}ms`);
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 30000);
  };

  ws.onerror = () => { /* onclose always follows; reconnect is handled there */ };
}

connect();
