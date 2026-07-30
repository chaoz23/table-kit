// story.mjs — the story-state lane + invariant guard (module-agnostic).
// The combat engine holds fight state; this holds the adventure: act, scene,
// flags — validated against the module's spine (the constitution) on every
// change. Freeform play, guarded outcomes: gates constrain WHAT may become
// true and endings constrain HOW the story may resolve — never which path
// the table takes to get there.
// Usage:
//   node story.mjs init <moduleDir>          # start/reset story state
//   node story.mjs get                       # full state (DM eyes — spoilers)
//   node story.mjs set <flag> <value> [note] # guarded flag change
//   node story.mjs scene <name> [note]       # change scene (unguarded label)
//   node story.mjs ending <id>               # guarded ending declaration
//   node story.mjs recap                     # 2-line spoiler-safe recap
//   node story.mjs card                      # current scene card (if authored)
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const DIR = dirname(fileURLToPath(import.meta.url));
const STORY = join(DIR, 'data', 'story.json');

const load = () => JSON.parse(readFileSync(STORY, 'utf8'));
const save = (s) => writeFileSync(STORY, JSON.stringify(s, null, 1));
const spineOf = (s) => JSON.parse(readFileSync(join(s.moduleDir, 'spine.json'), 'utf8'));
const parseVal = (v) => { try { return JSON.parse(v); } catch { return v; } };

const cmp = (val, cond) => Object.entries(cond).every(([op, want]) =>
  op === 'eq' ? val === want : op === 'gte' ? val >= want : op === 'lte' ? val <= want : false);

function checkGates(spine, flags, flag, value) {
  const fails = [];
  for (const g of spine.gates ?? []) {
    if (g.when_set !== flag) continue;
    const toMatch = typeof g.to === 'object' ? cmp(value, g.to) : value === g.to;
    if (!toMatch) continue;
    const next = { ...flags, [flag]: value };
    for (const [f, cond] of Object.entries(g.requires ?? {})) {
      if (!cmp(next[f], cond)) fails.push({ id: g.id, severity: g.severity, desc: g.desc, unmet: `${f} must satisfy ${JSON.stringify(cond)} (is ${JSON.stringify(next[f])})` });
    }
  }
  return fails;
}

const [cmd, ...args] = process.argv.slice(2);
const out = (o) => console.log(JSON.stringify(o, null, 1));

if (cmd === 'init') {
  const moduleDir = args[0];
  const spine = JSON.parse(readFileSync(join(moduleDir, 'spine.json'), 'utf8'));
  save({ module: spine.title, moduleDir, flags: { ...spine.flags_init }, ended: null, log: [`[init] ${spine.title}`] });
  out({ ok: true, module: spine.title, flags: spine.flags_init });
} else if (cmd === 'get') {
  out(load());
} else if (cmd === 'set') {
  const [flag, rawVal, note] = args;
  const value = parseVal(rawVal);
  const s = load(); const spine = spineOf(s);
  if (s.ended) { out({ ok: false, reason: `story already ended: ${s.ended}` }); process.exit(1); }
  if (!(flag in s.flags)) { out({ ok: false, reason: `unknown flag '${flag}' — spine declares: ${Object.keys(s.flags).join(', ')}` }); process.exit(1); }
  const fails = checkGates(spine, s.flags, flag, value);
  const fatal = fails.filter((f) => f.severity !== 'medium');
  if (fatal.length) { out({ ok: false, guard: 'REJECTED', violations: fatal }); process.exit(1); }
  s.flags[flag] = value;
  s.log.push(`[flag] ${flag}=${JSON.stringify(value)}${note ? ' (' + note + ')' : ''}`);
  save(s);
  out({ ok: true, flags: s.flags, warnings: fails });
} else if (cmd === 'scene') {
  const s = load();
  s.flags.scene = args[0];
  s.log.push(`[scene] ${args[0]}${args[1] ? ' (' + args[1] + ')' : ''}`);
  save(s);
  out({ ok: true, scene: args[0] });
} else if (cmd === 'ending') {
  const id = args[0];
  const s = load(); const spine = spineOf(s);
  const e = spine.endings[id];
  if (!e) { out({ ok: false, guard: 'REJECTED', reason: `'${id}' is not in the ending contract. ${spine.no_clean_win} Legal: ${Object.keys(spine.endings).join(', ')}` }); process.exit(1); }
  const unmet = Object.entries(e.precond).filter(([f, c]) => !cmp(s.flags[f], c));
  if (unmet.length) { out({ ok: false, guard: 'REJECTED', reason: `ending '${id}' precondition unmet: ${unmet.map(([f]) => f).join(', ')}`, means: e.means }); process.exit(1); }
  s.ended = id;
  s.log.push(`[ENDING] ${id} — ${e.means}`);
  save(s);
  out({ ok: true, ending: id, means: e.means });
} else if (cmd === 'recap') {
  const s = load(); const spine = spineOf(s);
  const act = s.flags.act; const g = spine.narrator?.by_act?.[String(act)];
  console.log(`${s.module} — Act ${act}${g ? ' (' + g.title + ')' : ''}, scene: ${s.flags.scene}.${s.ended ? ' ENDED: ' + s.ended : ''}`);
  console.log(`Recent: ${s.log.slice(-3).join(' | ')}`);
} else if (cmd === 'card') {
  const s = load();
  const p = join(s.moduleDir, 'scenes', `${s.flags.scene}.md`);
  console.log(existsSync(p) ? readFileSync(p, 'utf8') : `(no scene card authored for '${s.flags.scene}')`);
  const spine = spineOf(s); const g = spine.narrator?.by_act?.[String(s.flags.act)];
  if (g) console.log(`\n--- Act ${s.flags.act}: ${g.title} (latitude ${g.latitude}) ---\n` + g.establish.map((e) => '• ' + e).join('\n'));
} else {
  console.log('usage: story.mjs init|get|set|scene|ending|recap|card');
  process.exit(1);
}
