#!/usr/bin/env python3
"""Repro spec: C-1: theme saved by 0.2.0 crashes every launch

Replays the crash trigger with Playwright. Exits 1 while the crash reproduces and 0 once it is fixed.
    python repro_C-1.py [--base-url http://localhost:5173]
"""
import json, sys, time

SCENARIO = json.loads(r"""{
  "base_url": "http://localhost:4173",
  "browser": "chromium",
  "viewport": {
    "width": 1280,
    "height": 800
  },
  "locale": "en-AU",
  "timezone": "Australia/Sydney",
  "observe": {
    "root_selector": "#root",
    "fallback_text": [
      "Something went wrong",
      "Not available right now."
    ]
  },
  "settle_ms": 800,
  "name": "C-1: theme saved by 0.2.0 crashes every launch",
  "routes": [
    {
      "url": "**/api/recipes",
      "status": 200,
      "json": [
        {
          "id": "r-1",
          "title": "Lemon risotto",
          "author": {
            "id": "u-1",
            "displayName": "Mia Chen"
          },
          "rating": 4.6,
          "ratingCount": 31,
          "tags": [
            "dinner"
          ]
        },
        {
          "id": "r-2",
          "title": "Charred corn salad",
          "author": {
            "id": "u-2",
            "displayName": "Sam Okafor"
          },
          "rating": 4.1,
          "ratingCount": 12,
          "tags": [
            "salad"
          ]
        }
      ]
    }
  ],
  "steps": [
    {
      "goto": "/#/"
    },
    {
      "reload": true
    }
  ],
  "storage": {
    "localStorage": {
      "pantry.prefs": "dark"
    }
  }
}""")

def seed_script(storage: dict) -> str:
    """Init script that seeds storage on the first load only, so reloads see what the app wrote."""
    data = json.dumps(storage or {})
    return ("(() => { try { if (sessionStorage.getItem('__probe_seeded')) return;"
            f" const s = {data};"
            " for (const [k, v] of Object.entries(s.localStorage || {})) localStorage.setItem(k, v);"
            " for (const [k, v] of Object.entries(s.sessionStorage || {})) sessionStorage.setItem(k, v);"
            " sessionStorage.setItem('__probe_seeded', '1'); } catch (e) {} })();")


def apply_routes(page, routes: list) -> None:
    def make(r):
        def handler(route):
            if r.get('delay_ms'):
                time.sleep(r['delay_ms'] / 1000)
            if r.get('abort'):
                return route.abort(r['abort'])
            body = json.dumps(r['json']) if 'json' in r else r.get('body', '')
            headers = {'content-type': 'application/json' if 'json' in r else r.get('content_type', 'text/plain')}
            route.fulfill(status=r.get('status', 200), body=body, headers=headers)
        return handler
    for r in routes or []:
        page.route(r['url'], make(r))


def run_steps(page, context, steps: list, base_url: str, log: list) -> None:
    for n, step in enumerate(steps or [], 1):
        (action, arg), = step.items()
        log.append({'step': n, 'action': action, 'arg': arg, 'at': round(time.time(), 3)})
        if action == 'goto':
            page.goto(arg if '://' in arg else base_url.rstrip('/') + '/' + arg.lstrip('/'), wait_until='load')
        elif action == 'click':
            page.click(arg, timeout=5000)
        elif action == 'fill':
            page.fill(arg[0], arg[1], timeout=5000)
        elif action == 'press':
            page.keyboard.press(arg)
        elif action == 'reload':
            page.reload(wait_until='load')
        elif action == 'back':
            page.go_back(wait_until='load')
        elif action == 'offline':
            context.set_offline(bool(arg))
        elif action == 'wait_for':
            page.wait_for_selector(arg, timeout=5000)
        elif action == 'wait_ms':
            page.wait_for_timeout(int(arg))
        else:
            raise ValueError(f'unknown step action: {action}')


def observe(page, obs: dict) -> dict:
    root = obs.get('root_selector', 'body')
    js = """([root, texts]) => {
        const el = document.querySelector(root);
        const text = (document.body && document.body.innerText) || '';
        return {root_found: !!el, root_empty: !!el && el.children.length === 0 && !el.textContent.trim(),
                fallback_visible: texts.filter(t => text.includes(t)), body_text: text.slice(0, 400)};
    }"""
    return page.evaluate(js, [root, obs.get('fallback_text') or []])


def main():
    from playwright.sync_api import sync_playwright
    base_url = sys.argv[sys.argv.index('--base-url') + 1] if '--base-url' in sys.argv else SCENARIO['base_url']
    errors = []
    with sync_playwright() as pw:
        browser = getattr(pw, SCENARIO.get('browser', 'chromium')).launch()
        context = browser.new_context(viewport=SCENARIO.get('viewport') or {'width': 1280, 'height': 800},
                                      locale=SCENARIO.get('locale'), timezone_id=SCENARIO.get('timezone'))
        context.add_init_script(seed_script(SCENARIO.get('storage')))
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(e.message))
        page.on('console', lambda m: errors.append(m.text.splitlines()[0]) if m.type == 'error' else None)
        apply_routes(page, SCENARIO.get('routes'))
        run_steps(page, context, SCENARIO.get('steps'), base_url, [])
        page.wait_for_timeout(int(SCENARIO.get('settle_ms', 800)))
        state = observe(page, SCENARIO.get('observe') or {})
        browser.close()
    crashed = bool(errors) and (state['root_empty'] or bool(state['fallback_visible']))
    print(('REPRODUCED: ' + '; '.join(errors)) if crashed else 'Not reproduced: no uncaught error replaced the screen.')
    return 1 if crashed else 0

if __name__ == '__main__':
    raise SystemExit(main())
