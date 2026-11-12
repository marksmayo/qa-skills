// A network guard for Node test runs (Vitest, Jest, Mocha, node --test): refuses every connection that isn't
// loopback, and logs it. No dependencies. The Node counterpart of net_guard.py.
//
//   QA_NET_GUARD_LOG=<scratch>/net_guard.log NODE_OPTIONS="--require <abs path>/net_guard.cjs" npx vitest run
//   node net_guard.cjs --self-test
//
// On Windows write the path with forward slashes (C:/scratch/net_guard.cjs): NODE_OPTIONS reads a backslash
// inside quotes as an escape.
//
// It wraps net.Socket.prototype.connect (which http, https, tls, undici's fetch and database drivers all reach)
// and dns.lookup / dns.promises.lookup, so anything other than 127.0.0.0/8, ::1 and localhost fails with
// ECONNREFUSED. NODE_OPTIONS reaches child processes and Vitest's and Jest's fork and thread pools.
//
// The log proves the guard was on: every process writes `net_guard: loaded pid=<pid>` when the guard loads,
// and every refusal adds `net_guard: refused <host>:<port>`. Before claiming "zero refused connections",
// check the log holds a `loaded` line: a guard that never loaded (a wrong path, a shell that rewrote
// NODE_OPTIONS) refuses nothing and logs nothing. Without QA_NET_GUARD_LOG the lines go to stderr only.
'use strict';
const net = require('node:net');
const dns = require('node:dns');
const fs = require('node:fs');

const MARK = Symbol.for('qa-skills.net_guard');
const LOG = process.env.QA_NET_GUARD_LOG;

function say(line) {
  try { process.stderr.write(line + '\n'); } catch (e) { /* closed stderr */ }
  if (LOG) { try { fs.appendFileSync(LOG, line + '\n'); } catch (e) { /* unwritable log */ } }
}

function loopback(host) {
  if (host === undefined || host === null) return true;   // net defaults to localhost
  const h = String(host).replace(/^\[|\]$/g, '').toLowerCase();
  if (h === '' || h === 'localhost' || h.endsWith('.localhost')) return true;
  if (net.isIPv4(h)) return h.startsWith('127.');
  if (net.isIPv6(h)) return h === '::1' || /^::ffff:127\./.test(h);
  return false;
}

function refused(host, port) {
  say(`net_guard: refused ${host}:${port}`);
  const err = new Error(`net_guard: connection to ${host}:${port} refused (only loopback is allowed)`);
  err.code = 'ECONNREFUSED';
  return err;
}

if (!net.Socket.prototype[MARK]) {
  const realConnect = net.Socket.prototype.connect;
  net.Socket.prototype.connect = function (...args) {
    let opts = args[0];
    if (Array.isArray(opts)) opts = opts[0];   // internal normalised form
    let host, port;
    if (opts && typeof opts === 'object') { host = opts.host; port = opts.port; if (opts.path) return realConnect.apply(this, args); }
    else if (typeof opts === 'number' || /^\d+$/.test(String(opts))) { port = opts; host = typeof args[1] === 'string' ? args[1] : undefined; }
    else if (typeof opts === 'string') return realConnect.apply(this, args);   // a Unix socket or pipe path
    if (!loopback(host)) {
      const err = refused(host, port);
      process.nextTick(() => this.destroy(err));
      return this;
    }
    return realConnect.apply(this, args);
  };
  const realLookup = dns.lookup;
  dns.lookup = function (hostname, options, callback) {
    const cb = typeof options === 'function' ? options : callback;
    if (!loopback(hostname)) {
      const err = refused(hostname, 'dns');
      if (typeof cb === 'function') { process.nextTick(() => cb(err)); return {}; }
    }
    return realLookup.apply(this, arguments);
  };
  if (dns.promises && dns.promises.lookup) {
    const realP = dns.promises.lookup;
    dns.promises.lookup = function (hostname, ...rest) {
      if (!loopback(hostname)) return Promise.reject(refused(hostname, 'dns'));
      return realP.call(this, hostname, ...rest);
    };
  }
  Object.defineProperty(net.Socket.prototype, MARK, { value: true });
  say(`net_guard: loaded pid=${process.pid}`);
}

async function selfTest() {
  const { spawnSync } = require('node:child_process');
  const os = require('node:os');
  const path = require('node:path');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'net-guard-'));
  const log = path.join(dir, 'guard.log');
  const code = `
    const net = require('node:net');
    const srv = net.createServer(s => s.end()).listen(0, '127.0.0.1', async () => {
      const port = srv.address().port;
      const ok = await new Promise(r => net.connect(port, '127.0.0.1').on('connect', () => r(true)).on('error', () => r(false)));
      const out = await new Promise(r => net.connect(80, '192.0.2.1').on('connect', () => r('OUT')).on('error', e => r(e.code)));
      let f = 'none';
      try { await fetch('http://198.51.100.7/'); f = 'OUT'; } catch (e) { f = String(e.cause && e.cause.code || e.code || e.message); }
      const { Worker } = require('node:worker_threads');
      const w = await new Promise(r => new Worker("require('node:net').connect(443, '203.0.113.9').on('error', e => require('node:worker_threads').parentPort.postMessage(e.code)).on('connect', () => require('node:worker_threads').parentPort.postMessage('OUT'))", { eval: true }).on('message', r));
      console.log(JSON.stringify({ ok, out, f, w }));
      srv.close();
    });`;
  const r = spawnSync(process.execPath, ['-e', code], {
    env: { ...process.env, QA_NET_GUARD_LOG: log, NODE_OPTIONS: `--require "${__filename.replace(/\\/g, '/')}"` },   // NODE_OPTIONS reads \ as an escape
    encoding: 'utf8', timeout: 60000,
  });
  let res = {};
  try { res = JSON.parse(r.stdout.trim().split('\n').pop()); } catch (e) { /* reported below */ }
  const text = fs.existsSync(log) ? fs.readFileSync(log, 'utf8') : '';
  fs.rmSync(dir, { recursive: true, force: true });
  const checks = [
    ['loopback connects', res.ok === true, r.stderr],
    ['a non-loopback socket is refused', res.out === 'ECONNREFUSED', String(res.out)],
    ['fetch to a non-loopback host is refused', res.f === 'ECONNREFUSED', String(res.f)],
    ['a worker thread is guarded too', res.w === 'ECONNREFUSED', String(res.w)],
    ['the log has the loaded line', /net_guard: loaded pid=\d+/.test(text), text],
    ['the log names refused hosts', text.includes('192.0.2.1:80'), text],
  ];
  let failed = 0;
  for (const [name, ok, extra] of checks) {
    if (!ok) failed++;
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${ok ? '' : `  [${String(extra).trim().slice(-300)}]`}`);
  }
  console.log(failed ? `${failed} failed` : 'ALL PASS');
  process.exitCode = failed ? 1 : 0;
}

if (require.main === module && process.argv.includes('--self-test')) selfTest();
