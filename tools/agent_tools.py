"""Custom tools exposed to the agent through an in-process MCP server."""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool

from .browser import capture
from .shopify import Shopify
from .validate import validate_section, validate_template

OUT = Path("output")
SHOP = Shopify()
STATE: dict = {"schema": None, "section_type": None, "storefront_url": None, "captures": 0}


def _text(obj, limit: int = 60000) -> dict:
    t = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))
    return {"type": "text", "text": t[:limit]}


def _err(msg) -> dict:
    return {"content": [_text(msg)], "is_error": True}


def _frames(frames: list[bytes]) -> list[dict]:
    return [{"type": "image", "data": base64.b64encode(f).decode(), "mimeType": "image/jpeg"} for f in frames]


@tool("capture_page",
      "Open a URL in an iPhone-sized mobile browser. Returns viewport-sized screenshots (top to bottom) "
      "and a JSON inventory of visible text (with computed styles), images (with src), svgs, boxes and "
      "Shopify section boundaries, all with absolute page coordinates.",
      {"type": "object",
       "properties": {"url": {"type": "string"},
                      "max_y": {"type": "integer", "description": "Capture down to this page Y (px). Default 3400."}},
       "required": ["url"]})
async def capture_page(args):
    try:
        dom, frames = await capture(args["url"], max_y=int(args.get("max_y") or 3400),
                                    save_dir=OUT / "original", prefix="original")
    except Exception as e:
        return _err(f"capture failed: {e}")
    (OUT / "original" / "inventory.json").write_text(json.dumps(dom, indent=1), encoding="utf-8")
    note = f"{len(frames)} frames, each one mobile viewport tall ({dom['viewport']['w']}x{dom['viewport']['h']} CSS px), in order."
    return {"content": [_text(note), *_frames(frames), _text(dom)]}


@tool("upload_images",
      "Upload images to the store's Files by URL (their CDN links). Returns a map of source URL -> "
      "{ref: 'shopify://shop_images/<file>', cdn, w, h}. Use 'ref' as the value of image_picker settings in the template JSON.",
      {"type": "object",
       "properties": {"urls": {"type": "array", "items": {"type": "string"}}},
       "required": ["urls"]})
async def upload_images(args):
    try:
        res = await SHOP.upload_images(args["urls"])
    except Exception as e:
        return _err(f"upload failed: {e}")
    (OUT / "images.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return {"content": [_text(res)]}


@tool("write_section",
      "Validate and upload sections/<name>.liquid to the theme. Rejected if it contains hardcoded visible text, "
      "external image URLs, or schema problems; fix and call again.",
      {"type": "object",
       "properties": {"name": {"type": "string", "description": "file name without extension, e.g. nomore-landing"},
                      "liquid": {"type": "string"}},
       "required": ["name", "liquid"]})
async def write_section(args):
    name = re.sub(r"[^a-z0-9-_]", "-", args["name"].lower())
    errors, schema = validate_section(args["liquid"])
    if errors:
        return _err({"rejected_before_upload": errors})
    (OUT / "theme" / "sections").mkdir(parents=True, exist_ok=True)
    (OUT / "theme" / "sections" / f"{name}.liquid").write_text(args["liquid"], encoding="utf-8")
    try:
        res = await SHOP.upsert_theme_file(f"sections/{name}.liquid", args["liquid"])
    except Exception as e:
        return _err(f"upload failed: {e}")
    if not res["ok"]:
        return _err(res)
    STATE["schema"], STATE["section_type"] = schema, name
    return {"content": [_text(res)]}


@tool("write_template",
      "Validate and upload templates/product.<suffix>.json, which places the section on the product page and "
      "holds all the content (setting values, blocks, image refs). Call after write_section.",
      {"type": "object",
       "properties": {"suffix": {"type": "string", "description": "template suffix, e.g. nomore"},
                      "template_json": {"type": "string"}},
       "required": ["suffix", "template_json"]})
async def write_template(args):
    if not STATE["section_type"]:
        return _err("call write_section first")
    suffix = re.sub(r"[^a-z0-9-_]", "-", args["suffix"].lower())
    errors = validate_template(args["template_json"], STATE["section_type"], STATE["schema"])
    if errors:
        return _err({"rejected_before_upload": errors})
    (OUT / "theme" / "templates").mkdir(parents=True, exist_ok=True)
    (OUT / "theme" / "templates" / f"product.{suffix}.json").write_text(args["template_json"], encoding="utf-8")
    try:
        res = await SHOP.upsert_theme_file(f"templates/product.{suffix}.json", args["template_json"])
    except Exception as e:
        return _err(f"upload failed: {e}")
    return {"content": [_text(res)]} if res["ok"] else _err(res)


@tool("create_product",
      "Create (or update, if the handle exists) a placeholder product that uses the given template suffix, "
      "and publish it to the Online Store. Returns the storefront URL and theme editor URL.",
      {"type": "object",
       "properties": {"title": {"type": "string"}, "handle": {"type": "string"},
                      "template_suffix": {"type": "string"},
                      "price": {"type": "string", "description": "placeholder price, e.g. 49.99"},
                      "image_url": {"type": "string", "description": "optional product image source URL"}},
       "required": ["title", "handle", "template_suffix"]})
async def create_product(args):
    try:
        res = await SHOP.upsert_product(args["title"], args["handle"], args["template_suffix"],
                                        price=args.get("price") or "49.99", image_url=args.get("image_url"))
    except Exception as e:
        return _err(f"product failed: {e}")
    STATE["storefront_url"] = res["storefront_url"]
    (OUT / "product.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return {"content": [_text(res)]}


@tool("screenshot_rebuild",
      "Screenshot the rebuilt product page on the dev store at the same mobile size as capture_page "
      "(handles the storefront password), so you can compare it with the original frames.",
      {"type": "object",
       "properties": {"max_y": {"type": "integer"}},
       "required": []})
async def screenshot_rebuild(args):
    if not STATE["storefront_url"]:
        return _err("call create_product first")
    STATE["captures"] += 1
    try:
        _, frames = await capture(STATE["storefront_url"], max_y=int(args.get("max_y") or 3400),
                                  password=os.getenv("SHOPIFY_STOREFRONT_PASSWORD"), include_dom=False,
                                  save_dir=OUT / "rebuild", prefix=f"pass{STATE['captures']}")
    except Exception as e:
        return _err(f"capture failed: {e}")
    return {"content": [_text(f"Rebuild pass {STATE['captures']}: {len(frames)} frames."), *_frames(frames)]}


ALL_TOOLS = [capture_page, upload_images, write_section, write_template, create_product, screenshot_rebuild]
SERVER = create_sdk_mcp_server(name="shopify_rebuild", version="1.0.0", tools=ALL_TOOLS)
TOOL_NAMES = [f"mcp__shopify_rebuild__{t.name}" for t in ALL_TOOLS]