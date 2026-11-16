// A network guard for Node test runs (Vitest, Jest, Mocha, node --test): refuses every connection that isn't
// loopback, and logs it. No dependencies. The Node counterpart of net_guard.py.
//
//   QA_NET_GUARD_LOG=<scratch>/net_guard.log NODE_OPTIONS="--require <abs path>/net_guard.cjs" npx vitest run
//   node net_guard.cjs --self-test
//
// On Windows write the path with forward slashes (C:/scratch/net_guard.cjs): NODE_OPTIONS reads a backslash
// inside quotes as an escape.
//
// It wraps net.Socket.prototype.connect (which http, https, tls, undici's fetch and database drivers all reach),
// dgram send and connect (UDP), dns.lookup / dns.promises.lookup and the c-ares resolvers (dns.resolve*,
// dns.reverse, dns.Resolver, dns.promises.Resolver), so anything other than 127.0.0.0/8, ::1 and localhost
// fails with ECONNREFUSED. NODE_OPTIONS reaches child processes and Vitest's and Jest's fork and thread pools.
// Not covered: named pipes and Unix sockets (a {path} is passed through; on Windows a pipe path can name
// another machine over SMB), native addons, and child processes that drop NODE_OPTIONS.
//
// The log proves the guard was on: every process writes `net_guard: loaded pid=<pid>` when the guard loads,
// and every refusal adds `net_guard: refused <host>:<port>`. Before claiming "zero refused connections",
// check the log holds a `loaded` line: a guard that never loaded (a wrong path, a shell that rewrote
// NODE_OPTIONS) refuses nothing and logs nothing.
//
// With QA_NET_GUARD_LOG set the lines go to the log only, never to stderr, so tests that assert a child
// process's stderr is empty (CLI tests inherit NODE_OPTIONS) are not broken by the guard. Without
// QA_NET_GUARD_LOG, or when the log cannot be written, they go to stderr. A refusal names the first stack
// frame outside Node's internals and node_modules (`from <file>:<line>`), so it can be tied to a test.
//
// Go, Rust, JVM and .NET test binaries can't load this guard: use net_guard_proxy.py (a refusing loopback
// proxy with the same `loaded`/`refused` log lines, plus an isolated HOME/USERPROFILE). It guards only clients
// that honour HTTP(S)_PROXY, so its limits must be stated with the result.
'use strict';
const net = require('node:net');
const dns = require('node:dns');
const fs = require('node:fs');

const MARK = Symbol.for('qa-skills.net_guard');
const LOG = process.env.QA_NET_GUARD_LOG;

function say(line) {
  if (LOG) {
    try { fs.appendFileSync(LOG, line + '\n'); return; } catch (e) { /* unwritable log: fall back to stderr */ }
  }
  try { process.stderr.write(line + '\n'); } catch (e) { /* closed stderr */ }
}

// The first stack frame outside Node's internals, this file and node_modules, as ` from <file>:<line>`.
function caller() {
  const frames = String(new Error().stack || '').split('\n').slice(1);
  for (const f of frames) {
    const m = /\(?((?:file:\/\/)?[^()\s]+?):(\d+):\d+\)?\s*$/.exec(f.trim());
    if (!m) continue;
    const file = m[1];
    if (file.startsWith('node:') || file.includes(__filename) || /[\\/]node_modules[\\/]/.test(file)) continue;
    return ` from ${file}:${m[2]}`;
  }
  return '';
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
  say(`net_guard: refused ${host}:${port}${caller()}`);
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
  // UDP (dgram): send(msg[, offset, length][, port][, address][, callback]) and connect(port[, address][, callback]).
  // A send with no port goes to the connected peer, which connect already checked; no address means localhost.
  const dgram = require('node:dgram');
  const realSend = dgram.Socket.prototype.send;
  dgram.Socket.prototype.send = function (...args) {
    const rest = args.slice(1);
    const cb = typeof rest[rest.length - 1] === 'function' ? rest[rest.length - 1] : null;
    const address = rest.find(a => typeof a === 'string');
    const nums = rest.filter(a => typeof a === 'number');
    const port = nums.length ? nums[nums.length - 1] : undefined;
    if (address !== undefined && !loopback(address)) {
      const err = refused(address, port);
      if (cb) { process.nextTick(() => cb(err)); return; }
      process.nextTick(() => this.emit('error', err));
      return;
    }
    return realSend.apply(this, args);
  };
  const realUdpConnect = dgram.Socket.prototype.connect;
  dgram.Socket.prototype.connect = function (port, address, cb) {
    if (typeof address === 'string' && !loopback(address)) {
      const err = refused(address, port);
      process.nextTick(() => (typeof cb === 'function' ? cb(err) : this.emit('error', err)));
      return;
    }
    return realUdpConnect.apply(this, arguments);
  };
  // DNS through c-ares: dns.resolve*, dns.reverse, dns.Resolver and dns.promises (lookup is wrapped above). These
  // send queries to the configured DNS servers for any name, so every name that is not loopback is refused.
  const RESOLVE = ['resolve', 'resolve4', 'resolve6', 'resolveAny', 'resolveCaa', 'resolveCname', 'resolveMx', 'resolveNaptr',
    'resolveNs', 'resolvePtr', 'resolveSoa', 'resolveSrv', 'resolveTlsa', 'resolveTxt', 'reverse'];
  const guardResolvers = (obj, promises) => {
    for (const name of RESOLVE) {
      const real = obj && obj[name];
      if (typeof real !== 'function') continue;
      obj[name] = function (hostname, ...rest) {
        if (!loopback(hostname)) {
          const err = refused(hostname, 'dns');
          if (promises) return Promise.reject(err);
          const cb = rest[rest.length - 1];
          if (typeof cb === 'function') { process.nextTick(() => cb(err)); return {}; }
          throw err;
        }
        return real.call(this, hostname, ...rest);
      };
    }
  };
  guardResolvers(dns, false);
  if (dns.Resolver) guardResolvers(dns.Resolver.prototype, false);
  if (dns.promises) {
    guardResolvers(dns.promises, true);
    if (dns.promises.Resolver) guardResolvers(dns.promises.Resolver.prototype, true);
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
      // UDP: dgram send and connect to a TEST-NET address must be refused
      const dgram = require('node:dgram');
      const u = dgram.createSocket('udp4');
      const us = await new Promise(r => u.send('x', 9, '192.0.2.1', e => r(e ? e.code : 'OUT')));
      u.close();
      const c = dgram.createSocket('udp4');
      const uc = await new Promise(r => { c.on('error', e => r(e.code)); c.connect(9, '198.51.100.1', e => r(e ? e.code : 'OUT')); });
      c.close();
      // DNS through c-ares (dns.resolve*, dns.Resolver, dns.promises): pointed at a loopback "DNS server" that counts
      // the queries it receives, so a lookup that skips the guard is seen without leaving the machine
      const dns = require('node:dns');
      const fake = dgram.createSocket('udp4');
      let queries = 0;
      fake.on('message', () => { queries++; });
      await new Promise(r => fake.bind(0, '127.0.0.1', r));
      const server = '127.0.0.1:' + fake.address().port;
      const res = new dns.Resolver({ timeout: 300, tries: 1 }); res.setServers([server]);
      const pres = new dns.promises.Resolver({ timeout: 300, tries: 1 }); pres.setServers([server]);
      dns.setServers([server]);
      const race = p => Promise.race([p, new Promise(r => setTimeout(() => r('SENT'), 1000))]);   // a query on its way
      const r1 = await race(new Promise(r => res.resolve4('cdn.example.com', e => r(e ? e.code : 'OUT'))));
      const r2 = await race(pres.resolveTxt('tiles.example.org').then(() => 'OUT', e => e.code));
      const r3 = await race(new Promise(r => dns.resolve('api.example.net', 'A', e => r(e ? e.code : 'OUT'))));
      const r4 = await race(dns.promises.resolveAny('cdn.example.com').then(() => 'OUT', e => e.code));
      const r5 = await race(new Promise(r => dns.reverse('192.0.2.5', e => r(e ? e.code : 'OUT'))));
      await new Promise(r => setTimeout(r, 200));
      console.log(JSON.stringify({ ok, out, f, w, us, uc, dnsr: [r1, r2, r3, r4, r5], queries }));
      process.exit(0);   // pending c-ares queries (an unguarded run) would otherwise keep the process alive
    });`;
  const r = spawnSync(process.execPath, ['-e', code], {
    env: { ...process.env, QA_NET_GUARD_LOG: log, NODE_OPTIONS: `--require "${__filename.replace(/\\/g, '/')}"` },   // NODE_OPTIONS reads \ as an escape
    encoding: 'utf8', timeout: 60000,
  });
  const bareEnv = { ...process.env, NODE_OPTIONS: `--require "${__filename.replace(/\\/g, '/')}"` };
  delete bareEnv.QA_NET_GUARD_LOG;
  const bare = spawnSync(process.execPath, ['-e', '1'], { env: bareEnv, encoding: 'utf8', timeout: 60000 });
  let res = {};
  try { res = JSON.parse(r.stdout.trim().split('\n').pop()); } catch (e) { /* reported below */ }
  const text = fs.existsSync(log) ? fs.readFileSync(log, 'utf8') : '';
  fs.rmSync(dir, { recursive: true, force: true });
  const checks = [
    ['loopback connects', res.ok === true, r.stderr],
    ['a non-loopback socket is refused', res.out === 'ECONNREFUSED', String(res.out)],
    ['fetch to a non-loopback host is refused', res.f === 'ECONNREFUSED', String(res.f)],
    ['a worker thread is guarded too', res.w === 'ECONNREFUSED', String(res.w)],
    ['dgram send to a TEST-NET address is refused', res.us === 'ECONNREFUSED', String(res.us)],
    ['dgram connect to a TEST-NET address is refused', res.uc === 'ECONNREFUSED', String(res.uc)],
    ['dns.resolve*, dns.Resolver, dns.promises and dns.reverse are refused before a query is sent',
     Array.isArray(res.dnsr) && res.dnsr.every(c => c === 'ECONNREFUSED') && res.queries === 0, JSON.stringify([res.dnsr, res.queries])],
    ['the log names refused UDP and DNS targets', text.includes('192.0.2.1:9') && text.includes('cdn.example.com:dns'), text],
    ['the log has the loaded line', /net_guard: loaded pid=\d+/.test(text), text],
    ['the log names refused hosts', text.includes('192.0.2.1:80'), text],
    ['a refusal names the calling script', /refused 192\.0\.2\.1:80 from \S+:\d+/.test(text), text],
    ['with a log set, stderr stays empty', !/net_guard:/.test(r.stderr || ''), r.stderr],
    ['without a log, the loaded line goes to stderr', /net_guard: loaded pid=\d+/.test(bare.stderr || ''), bare.stderr],
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
