# Repro for F1 (resilience-prober). Needs: pip install playwright; python -m playwright install chromium;
# and the app running at SITE. The fault is injected in the browser; the server is never sent anything broken.
from playwright.sync_api import sync_playwright

SITE = 'http://127.0.0.1:8147/'

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.route('**/api/address', lambda route: route.fulfill(status=500, body='{"error": "Internal Server Error"}', headers={}, content_type='application/json') if route.request.method == 'POST' and True else route.fallback())
    page.goto(SITE)
    page.evaluate('location.hash = "#address"')
    page.fill('#street', '1 Test Lane')   # synthetic
    page.click('#save')
    page.wait_for_timeout(10000)
    page.screenshot(path='F1.png')
    browser.close()
