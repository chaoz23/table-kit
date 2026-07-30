// Short-lived move client: connect → sync → (optionally) submit one move → print state.
// Usage:
//   node move.mjs state
//   node move.mjs act <unitId> <action> [targetId] [extraJSON]
//   node move.mjs adjust <unitId> <patchJSON>
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
const { AgentTable } = require('./game.cjs');
const { Client } = await import('boardgame.io/dist/esm/client.js');
const { SocketIO } = await import('boardgame.io/dist/esm/multiplayer.js');

const match = JSON.parse(readFileSync(new URL('./data/match.json', import.meta.url), 'utf8'));
const [cmd, ...args] = process.argv.slice(2);

const client = Client({
  game: AgentTable,
  multiplayer: SocketIO({ server: 'http://127.0.0.1:18800' }),
  matchID: match.matchID,
  playerID: '0',
  credentials: match.credentials,
  debug: false,
});
client.start();

const waitFor = (pred, ms, what) => new Promise((res, rej) => {
  const t0 = Date.now();
  const iv = setInterval(() => {
    if (pred()) { clearInterval(iv); res(); }
    else if (Date.now() - t0 > ms) { clearInterval(iv); rej(new Error('timeout: ' + what)); }
  }, 25);
});

const summary = () => {
  const s = client.getState();
  if (!s) return null;
  const { G, ctx } = s;
  return {
    scenario: G.scenario,
    round: G.round,
    turn: G.order[G.active],
    order: G.order.map((id) => `${id}${G.units[id].alive ? '' : '†'}`),
    units: Object.fromEntries(Object.entries(G.units).map(([id, u]) =>
      [id, `${u.name} [${u.side}] ${u.hp}/${u.maxhp} AC${u.ac}${u.conds.length ? ' ' + u.conds.join(',') : ''}${u.alive ? '' : ' DOWN'}`])),
    log_tail: G.log.slice(-4),
    log_len: G.log.length,
    gameover: ctx.gameover ?? null,
  };
};

try {
  await waitFor(() => client.getState() !== null, 8000, 'initial sync');
  if (cmd && cmd !== 'state') {
    const before = client.getState().G.log.length;
    if (cmd === 'act') {
      const [unitId, action, targetId, extraJSON] = args;
      client.moves.act(unitId, action, targetId ?? null, extraJSON ? JSON.parse(extraJSON) : {});
    } else if (cmd === 'adjust') {
      const [unitId, patchJSON] = args;
      client.moves.adjust(unitId, JSON.parse(patchJSON ?? '{}'));
    } else {
      throw new Error('unknown command: ' + cmd);
    }
    // wait for server ack: log grows on accept; on INVALID_MOVE state reverts (no growth)
    await waitFor(() => {
      const s = client.getState();
      return s && s.G.log.length !== before || false;
    }, 4000, 'move ack').catch(() => {});
    const after = client.getState().G.log.length;
    console.log(JSON.stringify({ ok: after > before, rejected: after <= before, ...summary() }, null, 1));
  } else {
    console.log(JSON.stringify({ ok: true, ...summary() }, null, 1));
  }
  client.stop();
  process.exit(0);
} catch (e) {
  console.error('ERR ' + e.message);
  client.stop();
  process.exit(1);
}
