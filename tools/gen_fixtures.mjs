
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const HOME = process.env.USERPROFILE || 'C:/Users/74185';
const SESS_ROOT = path.join(HOME, '.dsh', 'sessions');
const OUT = 'D:/code/deeppyro/fixtures';
const SESS_OUT = path.join(OUT, 'sessions');
const TYPE_OUT = path.join(OUT, 'records', 'by-type');
fs.mkdirSync(SESS_OUT, { recursive: true });
fs.mkdirSync(TYPE_OUT, { recursive: true });

function walk(dir, acc = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, acc);
    else if (e.name.startsWith('session.v4.jsonl')) acc.push(p);
  }
  return acc;
}

function decode(file) {
  const buf = fs.readFileSync(file);
  if (file.endsWith('.zstd')) return zlib.zstdDecompressSync(buf).toString('utf8');
  if (file.endsWith('.gz')) return zlib.gunzipSync(buf).toString('utf8');
  return buf.toString('utf8');
}

function safeName(t) { return t.replace(/[^a-zA-Z0-9]+/g, '_'); }

const files = walk(SESS_ROOT);
const manifest = { generatedAt: new Date().toISOString(), sourceRoot: SESS_ROOT, sessions: [], typeSamples: {} };

// full-copy threshold on decoded size
const FULL_LIMIT = 3_000_000;
// trimmed big sessions: <= this many records
const TRIM_MAX = 160;

for (const file of files.sort()) {
  let text;
  try { text = decode(file); } catch (e) { console.error('decode failed', file, e.message); continue; }
  const lines = text.split('\n').filter(l => l.trim().length > 0);
  let header;
  try { header = JSON.parse(lines[0]); } catch { console.error('bad header', file); continue; }
  if (header.type !== 'session') { console.error('not a session header', file); continue; }

  const records = [];
  let bad = 0;
  for (const l of lines.slice(1)) { try { records.push(JSON.parse(l)); } catch { bad++; } }

  const hist = {}; let seqBreaks = 0; let prev = null;
  for (const r of records) {
    hist[r.type] = (hist[r.type] || 0) + 1;
    if (prev !== null && r.seq !== prev + 1) seqBreaks++;
    prev = r.seq;
  }

  const decodedSize = text.length;
  const id = header.id;
  const outName = id + '.jsonl';
  let trimmed = false;
  let kept = records;
  if (decodedSize > FULL_LIMIT) {
    trimmed = true;
    if (records.length <= TRIM_MAX) kept = records;
    else {
      const head = records.slice(0, 70);
      const tail = records.slice(records.length - 70);
      kept = [...head, ...tail];
    }
  }
  const outLines = [JSON.stringify(header), ...kept.map(r => JSON.stringify(r))];
  fs.writeFileSync(path.join(SESS_OUT, outName), outLines.join('\n') + '\n', 'utf8');

  // first sample of each record type (prefer the trimmed/kept set, else any)
  for (const r of records) {
    const key = safeName(r.type);
    if (!manifest.typeSamples[key]) {
      manifest.typeSamples[key] = { type: r.type, sessionId: id, sample: r };
      fs.writeFileSync(path.join(TYPE_OUT, key + '.jsonl'), JSON.stringify(r) + '\n', 'utf8');
    }
  }

  manifest.sessions.push({
    id, file: outName, source: path.relative(SESS_ROOT, file).replaceAll('\\', '/'),
    cwd: header.cwd, agentPreset: header.agentPreset ?? null, delegationDepth: header.delegationDepth ?? 0,
    parentSession: header.parentSession ?? null, origin: header.origin ?? null,
    createdAt: header.createdAt, recordCount: records.length, keptCount: kept.length,
    decodedSize, trimmed, badLines: bad, seqBreaks, types: hist,
  });
}

fs.writeFileSync(path.join(OUT, 'manifest.json'), JSON.stringify(manifest, null, 1), 'utf8');
console.log(JSON.stringify({
  sessions: manifest.sessions.length,
  types: Object.keys(manifest.typeSamples).length,
  fullDecodedLines: manifest.sessions.reduce((a, s) => a + s.recordCount, 0),
}, null, 1));
