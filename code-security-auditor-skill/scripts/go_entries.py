#!/usr/bin/env python3
"""Go entry points for recon.py: route registrations, CLI commands and flags, listeners and config files.

    python go_entries.py <file.go> [...]     # prints the entries found in each file

Line and regex level, with the file's imports as the gate:

- HTTP routes count only in a file that imports a router (net/http, chi, gin, echo, gorilla/mux,
  fiber), only for that router's registration methods, and only when the first argument is a path
  ("/x", or Go 1.22's "GET /x") followed by a handler argument. So an interpreter's
  rt.Get("output"), an options object's o.Get("user-data-dir") and an HTTP client's
  client.Get("http://...") are not routes. Group prefixes (r.Group("/api")) are joined in.
- CLI commands: urfave/cli v2 and v3 command tables (cli.App, cli.Command, []*cli.Command{ {Name:
  ...} }) and cobra.Command literals (the one whose Execute() is called is the root). Flags come
  from urfave flag literals (with the environment variables bound through EnvVars or
  cli.EnvVars(...)), cobra Flags()/PersistentFlags() and the standard flag package.
- Listeners: net.Listen / ListenPacket / tls.Listen and http.ListenAndServe(TLS), with the bind
  read from the address: unix (a socket file), loopback, all (":8080", "0.0.0.0:...") or unknown.
- Config input: a file that reads a file from disk and decodes YAML, JSON, TOML or a viper config.

Everything is a first pass: the agent confirms each entry by reading the code.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

IMPORT_BLOCK = re.compile(r'^import\s*\((.*?)^\)', re.M | re.S)
IMPORT_ONE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
# import path prefix -> (framework, methods that register a route with (path, handler, ...))
ROUTERS = [
    ('net/http', 'net/http', {'HandleFunc', 'Handle'}),
    ('github.com/go-chi/chi', 'chi', {'Get', 'Post', 'Put', 'Patch', 'Delete', 'Head', 'Options', 'Connect', 'Trace',
                                      'HandleFunc', 'Handle', 'Mount'}),
    ('github.com/gin-gonic/gin', 'gin', {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'Any'}),
    ('github.com/labstack/echo', 'echo', {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'Any', 'CONNECT', 'TRACE'}),
    ('github.com/gorilla/mux', 'gorilla/mux', {'HandleFunc', 'Handle'}),
    ('github.com/gofiber/fiber', 'fiber', {'Get', 'Post', 'Put', 'Patch', 'Delete', 'Head', 'Options', 'All'}),
]
ROUTE = re.compile(r'\b([A-Za-z_][\w.]*)\.([A-Za-z]\w*)\(\s*"((?:[A-Z]+\s+)?/[^"]*)"\s*,')
GROUP = re.compile(r'\b(\w+)\s*:?=\s*([A-Za-z_][\w.]*)\.(?:Group|Route|PathPrefix)\(\s*"(/[^"]*)"')
LISTEN = re.compile(r'\b(net\.Listen(?:Packet|TCP|Unix)?|tls\.Listen|http\.ListenAndServe(?:TLS)?|(?:[A-Za-z_]\w*)\.ListenAndServe(?:TLS)?)\s*\(([^\n]*)')
CONFIG_DECODE = re.compile(r'\b(?:yaml|json|toml|hcl|json5)\.(?:Unmarshal|NewDecoder)\s*\(|\btoml\.Decode(?:File)?\s*\(|'
                           r'\bviper\.(?:ReadInConfig|ReadConfig|MergeInConfig)\s*\(')
FILE_READ = re.compile(r'\bos\.(?:ReadFile|Open|OpenFile)\s*\(|\bioutil\.ReadFile\s*\(|\bviper\.(?:SetConfigFile|AddConfigPath|ReadInConfig)\s*\(')
NOT_A_FILE = re.compile(r'\.Body\b|\b(?:resp|res|response|body|conn|stdout|output)\b|\.Bytes\(\)|\.Output\(\)|\.CombinedOutput\(\)')
STD_FLAG = re.compile(r'\bflag\.(?:String|Int|Int64|Uint|Uint64|Bool|Duration|Float64|Func|Var|TextVar|StringVar|IntVar|Int64Var|'
                      r'UintVar|BoolVar|DurationVar|Float64Var)\s*\(\s*(?:&?[\w.]+\s*,\s*)?"([\w.-]+)"')
COBRA_FLAG = re.compile(r'\.(?:Persistent)?Flags\(\)\.\w+\(\s*(?:&[\w.\[\]]+\s*,\s*)?"([\w.-]+)"')
LOOPBACK = re.compile(r'^(?:127\.\d+\.\d+\.\d+|localhost|\[::1\]|::1)(?::|$)')
ANY = re.compile(r'^(?::|0\.0\.0\.0(?::|$)|\[::\](?::|$)|::(?::|$)|$)')

def imports(text: str) -> list[str]:
    out = [m.group(1) for m in IMPORT_ONE.finditer(text)]
    for blk in IMPORT_BLOCK.finditer(text):
        out += re.findall(r'"([^"]+)"', blk.group(1))
    return out

def blank(text: str) -> str:
    """The text with string, rune and comment contents replaced by spaces (same length, same newlines)."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith('//', i):
            j = text.find('\n', i)
            j = n if j < 0 else j
            out[i:j] = ' ' * (j - i)
            i = j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [ch if ch == '\n' else ' ' for ch in text[i:j]]
            i = j
        elif c in '"\'`':
            j = i + 1
            while j < n and text[j] != c and not (c != '`' and text[j] == '\n'):
                j += 2 if text[j] == '\\' and c != '`' else 1
            out[i + 1:j] = [ch if ch == '\n' else ' ' for ch in text[i + 1:j]]
            i = j + 1
        else:
            i += 1
    return ''.join(out)

def line_of(text: str, offset: int) -> int:
    return text.count('\n', 0, offset) + 1

def literals(text: str, bl: str) -> list[dict]:
    """Composite literals that matter for CLIs: [{kind, start, end, parent}] (offsets of the braces)."""
    stack: list[dict] = []
    out = []
    line_start = 0
    for i, ch in enumerate(bl):
        if ch == '\n':
            line_start = i + 1
        elif ch == '{':
            before = bl[line_start:i]
            parent = stack[-1]['kind'] if stack else None
            if re.search(r'cli\.App\s*$', before):
                kind = 'app'
            elif re.search(r'\[\]\s*\*?\s*cli\.Command\s*$', before):
                kind = 'command_list'
            elif re.search(r'cli\.Command\s*$', before):
                kind = 'command'
            elif re.search(r'cli\.\w*Flag\s*$', before):
                kind = 'flag'
            elif re.search(r'cobra\.Command\s*$', before):
                kind = 'cobra'
            elif parent == 'command_list' and re.fullmatch(r'[\s,]*(?:&\s*)?', before):
                kind = 'command'
            else:
                kind = 'other'
            stack.append({'kind': kind, 'start': i, 'parent': parent, 'before': before})
        elif ch == '}' and stack:
            node = stack.pop()
            node['end'] = i
            out.append(node)
    return sorted(out, key=lambda n: n['start'])

def field(text: str, bl: str, node: dict, name: str) -> tuple[str, int] | None:
    """A string field (Name: "x") directly inside the literal (not in a nested one): (value, offset)."""
    for m in re.finditer(rf'\b{name}\s*:\s*"', bl[node['start']:node['end']]):
        at = node['start'] + m.start()
        if bl[node['start'] + 1:at].count('{') - bl[node['start'] + 1:at].count('}') == 0:
            vm = re.match(r'"((?:[^"\\]|\\.)*)"', text[node['start'] + m.end() - 1:node['end']])
            if vm:
                return vm.group(1), at
    return None

def addr_bind(expr: str, text: str) -> tuple[str, str | None]:
    """(bind, address) for a listen address expression: a literal, a constant, a Sprintf or a variable."""
    expr = expr.strip()
    m = re.match(r'"([^"]*)"', expr)
    if not m:
        m2 = re.match(r'fmt\.Sprintf\(\s*"([^"]*)"', expr)
        if m2:
            m = m2
        elif re.fullmatch(r'[A-Za-z_]\w*', expr):
            d = re.search(rf'\b{re.escape(expr)}\s*(?:[\w.]+\s*)?:?=\s*"([^"]*)"', text)
            if not d:
                return 'unknown', None
            m = d
        else:
            return 'unknown', None
    addr = m.group(1)
    if LOOPBACK.match(addr):
        return 'loopback', addr
    if ANY.match(addr):
        return 'all', addr
    return 'unknown', addr

def split_args(s: str) -> list[str]:
    out, depth, cur, q = [], 0, '', None
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in '"`':
            q = ch
        elif ch in '([{':
            depth += 1
        elif ch in ')]}':
            if depth == 0:
                out.append(cur)
                return [a.strip() for a in out]
            depth -= 1
        elif ch == ',' and depth == 0:
            out.append(cur)
            cur = ''
            continue
        cur += ch
    out.append(cur)
    return [a.strip() for a in out]

def entries(rel: str, text: str) -> tuple[list[dict], list[dict]]:
    """(entry points, CLI flags) for one Go file."""
    imps = imports(text)
    bl = blank(text)
    lines, bl_lines = text.splitlines(), bl.splitlines()
    out: list[dict] = []
    flags: list[dict] = []
    local = 'local invocation: arguments, flags and the environment variables bound to flags come from the caller'
    # routes
    routers = [(fw, methods) for prefix, fw, methods in ROUTERS if any(i == prefix or i.startswith(prefix + '/') for i in imps)]
    if routers:
        prefixes: dict[str, str] = {}
        for m in GROUP.finditer(text):
            prefixes[m.group(1)] = prefixes.get(m.group(2), '') + m.group(3).rstrip('/')
        for i, line in enumerate(lines):
            if i < len(bl_lines) and not bl_lines[i].strip():
                continue  # a comment line
            for m in ROUTE.finditer(line):
                recv, meth, path = m.group(1), m.group(2), m.group(3)
                fw = next((f for f, ms in routers if meth in ms), None)
                if not fw or (recv == 'http' and fw != 'net/http'):
                    continue
                method = 'ANY'
                pm = re.match(r'([A-Z]+)\s+(/.*)', path)
                if pm:
                    method, path = pm.group(1), pm.group(2)
                elif meth.upper() in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'CONNECT', 'TRACE'):
                    method = meth.upper()
                mm = re.search(r'\.Methods\(\s*"([A-Z]+)"', line)
                if mm:
                    method = mm.group(1)
                full = prefixes.get(recv.split('.')[0], '') + path if recv.split('.')[0] in prefixes else path
                out.append({'kind': 'webhook' if 'webhook' in full.lower() else 'http_route', 'framework': fw, 'method': method,
                            'path': full or '/', 'handler': f'{rel}:{i + 1}', 'function': None, 'auth': 'unknown',
                            'auth_evidence': 'file-level auth middleware may apply' if re.search(r'(?i)\.Use\([^)]*(auth|session|jwt|token)', text) else None})
    # CLI commands and flags
    urfave = any(re.match(r'github\.com/urfave/cli(/v\d+)?$', i) for i in imps)
    cobra = 'github.com/spf13/cobra' in imps
    if urfave or cobra:
        nodes = literals(text, bl)
        executed = set(re.findall(r'\b(\w+)\.Execute(?:C|Context)?\s*\(', text))
        run_called = set(re.findall(r'\b(\w+)\.Run\s*\(', text))
        for n in nodes:
            if n['kind'] in ('app', 'command') and urfave:
                f = field(text, bl, n, 'Name')
                if not f:
                    continue
                var = re.search(r'(\w+)\s*:?=\s*&?\s*cli\.Command\s*$', n['before'])
                root = n['kind'] == 'app' or (n['parent'] is None and (bool(var and var.group(1) in run_called)
                                                                       or bool(re.search(r'\bCommands\s*:', bl[n['start']:n['end']]))))
                out.append({'kind': 'cli', 'framework': 'urfave_cli', 'method': 'ROOT' if root else 'COMMAND', 'path': f[0],
                            'handler': f'{rel}:{line_of(text, f[1])}', 'function': None, 'auth': 'unknown', 'auth_evidence': local})
            elif n['kind'] == 'cobra' and cobra:
                f = field(text, bl, n, 'Use')
                if not f:
                    continue
                var = re.search(r'(\w+)\s*:?=\s*&?\s*cobra\.Command\s*$', n['before'])
                root = bool(var and var.group(1) in executed)
                out.append({'kind': 'cli', 'framework': 'cobra', 'method': 'ROOT' if root else 'COMMAND', 'path': f[0].split()[0] if f[0].split() else f[0],
                            'handler': f'{rel}:{line_of(text, f[1])}', 'function': None, 'auth': 'unknown', 'auth_evidence': local})
            elif n['kind'] == 'flag' and urfave:
                f = field(text, bl, n, 'Name')
                if not f:
                    continue
                body = text[n['start']:n['end'] + 1]
                env = []
                em = re.search(r'EnvVars\s*:\s*\[\]string\s*\{([^}]*)\}', body)
                if em:
                    env = re.findall(r'"([^"]+)"', em.group(1))
                env += re.findall(r'cli\.EnvVar\s*\(\s*"([^"]+)"', body)
                for sm in re.finditer(r'cli\.EnvVars\s*\(([^)]*)\)', body):
                    env += re.findall(r'"([^"]+)"', sm.group(1))
                flags.append({'file': rel, 'line': line_of(text, f[1]), 'name': f[0], 'env': env, 'framework': 'urfave_cli'})
    for i, line in enumerate(lines):
        if i < len(bl_lines) and not bl_lines[i].strip():
            continue
        for rx, fw in ((COBRA_FLAG, 'cobra'), (STD_FLAG, 'flag')):
            for m in rx.finditer(line):
                if fw == 'cobra' and not cobra:
                    continue
                flags.append({'file': rel, 'line': i + 1, 'name': m.group(1), 'env': [], 'framework': fw})
    # listeners
    for i, line in enumerate(lines):
        if i >= len(bl_lines) or not bl_lines[i].strip() or re.match(r'\s*func\b', line):
            continue
        m = LISTEN.search(line)
        if not m:
            continue
        call, args = m.group(1), split_args(m.group(2))
        sock = None
        if call.startswith(('net.Listen', 'tls.Listen')):
            network = args[0].strip('"') if args else ''
            if call == 'net.ListenUnix' or network.startswith('unix'):
                bind = 'unix'
                b, sock = addr_bind(args[1] if len(args) > 1 else '', text)
            else:
                bind, _ = addr_bind(args[1] if len(args) > 1 else '', text)
        elif call.startswith('http.ListenAndServe'):
            bind, _ = addr_bind(args[0] if args else '', text)
        else:  # srv.ListenAndServe(): the Addr field of an http.Server literal in the file
            am = re.search(r'\bAddr\s*:\s*([^,\n}]+)', text)
            bind = addr_bind(am.group(1), text)[0] if am else 'unknown'
        why = {'unix': 'unix socket' + (f' {sock}' if sock else '') + ': any local process whose user can open the socket file can '
                       'connect; a fixed path in /tmp can be pre-created or replaced by another user',
               'loopback': 'binds a loopback address: reachable from this machine only (check Host and Origin for DNS rebinding)',
               'all': 'binds every interface: reachable from the network',
               'unknown': 'bind address not read (a variable or a config value); read the code'}[bind]
        label = f'unix socket {sock}' if bind == 'unix' and sock else bind
        out.append({'kind': 'listener', 'framework': 'go', 'method': 'LISTEN', 'path': f'{rel} ({label})', 'handler': f'{rel}:{i + 1}',
                    'function': None, 'auth': 'unknown', 'auth_evidence': why, 'bind': bind})
    # config files read from disk
    if FILE_READ.search(text):
        reads = [i for i, line in enumerate(lines) if FILE_READ.search(line) and i < len(bl_lines) and bl_lines[i].strip()]
        for i, line in enumerate(lines):
            # a decode of what was just read from disk, not of an HTTP body or a process's output
            if i < len(bl_lines) and bl_lines[i].strip() and CONFIG_DECODE.search(line) and not NOT_A_FILE.search(line) \
                    and any(0 <= i - r <= 15 for r in reads):
                out.append({'kind': 'config_input', 'framework': 'go', 'method': 'CONFIG', 'path': rel, 'handler': f'{rel}:{i + 1}',
                            'function': None, 'auth': 'unknown', 'auth_evidence': 'reads and decodes a file from disk (a config, flow '
                            'or spec file): whoever writes that file (a cloned repository, a pull request, a shared workspace) '
                            'controls every field it decodes'})
                break
    return out, flags

def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    for f in sys.argv[1:]:
        p = Path(f)
        eps, flags = entries(p.as_posix(), p.read_text(encoding='utf-8', errors='replace'))
        for e in eps:
            print(f"{e['handler']}\t{e['kind']}\t{e['method']}\t{e['path']}" + (f"\tbind={e['bind']}" if e.get('bind') else ''))
        for fl in flags:
            print(f"{fl['file']}:{fl['line']}\tflag\t{fl['name']}\t{','.join(fl['env'])}")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
