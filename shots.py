"""
Take phone-style screenshots of web pages (e.g. SEC filings) for TikTok slides.

Run from GitHub Actions -> "Screenshots" -> Run workflow, paste one or more URLs
separated by spaces. PNGs are saved to the shots/ folder of the repo.
"""

import os
import re

import requests
from playwright.sync_api import sync_playwright

MAX_HEIGHT_CSS = int(os.environ.get("MAX_HEIGHT", "3000"))   # only the top of long documents


def name_for(i, url):
    slug = re.sub(r"[^A-Za-z0-9]+", "_", url.split("//", 1)[-1]).strip("_")
    return f"shots/{i:02d}_{slug[:80]}.png"


def main():
    urls = os.environ.get("URLS", "").split()
    if not urls:
        raise SystemExit("No URLS given.")
    ua = os.environ.get("SEC_USER_AGENT") or "InsiderRadar contact@example.com"
    os.makedirs("shots", exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3,
                                  is_mobile=True, has_touch=True, user_agent=ua)
        for i, url in enumerate(urls):
            page = ctx.new_page()
            if "sec.gov" in url:
                # SEC blocks headless browsers, so download the HTML like the bot does
                # and render that exact page (with a <base> tag so styles still load).
                html = requests.get(url, headers={"User-Agent": ua}, timeout=60).text
                base = f'<base href="{url}">'
                html = re.sub(r"(<head[^>]*>)", r"\1" + base, html, count=1, flags=re.I) \
                    if re.search(r"<head", html, re.I) else base + html
                page.route("**/*", lambda route: route.abort()
                           if "sec.gov" in route.request.url and route.request.resource_type in ("script", "xhr", "fetch")
                           else route.continue_())
                page.set_content(html, wait_until="load", timeout=90000)
            else:
                page.goto(url, wait_until="networkidle", timeout=90000)
            page.wait_for_timeout(1500)
            full = page.evaluate("Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)")
            width = page.evaluate("Math.max(document.documentElement.scrollWidth, window.innerWidth)")
            height = min(full, MAX_HEIGHT_CSS)
            path = name_for(i, url)
            page.screenshot(path=path, full_page=True, clip={"x": 0, "y": 0, "width": width, "height": height})
            print(f"saved {path} ({width}x{height} css px, page height {full})")
            page.close()
        browser.close()


if __name__ == "__main__":
    main()
