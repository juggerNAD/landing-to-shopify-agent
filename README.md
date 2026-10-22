# Landing Page → Shopify Section Agent

A Claude Agent SDK script that takes a product landing page URL and rebuilds its first three sections as **one Liquid section** in a Shopify theme. It is tuned for mobile (390 px), and every text and image is editable in the theme editor.

```
python agent.py https://try-nomore.com/products/nomorecolds-red-light-therapy-colds-sinusitis
```

## How it works

The agent runs a capture → build → verify loop with six custom tools. They are served from an in-process MCP server, and all built-in Claude Code tools are disabled.

| Tool | What it does |
|---|---|
| `capture_page` | Playwright opens the page as an iPhone 13. It returns viewport-sized screenshots plus a JSON inventory of visible text (computed font, size, weight, color, spacing), images, SVGs and section boundaries. |
| `upload_images` | Uploads the source CDN images into the store's **Files** via `fileCreate` and returns `shopify://shop_images/...` refs. |
| `write_section` | Validates, then upserts `sections/<name>.liquid` via `themeFilesUpsert`. |
| `write_template` | Validates, then upserts `templates/product.<suffix>.json`, which holds all the content. |
| `create_product` | Creates or updates a placeholder product on that template and publishes it to the Online Store. |
| `screenshot_rebuild` | Screenshots the dev-store page at the same mobile size (handles the storefront password). The agent compares frame by frame and fixes differences, up to 3 passes. |

### Design decisions

**Content lives in the template, not the code.** The `.liquid` file contains only markup, schema and scoped CSS. All copy and image values sit in `product.<suffix>.json`, which is exactly what the theme editor reads and writes.

**Images are real editor images.** An `image_picker` setting can't point at an external URL. So images are first copied into Shopify Files, and the template references them as `shopify://shop_images/<file>`. In the editor they show up as normal, swappable images.

**Repeating items are blocks.** Bullets, badges, reviews and icons can be added, removed and reordered in the editor.

**A deterministic guard backs the "no hardcoded content" rule.** `tools/validate.py` strips the schema, styles, Liquid tags and HTML before upload. If any visible text is left, the file is rejected and the agent has to move it into settings. The validator also catches common schema errors that Shopify would reject: duplicate ids, defaults on `image_picker`, bad `range` steps, unknown template setting ids, and raw image URLs.

**Upload is verified, not assumed.** After `themeFilesUpsert`, the script compares the stored file's MD5 with what it sent. This catches cases where Shopify silently kept the old version.

**The buy box works.** Price comes from `product.price`, and add-to-cart is a real `{% form 'product' %}`. Labels and supporting lines are settings.

**CSS is scoped.** Styles are scoped to `#shopify-section-{{ section.id }}`, so the section can't break the rest of the theme (works on Horizon or Dawn).

## Setup

1. Create a Shopify Partners account and a **development store**. Use the default **Horizon** theme (Dawn also works).
2. Create an app for the store with these Admin API scopes: `write_themes`, `read_themes`, `write_files`, `read_files`, `write_products`, `read_products`, `write_publications`, `read_publications`.
   * Put an `shpat_` Admin token in `SHOPIFY_ADMIN_TOKEN`, **or**
   * for a Dev Dashboard app, put the client ID and secret in `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET` (client-credentials grant).
3. Install dependencies:
   ```bash
   python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   playwright install chromium
   ```
   The Agent SDK drives the Claude Code CLI. If the SDK reports that the CLI isn't found, run `npm install -g @anthropic-ai/claude-code`.
4. Copy `.env.example` to `.env` and fill it in, including `SHOPIFY_STOREFRONT_PASSWORD` (Online Store → Preferences).

## Run

```bash
python agent.py <url> [--max-turns 60]
```

The script streams every tool call and its result to the terminal. At the end it prints the storefront URL and the **API cost** (`total_cost_usd` from the SDK). It also writes:

```
output/
  original/        original page frames + inventory.json
  rebuild/         dev-store frames for each compare pass
  theme/           the exact section + template files that were uploaded
  images.json      source URL -> Shopify file ref map
  product.json     product id, storefront URL, theme editor URL
  run_summary.json duration, turns, compare passes, API cost
```

## Checking editability

Open the `editor_url` from `output/product.json`. The rebuilt section appears as a single section with all its settings and blocks. Try changing a headline or swapping an image, then save.

## Notes / limits

* Fonts: the original's custom web fonts are matched with the closest font stack, or with a `font_picker` setting.
* Videos are represented by a poster image. Shopify `video` settings need a Shopify-hosted file.
* If `themeFilesUpsert` is denied for your app type, push the files in `output/theme/` with `shopify theme push --only sections/... templates/...`.
* The dev store stays password protected. The rebuild is a private test and is not published.
