#!/usr/bin/env node
/**
 * Extract the DSH (cordis) event catalog from the installed app.asar.
 *
 * Zero dependencies. Three extraction passes, matching M0 of
 * `dsh-event-bus-full-alignment-brief.md`:
 *
 *   1. call sites    ctx.(emit|parallel|serial|bail|waterfall|on|once)([thisArg,] "name")
 *   2. declarations  `interface Events { ... }` members (TypeScript sources only)
 *   3. literals      any `"namespace/name"` string literal
 *
 * Outputs `docs/spec/dsh-event-catalog.md` with a deterministic body so that
 * `--check` can fail when the asar or the extraction rules drift.
 *
 * Usage:
 *   node scripts/extract_dsh_events.mjs                 # write the catalog
 *   node scripts/extract_dsh_events.mjs --check         # verify, exit 1 on drift
 *   node scripts/extract_dsh_events.mjs --json          # machine summary
 *   node scripts/extract_dsh_events.mjs --refresh       # re-extract app.asar
 */
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
const DEFAULTS = {
  asar: 'D:/deepseekharness/resources/app.asar',
  dir: 'D:/dshworkspace/.dsh-tmp/dsh-full',
  out: path.join(ROOT, 'docs/spec/dsh-event-catalog.md'),
  records: path.join(ROOT, 'src/deeppyro/core/wire.py'),
  hooks: path.join(ROOT, 'src/deeppyro/hooks/types.py'),
  partial: 'D:/dshworkspace/dsh-event-catalog-partial.md',
  maxSites: 6,
};

const MODES = ['emit', 'parallel', 'serial', 'bail', 'waterfall', 'on', 'once'];

function parseArgs(argv) {
  const opts = { ...DEFAULTS, check: false, json: false, refresh: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--check') opts.check = true;
    else if (arg === '--dump') opts.dump = argv[++i];
    else if (arg === '--json') opts.json = true;
    else if (arg === '--refresh') opts.refresh = true;
    else if (arg.startsWith('--')) {
      const [flag, inline] = arg.slice(2).split('=', 2);
      const value = inline ?? argv[++i];
      if (!(flag in opts)) throw new Error(`unknown option --${flag}`);
      opts[flag] = value;
    } else throw new Error(`unexpected argument ${arg}`);
  }
  return opts;
}

// -- asar ------------------------------------------------------------------

function readAsar(file) {
  const fd = fs.openSync(file, 'r');
  const head = Buffer.alloc(16);
  fs.readSync(fd, head, 0, 16, 0);
  const headerSize = head.readUInt32LE(12);
  const raw = Buffer.alloc(headerSize);
  fs.readSync(fd, raw, 0, headerSize, 16);
  const header = JSON.parse(raw.toString('utf8'));
  const base = 16 + headerSize;
  const entries = [];
  const walk = (node, prefix) => {
    for (const [name, value] of Object.entries(node.files || {})) {
      const rel = prefix ? `${prefix}/${name}` : name;
      if (value.files) walk(value, rel);
      else if (typeof value.size === 'number' && !value.unpacked) entries.push({ rel, ...value });
    }
  };
  walk(header, '');
  return { fd, base, entries, bytes: fs.statSync(file).size };
}

function extract(asar, dir, { refresh }) {
  const marker = path.join(dir, '.extracted');
  if (!refresh && fs.existsSync(marker)) {
    const text = fs.readFileSync(marker, 'utf8').trim().split('\n');
    return { files: Number(text[0] || 0), bytes: Number(text[1] || 0), cached: true };
  }
  const { fd, base, entries, bytes } = readAsar(asar);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  let written = 0;
  let total = 0;
  for (const entry of entries) {
    if (!entry.size) continue;
    const dest = path.join(dir, entry.rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    const buf = Buffer.alloc(entry.size);
    fs.readSync(fd, buf, 0, entry.size, base + Number(entry.offset));
    fs.writeFileSync(dest, buf);
    written += 1;
    total += entry.size;
  }
  fs.closeSync(fd);
  fs.writeFileSync(marker, `${written}\n${total}\n${bytes}\n`, 'utf8');
  return { files: written, bytes: total, asarBytes: bytes, cached: false };
}

// -- scanning ---------------------------------------------------------------

const SOURCE = /\.(?:m?js|cjs|jsx|ts|mts|cts|tsx)$/;
const NAME = /^[a-z][a-z0-9.-]*(?:\/[A-Za-z0-9_.:${}-]+)+$/;

function walkFiles(root) {
  const out = [];
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else if (SOURCE.test(entry.name)) out.push(full);
    }
  }
  return out.sort();
}

function lineIndex(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i += 1) if (text.charCodeAt(i) === 10) starts.push(i + 1);
  return (offset) => {
    let lo = 0;
    let hi = starts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (starts[mid] <= offset) lo = mid;
      else hi = mid - 1;
    }
    return lo + 1;
  };
}

// DSH dispatches through wrappers (`this.dispatch.waterfall("agent/pre-step", …)`) as
// well as through the cordis context (`ctx.on(...)`), so match any receiver chain.
const RECEIVER = String.raw`[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*`;
const CALL = new RegExp(
  String.raw`\b(${RECEIVER})\s*\.\s*(${MODES.join('|')})\s*\(\s*(?:[A-Za-z_$][\w$.[\]]*\s*,\s*)?"([^"\\]{1,120})"`,
  'g',
);
// Only cordis-shaped receivers count toward the unresolved-call risk: generic
// `.emit(...)` on streams and event emitters is not the cordis bus.
const ANY_CALL = new RegExp(
  String.raw`\b(?:[A-Za-z_$][\w$]*\.)*(?:ctx|dispatch|events)\s*\.\s*(?:${MODES.join('|')})\s*\(`,
  'g',
);
const LITERAL = /["'`]([a-z][a-z0-9.-]*(?:\/[A-Za-z0-9_.:-]+)+)["'`]/g;

// `.on(name, handler)` and `.once(name, handler)` subscribe; the handler arity
// tells us how the event is dispatched: cordis hands waterfall handlers their
// extra arguments plus a trailing `next`. Subscription verbs therefore must not
// be recorded as dispatch modes.
function handlerMode(tail) {
  const arrow = tail.match(/^\s*,\s*(?:async\s+)?(?:function\s*)?\(([^)]*)\)\s*(?:=>|\{)/);
  if (arrow) {
    const params = arrow[1].split(',').map((part) => part.trim()).filter(Boolean);
    if (params.length === 0) return null;
    return params[params.length - 1] === 'next' ? 'waterfall' : null;
  }
  return null;
}

function scanSource(text, rel, state) {
  const lineOf = lineIndex(text);
  for (const match of text.matchAll(ANY_CALL)) state.anyCalls += 1;
  for (const match of text.matchAll(CALL)) {
    const [, receiver, mode, name] = match;
    if (!NAME.test(name)) continue;
    const site = `${rel}:${lineOf(match.index)}`;
    const record = state.names.get(name) ?? { name, modes: new Set(), subscribed: new Set(), sites: [], literals: 0, declared: null, files: new Set(), receivers: new Set() };
    if (mode === 'on' || mode === 'once') {
      record.subscribed.add(mode);
      const tail = text.slice(match.index + match[0].length, match.index + match[0].length + 400);
      const inferred = handlerMode(tail);
      if (inferred) record.modes.add(inferred);
    } else {
      record.modes.add(mode);
    }
    record.receivers.add(receiver);
    record.files.add(rel);
    if (record.sites.length < 400) record.sites.push(site);
    state.names.set(name, record);
    state.callSites += 1;
  }
  for (const match of text.matchAll(LITERAL)) {
    const name = match[1];
    if (!NAME.test(name)) continue;
    if (name.includes('${')) continue;
    const record = state.names.get(name) ?? { name, modes: new Set(), subscribed: new Set(), sites: [], literals: 0, declared: null, files: new Set(), receivers: new Set() };
    record.literals += 1;
    record.files.add(rel);
    state.names.set(name, record);
  }
}

function scanDeclarations(text, rel, state) {
  // A package can augment the same `interface Events` more than once (module
  // declarations per subpath), so every block counts, not just the first.
  const pattern = /interface\s+Events\b[^{]*\{/g;
  let match;
  while ((match = pattern.exec(text))) {
    const open = text.indexOf('{', match.index);
    let depth = 0;
    let close = text.length - 1;
    for (let i = open; i < text.length; i += 1) {
      const ch = text[i];
      if (ch === '{') depth += 1;
      else if (ch === '}') {
        depth -= 1;
        if (depth === 0) {
          close = i;
          break;
        }
      }
    }
    collectMembers(text.slice(open + 1, close), rel, state);
    pattern.lastIndex = close;
  }
}

function collectMembers(body, rel, state) {
  // Both shapes matter: object members (`'name': Payload`) and method signatures
  // (`'name'(args): Ret`), plus the JSDoc `@mode` tag cordis uses to document dispatch.
  const lines = body.split('\n');
  let comment = [];
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith('/**') || trimmed.startsWith('*') || trimmed.startsWith('/*')) {
      comment.push(trimmed.replace(/^\/?\*+\/?/, '').trim());
      continue;
    }
    const member = trimmed.match(/^['"]([a-z][a-z0-9.-]*(?:\/[A-Za-z0-9_.:-]+)+)['"]\s*(?::|\(|\?\s*:)/);
    if (!member) {
      if (trimmed.endsWith('*/') || (trimmed && !trimmed.startsWith('*'))) comment = [];
      continue;
    }
    const name = member[1];
    const payload = trimmed.replace(/,$/, '').replace(/\s+/g, ' ').slice(0, 160);
    const modeTag = comment.map((c) => c.match(/^@mode\s+(\w+)/)).find(Boolean);
    const proseMode = comment.join(' ').match(/\b(Waterfall|Bail|Serial|Parallel|Emit)\b/);
    const record = state.names.get(name) ?? {
      name,
      modes: new Set(),
      sites: [],
      literals: 0,
      declared: null,
      files: new Set(),
      receivers: new Set(),
    };
    record.declared = record.declared ?? payload;
    if (modeTag) record.modes.add(modeTag[1].toLowerCase());
    else if (proseMode) record.modes.add(proseMode[1].toLowerCase());
    record.apiDeclared = true;
    record.files.add(rel);
    state.names.set(name, record);
    comment = [];
  }
}

// -- deeppyro record names --------------------------------------------------

function readRecordTypes(file) {
  const text = fs.readFileSync(file, 'utf8');
  const block = text.match(/RECORD_TYPES\s*=\s*\(([\s\S]*?)\n\)/);
  if (!block) throw new Error(`RECORD_TYPES not found in ${file}`);
  const names = new Set();
  for (const match of block[1].matchAll(/["']([^"']+)["']/g)) names.add(match[1]);
  return names;
}

function relPath(file) {
  return path.relative(ROOT, file).split(path.sep).join('/');
}

function readHookNames(file) {
  if (!fs.existsSync(file)) return [];
  const text = fs.readFileSync(file, 'utf8');
  const modes = new Map();
  const block = text.match(/HOOK_MODES\s*:\s*dict\[str, str\]\s*=\s*\{([\s\S]*?)\n\}/);
  if (block) {
    for (const match of block[1].matchAll(/([A-Z][A-Z0-9_]*)\s*:\s*"([a-z]+)"/g)) modes.set(match[1], match[2]);
  }
  const out = [];
  for (const match of text.matchAll(/^([A-Z][A-Z0-9_]*)\s*=\s*"([^"]+)"$/gm)) {
    const [, constant, name] = match;
    if (!name.includes('/')) continue;
    out.push([name, modes.get(constant) ?? '']);
  }
  return [...new Map(out).entries()];
}

function readPartialNames(file) {
  if (!fs.existsSync(file)) return new Set();
  const names = new Set();
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const cell = line.match(/^\|[^|]*\|\s*`([^`]+)`\s*\|/);
    if (cell) names.add(cell[1]);
  }
  return names;
}

// -- render -----------------------------------------------------------------

const INTERNAL_KNOWN = new Set([
  'internal/plugin',
  'internal/status',
  'internal/service',
  'internal/update',
  'internal/commit',
  'internal/config',
  'internal/get',
  'internal/set',
  'internal/listener',
  'internal/dispatch',
]);

function classify(name, record, records) {
  const hasCall = record.modes.size > 0 || record.subscribed.size > 0 || Boolean(record.declared);
  if (INTERNAL_KNOWN.has(name)) return 'internal';
  if (hasCall) return 'bus';
  if (records.has(name)) return 'record';
  return 'noise';
}

function mdEscape(text) {
  return String(text).replace(/\|/g, '\\|');
}

function render(state, opts, scan) {
  const names = [...state.names.values()].sort((a, b) => a.name.localeCompare(b.name));
  const buckets = { bus: [], internal: [], record: [], noise: [] };
  for (const record of names) buckets[classify(record.name, record, state.records)].push(record);
  const overlap = buckets.bus.filter((r) => state.records.has(r.name)).map((r) => r.name);
  const lines = [];
  const push = (...parts) => lines.push(...parts);

  push('# DSH event catalog (generated)', '');
  push('Generated by `scripts/extract_dsh_events.mjs`; do not edit by hand. M0 artifact of');
  push('`dsh-event-bus-full-alignment-brief.md`: it is the reference for `EVENT_SPECS` (M2), not a');
  push('runtime contract by itself.', '');
  push('## Source', '');
  push(`- app.asar: \`${opts.asar}\` (${state.asarBytes.toLocaleString('en-US')} bytes, read-only)`);
  push(`- extracted: ${scan.files.toLocaleString('en-US')} files, ${scan.bytes.toLocaleString('en-US')} bytes${scan.cached ? ' (cached)' : ''}`);
  push(`- scanned: ${state.scanned.toLocaleString('en-US')} JS/TS files`);
  push(`- deeppyro record types: \`${path.relative(ROOT, opts.records)}\` (${state.records.size} names)`);
  push(`- partial reference: \`${opts.partial}\` (${state.partial.size} names)`, '');

  push('## Statistics', '');
  push('| bucket | names | meaning |');
  push('| --- | --- | --- |');
  push(`| bus | ${buckets.bus.length} | has a \`ctx.*\` call site or an \`interface Events\` member |`);
  push(`| internal | ${buckets.internal.length} | \`internal/*\` meta events (always bus, tracked apart) |`);
  push(`| record | ${buckets.record.length} | session-log record name, never a bus event |`);
  push(`| noise | ${buckets.noise.length} | literal only, no call site, not a record |`);
  push(`| **total** | **${names.length}** | distinct \`namespace/name\` strings |`, '');
  push(`- resolved call sites: ${state.callSites}`);
  push(`- unresolved \`ctx.<mode>(\` calls (template literal or computed name): ${state.anyCalls - state.callSites}`);
  push(`- names declared in \`interface Events\`: ${names.filter((r) => r.declared).length}`);
  push(`- names that are both bus event and deeppyro record: ${overlap.length}${overlap.length ? ` (${overlap.join(', ')})` : ''}`, '');

  push('## Catalog', '');
  const table = (rows) => {
    push('| namespace | name | modes | receiver | call sites | payload |');
    push('| --- | --- | --- | --- | --- | --- |');
    for (const record of rows) {
      const ns = record.name.split('/')[0];
      const modes = record.modes.size ? [...record.modes].sort().join('/') : '—';
      const receivers = record.receivers && record.receivers.size ? [...record.receivers].sort().join(', ') : '—';
      const sites = record.sites.length
        ? record.sites.slice(0, opts.maxSites).map((s) => `\`${mdEscape(s)}\``).join('<br>')
        : '—';
      const payload = record.declared ? `\`${mdEscape(record.declared)}\`` : 'unknown (M2 evidence)';
      push(`| ${ns} | \`${record.name}\` | ${modes} | ${receivers} | ${sites} | ${payload} |`);
    }
    push('');
  };
  push(`### Bus events (${buckets.bus.length})`, '');
  table(buckets.bus);
  push(`### internal/* (${buckets.internal.length})`, '');
  table(buckets.internal);
  push(`### Session records — excluded by design (${buckets.record.length})`, '');
  push('These names exist in the session log vocabulary; per brief D2 they stay out of the bus.', '');
  push(buckets.record.map((r) => `\`${r.name}\``).join(', '), '');
  push(`### Unclassified literals (${buckets.noise.length})`, '');
  push('Literal-only names: no `ctx.*` call site, not a deeppyro record. They group into Node/JS');
  push('internals, MIME types, package-relative paths and cordis helper names. They are listed by');
  push('namespace here; `--dump` carries the full list for M2 triage.', '');
  const noiseByNs = new Map();
  for (const record of buckets.noise) {
    const ns = record.name.split('/')[0];
    const entry = noiseByNs.get(ns) ?? [];
    entry.push(record.name);
    noiseByNs.set(ns, entry);
  }
  push('| namespace | literals | sample |');
  push('| --- | --- | --- |');
  for (const [ns, list] of [...noiseByNs.entries()].sort((a, b) => b[1].length - a[1].length)) {
    const sample = list.slice(0, 4).map((name) => `\`${name}\``).join(', ');
    push(`| ${ns} | ${list.length} | ${sample}${list.length > 4 ? ', …' : ''} |`);
  }
  push('');

  push('## deeppyro hook names against the catalog', '');
  push('The 19 names deeppyro dispatches today (M1 keeps them as deprecated aliases). `DSH modes` are');
  push('the modes actually observed at call sites, so a dash means the catalog cannot confirm the name —');
  push('M2 must supply a `dsh_ref` or mark it `reserved`.', '');
  push('| deeppyro name | deeppyro mode | DSH modes | DSH call sites |');
  push('| --- | --- | --- | --- |');
  for (const [name, mode] of state.hooks) {
    const record = state.names.get(name);
    const dshModes = record && record.modes.size ? [...record.modes].sort().join('/') : '—';
    const sites = record && record.sites.length ? String(record.sites.length) : '0';
    push(`| \`${name}\` | ${mode || '—'} | ${dshModes} | ${sites} |`);
  }
  push('');

  push('## Namespace summary', '');
  const byNs = new Map();
  for (const record of names) {
    const ns = record.name.split('/')[0];
    const entry = byNs.get(ns) ?? { all: 0, bus: 0 };
    entry.all += 1;
    if (record.modes.size || record.subscribed.size) entry.bus += 1;
    byNs.set(ns, entry);
  }
  push('| namespace | names | with call site |');
  push('| --- | --- | --- |');
  for (const [ns, entry] of [...byNs.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    push(`| ${ns} | ${entry.all} | ${entry.bus} |`);
  }
  push('');

  push('## Diff against the partial catalog', '');
  const ours = new Set(names.map((r) => r.name));
  const missing = [...state.partial].filter((name) => !ours.has(name)).sort();
  const added = [...ours].filter((name) => !state.partial.has(name)).sort();
  push(`- in partial but not found here: ${missing.length}${missing.length ? ` (${missing.slice(0, 12).join(', ')}${missing.length > 12 ? ', …' : ''})` : ''}`);
  push(`- found here but not in partial: ${added.length}${added.length ? ` (${added.slice(0, 12).join(', ')}${added.length > 12 ? ', …' : ''})` : ''}`);
  push('- the partial catalog was a wide scan of 867 files; a superset here is expected, a subset is not.', '');

  const dispatchModes = new Set(['emit', 'serial', 'parallel', 'bail', 'waterfall']);
  const subscribeOnly = buckets.bus.filter(
    (record) => record.modes.size > 0 && ![...record.modes].some((mode) => dispatchModes.has(mode)),
  );
  const declaredOnly = buckets.bus.filter((record) => record.modes.size === 0);
  push('## Evidence gaps', '');
  push(
    `${subscribeOnly.length} bus names are only ever *subscribed* in the shipped code and ` +
      `${declaredOnly.length} have no call site at all (their only evidence is an ` +
      '`interface Events` member). Both groups are the M2 review list: subscribing proves the ' +
      'name exists, not who fires it.',
    '',
  );
  if (subscribeOnly.length) {
    push('| subscribe-only name | sites |', '| --- | --- |');
    for (const record of subscribeOnly) {
      push(`| \`${mdEscape(record.name)}\` | ${record.sites.length} |`);
    }
    push('');
  }
  if (declaredOnly.length) {
    push('| declared-only name | payload signature |', '| --- | --- |');
    for (const record of declaredOnly) {
      push(`| \`${mdEscape(record.name)}\` | ${record.declared ? `\`${mdEscape(record.declared)}\`` : '—'} |`);
    }
    push('');
  }

  push('## Risks and limits', '');
  push('1. Compiled JavaScript erases the `Events` interface, so payload types are `unknown` unless a');
  push('   `.d.ts` survives in the asar. M2 must take payload evidence from call-site arguments.');
  push('2. Computed event names (`ctx.emit(\`${ns}/${name}\`)`) cannot be resolved statically; the count');
  push('   above is the residual risk for "P0 missing = 0".');
  push('3. A literal without a call site may still be a real event dispatched through a wrapper helper');
  push('   (`ctx[name]`, `events.dispatch`), or a record/const. These stay in "unclassified".');
  push('4. `approval/asked` and `approval/decided` are both bus events and session records. The bus copy');
  push('   is catalogued here; the record copy stays in the frozen 62-name vocabulary.');
  push('5. Presence of a name does not prove a consumer exists in DSH; `modes` only says how it is called.', '');

  return `${lines.join('\n')}\n`;
}

// -- main -------------------------------------------------------------------

function build(opts) {
  const scan = extract(opts.asar, opts.dir, opts);
  const files = walkFiles(opts.dir);
  const state = {
    names: new Map(),
    records: readRecordTypes(opts.records),
    hooks: readHookNames(opts.hooks),
    partial: readPartialNames(opts.partial),
    callSites: 0,
    anyCalls: 0,
    scanned: files.length,
    asarBytes: Number(fs.readFileSync(path.join(opts.dir, '.extracted'), 'utf8').trim().split('\n')[2] || 0),
  };
  for (const file of files) {
    const rel = path.relative(opts.dir, file).split(path.sep).join('/');
    let text;
    try {
      text = fs.readFileSync(file, 'utf8');
    } catch {
      continue;
    }
    if (text.length > 4_000_000) continue;
    scanSource(text, rel, state);
    if (/\.(?:ts|mts|cts|tsx)$/.test(file)) scanDeclarations(text, rel, state);
  }
  return { state, scan, markdown: render(state, opts, scan) };
}

function summary(state) {
  const names = [...state.names.values()];
  const count = (predicate) => names.filter(predicate).length;
  return {
    total: names.length,
    bus: count((r) => classify(r.name, r, state.records) === 'bus'),
    internal: count((r) => r.name.startsWith('internal/')),
    record: count((r) => classify(r.name, r, state.records) === 'record'),
    noise: count((r) => classify(r.name, r, state.records) === 'noise'),
    callSites: state.callSites,
    unresolvedCalls: state.anyCalls - state.callSites,
    scannedFiles: state.scanned,
  };
}

const opts = parseArgs(process.argv.slice(2));
const { state, markdown } = build(opts);
if (opts.dump) {
  const names = [...state.names.values()]
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((record) => ({
      name: record.name,
      namespace: record.name.split('/')[0],
      bucket: classify(record.name, record, state.records),
      modes: [...record.modes].sort(),
    subscribed: [...record.subscribed].sort(),
      sites: record.sites,
      literalCount: record.literals,
      declared: record.declared,
      files: [...record.files].sort(),
    }));
  fs.mkdirSync(path.dirname(opts.dump), { recursive: true });
  fs.writeFileSync(opts.dump, `${JSON.stringify({ summary: summary(state), names }, null, 2)}\n`, 'utf8');
  console.log(`wrote ${relPath(opts.dump)}`);
} else if (opts.json) {
  console.log(JSON.stringify(summary(state), null, 2));
} else if (opts.check) {
  const current = fs.existsSync(opts.out) ? fs.readFileSync(opts.out, 'utf8') : '';
  if (current === markdown) {
    console.log(`OK ${relPath(opts.out)} is up to date`);
  } else {
    console.error(`DRIFT ${relPath(opts.out)} differs from the generator`);
    process.exitCode = 1;
  }
} else {
  fs.mkdirSync(path.dirname(opts.out), { recursive: true });
  fs.writeFileSync(opts.out, markdown, 'utf8');
  console.log(`wrote ${relPath(opts.out)}`);
  console.log(JSON.stringify(summary(state)));
}