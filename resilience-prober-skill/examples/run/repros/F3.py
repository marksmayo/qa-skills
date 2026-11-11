# Repro for F3 (resilience-prober). Needs: pip install playwright; python -m playwright install chromium;
# and the app running at SITE. The fault is injected in the browser; the server is never sent anything broken.
from playwright.sync_api import sync_playwright

SITE = 'http://127.0.0.1:8147/'

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.route('**/api/status/*', lambda route: None)   # never answered
    page.goto(SITE)
    page.evaluate('location.hash = "#orders"')
    page.click('a.track')
    page.wait_for_timeout(10000)
    page.screenshot(path='F3.png')
    browser.close()
