// Moves the JavaScript clock for a whole test run, for confirming time bombs without libfaketime
// (Node 18+, no dependencies). The JavaScript counterpart of clock_shift.py, driven by the same variables:
//
//   TDA_CLOCK_SHIFT=+400d   shift by days (or +6h, -30d, +90m)
//   TDA_CLOCK_DATE=2027-01-31   jump to a date, keeping the local time of day
//
// Load it before the tests import anything, in either way:
//   NODE_OPTIONS="--import=file:///abs/path/clock_shift.mjs" npx vitest run     (any Node runner; reaches
//       child processes and Vitest's forks and threads pools; on Windows give a file:// URL)
//   or list it first in Vitest's setupFiles (test.setupFiles: ['./clock_shift.mjs', ...]) in a scratch
//       config that extends the repo's, so the repo's own setup file still runs.
//
// It replaces the global Date so `Date.now()`, `new Date()` and `Date()` return the moved time; Dates built
// from explicit arguments are unchanged, and `instanceof Date` still holds. It does not change the time
// zone: set TZ to an IANA name in the runner's environment for that (a TZ assigned inside a setup file
// does not reach Vitest's worker threads). performance.now(), timers, other processes, databases and
// browsers keep the real clock, and a test that installs fake timers (vi.useFakeTimers, jest fake timers)
// replaces this clock while they are on. File times reported by fs.stat and friends move with the clock (so
// lock and cache ages stay consistent); set TDA_CLOCK_FILES=0 to keep them real. Other processes (git, a
// database, a child that does not load this module) still see the real file times. Prefer the repo's own
// injected clock when it has one.
//
// Self-test: node clock_shift.mjs --self-test
import { pathToFileURL, fileURLToPath } from 'node:url';

const MARK = Symbol.for('qa-skills.clock_shift');

export function parseOffset(env = process.env, realNow = Date.now()) {
  const shift = env.TDA_CLOCK_SHIFT, target = env.TDA_CLOCK_DATE;
  if (target) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(target.trim());
    if (!m) throw new Error(`TDA_CLOCK_DATE must look like 2027-01-31, not ${JSON.stringify(target)}`);
    const now = new Date(realNow);
    const want = new Date(realNow);
    want.setFullYear(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return want.getTime() - now.getTime();
  }
  if (shift) {
    const m = /^([+-]?\d+)([dhm])$/.exec(shift.trim());
    if (!m) throw new Error(`TDA_CLOCK_SHIFT must look like +400d, -6h or +30m, not ${JSON.stringify(shift)}`);
    return Number(m[1]) * { d: 86400000, h: 3600000, m: 60000 }[m[2]];
  }
  return 0;
}

export function install(offset) {
  const current = globalThis.Date;
  if (current[MARK] !== undefined) return current[MARK];   // already installed (setup file and --import both)
  if (!offset) return 0;
  const RealDate = current;
  function ShiftedDate(...args) {
    if (!new.target) return new RealDate(RealDate.now() + offset).toString();
    return Reflect.construct(RealDate, args.length ? args : [RealDate.now() + offset], new.target);
  }
  ShiftedDate.prototype = RealDate.prototype;
  Object.setPrototypeOf(ShiftedDate, RealDate);
  Object.defineProperty(ShiftedDate, 'now', { value: () => RealDate.now() + offset, writable: true, configurable: true });
  Object.defineProperty(ShiftedDate, MARK, { value: offset });
  Object.defineProperty(ShiftedDate, 'name', { value: 'Date' });
  globalThis.Date = ShiftedDate;
  return offset;
}

// File times: a test that ages a file (`Date.now() - statSync(f).mtimeMs`, lock or cache expiry) would compare
// the moved clock with the real file system clock and see every fresh file as hundreds of days old. So the stat
// family (fs.stat/lstat/fstat, their Sync forms, fs.promises and FileHandle.stat) report atime, mtime, ctime and
// birthtime moved by the same offset, and the utimes family (utimes, futimes, lutimes) moves times the tests set
// back by it, so a round trip is exact. TDA_CLOCK_FILES=0 leaves file times alone.
const FILE_MARK = Symbol.for('qa-skills.clock_shift.files');

function shiftStats(st, offset) {
  if (!st || typeof st !== 'object' || typeof st.mtimeMs === 'undefined') return st;
  const big = typeof st.mtimeMs === 'bigint';
  for (const k of ['atime', 'mtime', 'ctime', 'birthtime']) {
    const ms = st[k + 'Ms'];
    if (ms === undefined) continue;
    const moved = big ? ms + BigInt(Math.trunc(offset)) : ms + offset;
    Object.defineProperty(st, k + 'Ms', { value: moved, writable: true, enumerable: true, configurable: true });
    if (big && st[k + 'Ns'] !== undefined) {
      Object.defineProperty(st, k + 'Ns', { value: st[k + 'Ns'] + BigInt(Math.trunc(offset)) * 1000000n,
        writable: true, enumerable: true, configurable: true });
    }
    Object.defineProperty(st, k, { value: new Date(Number(moved)), writable: true, enumerable: true, configurable: true });
  }
  return st;
}

function unshiftTime(t, offset) {
  if (t instanceof Date) return new Date(t.getTime() - offset);
  if (typeof t === 'number') return t - offset / 1000;                  // utimes takes seconds
  if (typeof t === 'bigint') return t - BigInt(Math.trunc(offset / 1000));
  if (typeof t === 'string' && /^\d+(\.\d+)?$/.test(t)) return String(Number(t) - offset / 1000);
  return t;
}

export async function installFiles(offset) {
  if (!offset) return false;
  const { createRequire } = await import('node:module');
  const require = createRequire(import.meta.url);
  const fs = require('node:fs');
  if (fs[FILE_MARK]) return true;
  const wrapSync = name => {
    const real = fs[name];
    if (typeof real === 'function') fs[name] = function (...a) { return shiftStats(real.apply(this, a), offset); };
  };
  const wrapCb = name => {
    const real = fs[name];
    if (typeof real !== 'function') return;
    fs[name] = function (...a) {
      const cb = a[a.length - 1];
      if (typeof cb === 'function') a[a.length - 1] = (err, st) => cb(err, err ? st : shiftStats(st, offset));
      return real.apply(this, a);
    };
  };
  const wrapPromise = (obj, name) => {
    const real = obj && obj[name];
    if (typeof real === 'function') obj[name] = async function (...a) { return shiftStats(await real.apply(this, a), offset); };
  };
  const unshiftArgs = (obj, name, first) => {   // (target, atime, mtime[, cb]): the times are arguments first, first+1
    const real = obj && obj[name];
    if (typeof real !== 'function') return;
    obj[name] = function (...a) {
      a[first] = unshiftTime(a[first], offset);
      a[first + 1] = unshiftTime(a[first + 1], offset);
      return real.apply(this, a);
    };
  };
  ['statSync', 'lstatSync', 'fstatSync'].forEach(wrapSync);
  ['stat', 'lstat', 'fstat'].forEach(wrapCb);
  ['stat', 'lstat'].forEach(n => wrapPromise(fs.promises, n));
  ['utimes', 'utimesSync', 'futimes', 'futimesSync', 'lutimes', 'lutimesSync'].forEach(n => unshiftArgs(fs, n, 1));
  ['utimes', 'lutimes'].forEach(n => unshiftArgs(fs.promises, n, 1));
  try {   // FileHandle.prototype: reach it through a handle on this module's own file
    const fh = await fs.promises.open(fileURLToPath(import.meta.url), 'r');
    const proto = Object.getPrototypeOf(fh);
    await fh.close();
    wrapPromise(proto, 'stat');
    unshiftArgs(proto, 'utimes', 0);
  } catch { /* no FileHandle: fine */ }
  Object.defineProperty(fs, FILE_MARK, { value: true });
  require('node:module').syncBuiltinESMExports();   // named imports (import { statSync } from 'node:fs') follow
  return true;
}

export const OFFSET_MS = install(parseOffset());
export const FILES_MOVED = process.env.TDA_CLOCK_FILES === '0' ? false : await installFiles(OFFSET_MS);

if (OFFSET_MS && !process.env.TDA_CLOCK_ANNOUNCED) {
  process.env.TDA_CLOCK_ANNOUNCED = '1';
  const today = new Date().toISOString().slice(0, 10);
  process.stderr.write(`clock_shift: clock moved by ${(OFFSET_MS / 86400000).toFixed(2)} days (today is ${today} UTC)\n`);
}

async function selfTest() {
  const { spawnSync } = await import('node:child_process');
  const self = pathToFileURL(fileURLToPath(import.meta.url)).href;
  const run = (env, code) => spawnSync(process.execPath, [`--import=${self}`, '-e', code],
    { env: { ...process.env, TDA_CLOCK_SHIFT: '', TDA_CLOCK_DATE: '', TDA_CLOCK_ANNOUNCED: '1', ...env }, encoding: 'utf8' });
  const checks = [];
  const check = (name, ok, extra = '') => checks.push([name, ok, extra]);
  const day = 86400000;

  let r = run({ TDA_CLOCK_SHIFT: '+400d' }, 'console.log(JSON.stringify([Date.now(), new Date().getTime(), Date(), new Date(0).getTime(), new Date() instanceof Date, Date.name]))');
  const [now, made, str, epoch, inst, name] = JSON.parse(r.stdout || '[]');
  const real = Date.now();
  check('+400d moves Date.now()', Math.abs(now - real - 400 * day) < 60000, `${(now - real) / day} days`);
  check('+400d moves new Date()', Math.abs(made - real - 400 * day) < 60000);
  check('Date() as a function moves too', typeof str === 'string' && new Date(str).getFullYear() === new Date(real + 400 * day).getFullYear(), str);
  check('explicit dates are unchanged', epoch === 0);
  check('instanceof Date and the name still hold', inst === true && name === 'Date');

  r = run({ TDA_CLOCK_SHIFT: '-6h' }, 'console.log(Date.now())');
  check('-6h', Math.abs(Number(r.stdout) - (Date.now() - 6 * 3600000)) < 60000);

  r = run({ TDA_CLOCK_DATE: '2031-02-28' }, 'const d = new Date(); console.log(d.getFullYear(), d.getMonth() + 1, d.getDate())');
  check('TDA_CLOCK_DATE jumps to the date', r.stdout.trim() === '2031 2 28', r.stdout.trim());

  r = run({ TDA_CLOCK_SHIFT: '+400d' }, `await import(${JSON.stringify(self)}); console.log(Math.round((Date.now() - ${real}) / ${day}))`);
  check('loading twice does not shift twice', r.stdout.trim() === '400', r.stdout.trim() + r.stderr);

  r = run({ TDA_CLOCK_SHIFT: 'tomorrow' }, 'console.log(1)');
  check('a malformed shift is an error', r.status !== 0 && /TDA_CLOCK_SHIFT must look like/.test(r.stderr));

  r = run({}, 'console.log(Math.abs(Date.now() - ' + real + ') < 60000)');
  check('no variable, no change', r.stdout.trim() === 'true');

  const { mkdtempSync, writeFileSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const dir = mkdtempSync(join(tmpdir(), 'clock-shift-'));
  const workerFile = join(dir, 'worker.mjs');
  writeFileSync(workerFile, 'import { parentPort } from "node:worker_threads"; parentPort.postMessage(Date.now());\n');
  r = run({ TDA_CLOCK_SHIFT: '+400d' }, `const { Worker } = require('node:worker_threads');
    new Worker(${JSON.stringify(workerFile)}).on('message', v => console.log(Math.round((v - ${real}) / ${day})));`);
  rmSync(dir, { recursive: true, force: true });
  check('worker threads (started from a file) see the shift', r.stdout.trim() === '400', r.stdout.trim() + r.stderr);

  const fdir = mkdtempSync(join(tmpdir(), 'clock-shift-'));
  const ff = join(fdir, 'lock').replace(/\\/g, '/');
  const ageCode = `import fs, { statSync } from 'node:fs'; import { stat } from 'node:fs/promises';
    fs.writeFileSync(${JSON.stringify(ff)}, 'x');
    const ages = [Date.now() - statSync(${JSON.stringify(ff)}).mtimeMs, Date.now() - fs.lstatSync(${JSON.stringify(ff)}).mtime.getTime(),
                  Date.now() - (await stat(${JSON.stringify(ff)})).mtimeMs,
                  await new Promise(r => fs.stat(${JSON.stringify(ff)}, (e, s) => r(Date.now() - s.mtimeMs)))];
    const set = new Date(Date.now() - 3600000); fs.utimesSync(${JSON.stringify(ff)}, set, set);
    console.log(JSON.stringify({ ages, roundTrip: Math.abs(statSync(${JSON.stringify(ff)}).mtimeMs - set.getTime()) }));`;
  const runEsm = env => spawnSync(process.execPath, [`--import=${self}`, '--input-type=module', '-e', ageCode],
    { env: { ...process.env, TDA_CLOCK_SHIFT: '', TDA_CLOCK_DATE: '', TDA_CLOCK_ANNOUNCED: '1', ...env }, encoding: 'utf8' });
  r = runEsm({ TDA_CLOCK_SHIFT: '+400d' });
  let res = {};
  try { res = JSON.parse(r.stdout); } catch { /* reported below */ }
  check('a file written now is fresh under the moved clock (stat, lstat, promises, callback)',
    Array.isArray(res.ages) && res.ages.every(a => Math.abs(a) < 60000), r.stdout.trim() + r.stderr);
  check('utimes with a moved Date round-trips', typeof res.roundTrip === 'number' && res.roundTrip < 2000, r.stdout.trim());
  r = runEsm({ TDA_CLOCK_SHIFT: '+400d', TDA_CLOCK_FILES: '0' });
  try { res = JSON.parse(r.stdout); } catch { res = {}; }
  check('TDA_CLOCK_FILES=0 leaves file times real', Array.isArray(res.ages) && res.ages[0] > 399 * day, r.stdout.trim() + r.stderr);
  rmSync(fdir, { recursive: true, force: true });

  let failed = 0;
  for (const [n, ok, extra] of checks) {
    if (!ok) failed++;
    console.log(`${ok ? 'PASS' : 'FAIL'} ${n}${extra ? `  [${extra}]` : ''}`);
  }
  console.log(failed ? `${failed} failed` : 'ALL PASS');
  process.exitCode = failed ? 1 : 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href && process.argv.includes('--self-test')) {
  await selfTest();
}
