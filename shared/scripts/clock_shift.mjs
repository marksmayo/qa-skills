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
// replaces this clock while they are on. Prefer the repo's own injected clock when it has one.
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

export const OFFSET_MS = install(parseOffset());

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
