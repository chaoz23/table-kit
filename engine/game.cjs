// table-kit: scenario-driven combat state for a live hybrid table.
// Topology: ONE bgio player ('0') = the arbiter (DM). Players express intent in
// Discord; the DM submits moves. Initiative lives IN the engine (G.order) and is
// enforced per-unit — the single-writer ledger model. boardgame.io provides:
// authoritative state, seeded replayable dice, the event log, and INVALID_MOVE.
const { INVALID_MOVE } = require('boardgame.io/core');

const DEFAULT_SCENARIO = {
  name: 'test-skirmish',
  units: {
    hero: { name: 'Hero', side: 'party', hp: 10, maxhp: 10, ac: 14, atk: 4, dmg: '1d6+2', initMod: 1 },
    gob:  { name: 'Goblin', side: 'foe', hp: 7, maxhp: 7, ac: 15, atk: 4, dmg: '1d6+2', initMod: 2 },
  },
};

function rollExpr(random, expr, crit = false) {
  // d20-notation subset: NdM[khX|klX][+K|-K]. On crit, dice are doubled (5e).
  const m = /^(\d+)d(\d+)(k[hl]\d+)?([+-]\d+)?$/.exec(String(expr).replace(/\s/g, ''));
  if (!m) return null;
  let n = parseInt(m[1], 10);
  const sides = parseInt(m[2], 10), keep = m[3], mod = parseInt(m[4] || '0', 10);
  if (crit) n *= 2;
  const rolls = [];
  for (let i = 0; i < n; i++) rolls.push(random.Die(sides));
  let kept = rolls;
  if (keep) {
    const k = parseInt(keep.slice(2), 10), hi = keep[1] === 'h';
    kept = [...rolls].sort((a, b) => (hi ? b - a : a - b)).slice(0, k);
  }
  const total = kept.reduce((x, y) => x + y, 0) + mod;
  return { total: Math.max(0, total), rolls, kept, mod };
}

const cur = (G) => G.order[G.active];

function advance(G) {
  const n = G.order.length;
  for (let i = 1; i <= n; i++) {
    const idx = (G.active + i) % n;
    if (G.units[G.order[idx]].alive) {
      if (idx <= G.active) G.round += 1; // wrapped past the top: new round
      G.active = idx;
      return;
    }
  }
}

function applyDamage(G, t, dmg) {
  t.hp = Math.max(0, t.hp - dmg);
  if (t.hp === 0) t.alive = false;
}

module.exports.AgentTable = {
  name: 'table-kit',

  setup: ({ random }, setupData) => {
    const sc = setupData && setupData.units ? setupData : DEFAULT_SCENARIO;
    const units = {};
    for (const [id, u] of Object.entries(sc.units)) {
      units[id] = { conds: [], alive: (u.hp ?? u.maxhp) > 0, ...u, hp: u.hp ?? u.maxhp };
    }
    const order = Object.keys(units)
      .map((id) => ({ id, init: random.D20() + (units[id].initMod || 0) }))
      .sort((a, b) => b.init - a.init);
    const log = [`[setup] ${sc.name}: initiative ${order.map((o) => `${units[o.id].name}(${o.init})`).join(' > ')}`];
    return { scenario: sc.name, units, order: order.map((o) => o.id), active: 0, round: 1, log };
  },

  turn: { activePlayers: { currentPlayer: 'play' } },

  moves: {
    act: ({ G, random }, unitId, action, targetId, extra = {}) => {
      const a = G.units[unitId];
      if (!a || !a.alive) return INVALID_MOVE;
      if (unitId !== cur(G)) return INVALID_MOVE; // engine-enforced initiative
      if (action === 'pass') {
        G.log.push(`[r${G.round}] ${a.name} holds.`);
        advance(G);
        return;
      }
      const t = G.units[targetId];
      if (!t || !t.alive || t.side === a.side) return INVALID_MOVE;

      if (action === 'attack') {
        let d20 = random.D20(), alt = null;
        if (extra.adv || extra.disadv) {
          alt = random.D20();
          d20 = extra.adv ? Math.max(d20, alt) : Math.min(d20, alt);
        }
        const atk = extra.atk ?? a.atk ?? 0;
        const total = d20 + atk;
        const crit = d20 === 20, fumble = d20 === 1;
        const hit = crit || (!fumble && total >= t.ac);
        if (hit) {
          const dm = rollExpr(random, extra.dmg ?? a.dmg ?? '1d6', crit);
          if (!dm) return INVALID_MOVE;
          applyDamage(G, t, dm.total);
          G.log.push(`[r${G.round}] ${a.name}→${t.name}: d20(${d20})+${atk}=${total} vs AC${t.ac}${alt !== null ? (extra.adv ? ' adv' : ' dis') : ''} HIT${crit ? ' CRIT' : ''} ${dm.total} → ${t.alive ? t.hp + '/' + t.maxhp : 'DOWN'}`);
        } else {
          G.log.push(`[r${G.round}] ${a.name}→${t.name}: d20(${d20})+${atk}=${total} vs AC${t.ac}${alt !== null ? (extra.adv ? ' adv' : ' dis') : ''} MISS${fumble ? ' (nat 1)' : ''}`);
        }
        advance(G);
        return;
      }

      if (action === 'save-spell') {
        // extra: {name, dc, saveMod?, dmg, half?}
        const dc = extra.dc; const dmgExpr = extra.dmg;
        if (typeof dc !== 'number' || !dmgExpr) return INVALID_MOVE;
        const d20 = random.D20();
        const saveMod = extra.saveMod ?? t.saveMod ?? 0;
        const save = d20 + saveMod;
        const made = save >= dc;
        const dm = rollExpr(random, dmgExpr, false);
        if (!dm) return INVALID_MOVE;
        let dealt = made ? (extra.half ? Math.floor(dm.total / 2) : 0) : dm.total;
        applyDamage(G, t, dealt);
        G.log.push(`[r${G.round}] ${a.name} ${extra.name || 'spell'}→${t.name}: save d20(${d20})+${saveMod}=${save} vs DC${dc} ${made ? 'SAVE' : 'FAIL'} ${dealt} → ${t.alive ? t.hp + '/' + t.maxhp : 'DOWN'}`);
        advance(G);
        return;
      }

      return INVALID_MOVE;
    },

    // DM ruling lane: direct state adjustment, always logged as a ruling.
    adjust: ({ G }, unitId, patch = {}) => {
      const u = G.units[unitId];
      if (!u) return INVALID_MOVE;
      const bits = [];
      if (typeof patch.hp === 'number') {
        u.hp = Math.max(0, Math.min(u.maxhp, u.hp + patch.hp));
        u.alive = u.hp > 0;
        bits.push(`hp${patch.hp >= 0 ? '+' : ''}${patch.hp}→${u.hp}/${u.maxhp}${u.alive ? '' : ' DOWN'}`);
      }
      for (const f of ["ac", "atk", "dmg", "name"]) {
        if (patch[f] !== undefined) { u[f] = patch[f]; bits.push(`${f}=${patch[f]}`); }
      }
      if (patch.addCond) { u.conds.push(patch.addCond); bits.push(`+${patch.addCond}`); }
      if (patch.delCond) { u.conds = u.conds.filter((c) => c !== patch.delCond); bits.push(`-${patch.delCond}`); }
      G.log.push(`[r${G.round}] [ruling] ${u.name}: ${bits.join(' ')}${patch.note ? ' (' + patch.note + ')' : ''}`);
    },
  },

  endIf: ({ G }) => {
    const alive = (s) => Object.values(G.units).some((u) => u.side === s && u.alive);
    if (!alive('party')) return { winner: 'foe' };
    if (!alive('foe')) return { winner: 'party' };
  },
};
