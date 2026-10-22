"""Mobile page capture with Playwright.

Returns viewport-sized JPEG frames (so Claude sees them at full resolution
instead of one tall, downscaled image) plus a structured inventory of the
visible content: text nodes with computed styles, images, boxes, and the
Shopify section boundaries.
"""
from __future__ import annotations

import math
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

DEVICE = "iPhone 13"

# Removes cookie banners, newsletter popups and other overlays that would
# hide the content in screenshots.
CLEANUP_JS = r"""
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed') continue;
    const r = el.getBoundingClientRect();
    const area = (r.width * r.height) / (vw * vh);
    const z = parseInt(cs.zIndex) || 0;
    if (area > 0.35 || (z >= 1000 && area > 0.15)) el.remove();
  }
  document.querySelectorAll('iframe[src*="klaviyo"], [class*="klaviyo-form"], [id*="popup"]').forEach(e => e.remove());
  document.documentElement.style.overflow = 'auto';
  document.body.style.overflow = 'auto';
}
"""

EXTRACT_JS = r"""
(maxY) => {
  const sy = window.scrollY;
  const box = r => ({x: Math.round(r.left), y: Math.round(r.top + sy), w: Math.round(r.width), h: Math.round(r.height)});
  const visible = el => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.05;
  };
  const style = cs => ({
    font: cs.fontFamily.split(',')[0].replace(/["']/g, '').trim(),
    size: cs.fontSize, weight: cs.fontWeight, lh: cs.lineHeight, ls: cs.letterSpacing,
    color: cs.color, bg: cs.backgroundColor, align: cs.textAlign, transform: cs.textTransform,
    radius: cs.borderRadius, border: cs.borderWidth !== '0px' ? `${cs.borderWidth} ${cs.borderStyle} ${cs.borderColor}` : null,
    pad: cs.padding
  });
  const skip = ['SCRIPT','STYLE','NOSCRIPT','LINK','META','TEMPLATE','IFRAME'];
  const nodes = [];
  const fonts = new Set();
  for (const el of document.querySelectorAll('body *')) {
    if (nodes.length >= 300) break;
    if (skip.includes(el.tagName)) continue;
    if (el.closest('[data-cap-text]')) continue;           // already captured as part of a text block
    if (el.parentElement && el.parentElement.closest('svg')) continue;
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect(), b = box(r);
    if (b.y > maxY) continue;
    const cs = getComputedStyle(el);
    const item = {tag: el.tagName.toLowerCase(), box: b};
    const tag = el.tagName.toUpperCase();
    if (tag === 'IMG') {
      item.kind = 'img'; item.src = el.currentSrc || el.src; item.alt = el.alt || '';
      item.fit = cs.objectFit;
    } else if (tag === 'VIDEO') {
      item.kind = 'video'; item.src = el.currentSrc || (el.querySelector('source') || {}).src || ''; item.poster = el.poster || '';
    } else if (tag === 'SVG') {
      item.kind = 'svg'; item.color = cs.color; item.fill = cs.fill;
      item.svg = el.outerHTML.length < 900 ? el.outerHTML : el.outerHTML.slice(0, 900) + '...';
    } else {
      const direct = Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
      const bgImg = cs.backgroundImage && cs.backgroundImage.includes('url(');
      const hasBg = !['rgba(0, 0, 0, 0)', 'transparent'].includes(cs.backgroundColor);
      if (direct) {
        item.kind = 'text';
        item.text = (el.innerText || direct).replace(/\n{3,}/g, '\n\n').trim().slice(0, 1200);
        item.html = el.innerHTML.replace(/\s(class|style|data-[\w-]+|id)="[^"]*"/g, '').slice(0, 700);
        item.href = el.closest('a') ? el.closest('a').getAttribute('href') : null;
        el.setAttribute('data-cap-text', '1');
      } else if (bgImg) {
        item.kind = 'bgimg'; item.src = cs.backgroundImage;
      } else if ((hasBg || cs.borderWidth !== '0px') && r.width <= window.innerWidth * 1.01) {
        item.kind = 'box';
      } else continue;
    }
    item.style = style(cs);
    fonts.add(item.style.font);
    nodes.push(item);
  }
  nodes.sort((a, b) => a.box.y - b.box.y || a.box.x - b.box.x);
  const sections = Array.from(document.querySelectorAll('[id^="shopify-section-"]'))
    .filter(visible)
    .map(s => ({id: s.id, classes: s.className.toString().slice(0, 200), ...box(s.getBoundingClientRect())}))
    .filter(s => s.y <= maxY);
  return {
    title: document.title,
    viewport: {w: window.innerWidth, h: window.innerHeight},
    page_height: document.documentElement.scrollHeight,
    fonts: Array.from(fonts),
    body_bg: getComputedStyle(document.body).backgroundColor,
    sections, nodes
  };
}
"""


PASSWORD_INPUT = 'form[action*="/password"] input[type="password"], input[name="password"]'


async def _is_password_page(page) -> bool:
    return "/password" in page.url or await page.locator(PASSWORD_INPUT).count() > 0


async def _unlock_storefront(ctx, page, password: str | None, target: str) -> None:
    """Shopify dev stores sit behind a password page (served with or without a /password redirect)."""
    if not await _is_password_page(page):
        return
    if not password:
        raise RuntimeError("Storefront is password protected; set SHOPIFY_STOREFRONT_PASSWORD")
    p = urlparse(target)
    base = f"{p.scheme}://{p.netloc}"
    # 1) Submit the password form directly; the cookie lands in this browser context.
    try:
        await ctx.request.post(f"{base}/password", form={
            "form_type": "storefront_password", "utf8": "\u2713", "password": password})
    except Exception:
        pass
    await page.goto(target, wait_until="domcontentloaded", timeout=60000)
    if not await _is_password_page(page):
        return
    # 2) Fall back to the UI: themes hide the field behind an "Enter using password" button.
    for label in ("Enter using password", "Enter store using password", "Enter with password", "Password"):
        btn = page.get_by_role("button", name=label)
        if await btn.count():
            try:
                await btn.first.click(timeout=3000)
                break
            except Exception:
                pass
    field = page.locator(PASSWORD_INPUT).first
    await field.fill(password, timeout=10000)
    await field.press("Enter")
    await page.wait_for_load_state("domcontentloaded")
    await page.goto(target, wait_until="domcontentloaded", timeout=60000)
    if await _is_password_page(page):
        raise RuntimeError("Storefront password was not accepted - check SHOPIFY_STOREFRONT_PASSWORD "
                           "(Online Store > Preferences > Password protection)")


async def capture(url: str, *, max_y: int = 3400, password: str | None = None,
                  include_dom: bool = True, save_dir: Path | None = None,
                  prefix: str = "frame") -> tuple[dict | None, list[bytes]]:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        device = dict(p.devices[DEVICE])
        device.pop("default_browser_type", None)
        device["device_scale_factor"] = 1.5  # sharp enough to read, small enough to send
        vw, vh = device["viewport"]["width"], device["viewport"]["height"]
        ctx = await browser.new_context(**device)
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await _unlock_storefront(ctx, page, password, url)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        # Scroll through to trigger lazy-loaded images, then return to top.
        for y in range(0, max_y + 1200, 350):
            await page.evaluate(f"window.scrollTo(0, {y})")
            await page.wait_for_timeout(120)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1200)
        await page.evaluate(CLEANUP_JS)

        dom = await page.evaluate(EXTRACT_JS, max_y) if include_dom else None
        height = await page.evaluate("document.documentElement.scrollHeight")
        limit = min(max_y, height)

        frames: list[bytes] = []
        for i in range(math.ceil(limit / vh)):
            y = i * vh
            h = min(vh, height - y)
            if h <= 0:
                break
            frames.append(await page.screenshot(
                full_page=True, type="jpeg", quality=62,
                clip={"x": 0, "y": y, "width": vw, "height": h}))
        await browser.close()

    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(frames):
            (save_dir / f"{prefix}_{i:02d}.jpg").write_bytes(f)
    return dom, frames