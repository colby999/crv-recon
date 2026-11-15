"""Headless render helper (Playwright) for JS-only directory pages.

Used sparingly: only adapters without a server-side data path come here.
"""
from . import UA, TIMEOUT


def render_html(url, scrolls=4, wait=6):
    """Render a page headless and return final HTML. Raises on failure."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox'])
        try:
            pg = b.new_page(user_agent=UA['User-Agent'])
            pg.goto(url, timeout=45000, wait_until='domcontentloaded')
            pg.wait_for_timeout(wait * 1000)
            for _ in range(scrolls):
                pg.mouse.wheel(0, 4000)
                pg.wait_for_timeout(1500)
            return pg.content()
        finally:
            b.close()
