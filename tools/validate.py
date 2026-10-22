"""Checks run before anything is uploaded to Shopify.

The brief says every text and image must be editable in the theme editor,
so the section file is rejected if any visible text survives once the schema,
styles, Liquid tags and HTML tags are stripped out.
"""
from __future__ import annotations

import json
import re

ALLOWED_TYPES = {
    "text", "textarea", "richtext", "inline_richtext", "image_picker", "url", "color",
    "color_background", "range", "number", "checkbox", "select", "radio", "font_picker",
    "header", "paragraph", "video_url", "html", "product", "link_list", "text_alignment",
}
NO_DEFAULT = {"image_picker", "product", "link_list", "video_url"}  # Shopify rejects defaults here


def _visible_leftovers(liquid: str) -> list[str]:
    s = re.sub(r"{%-?\s*schema\s*-?%}.*?{%-?\s*endschema\s*-?%}", " ", liquid, flags=re.S)
    s = re.sub(r"{%-?\s*(style|stylesheet|javascript|comment)\s*-?%}.*?{%-?\s*end\1\s*-?%}", " ", s, flags=re.S)
    s = re.sub(r"<(style|script)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<svg\b.*?</svg>", " ", s, flags=re.S | re.I)
    s = re.sub(r"{%.*?%}", " ", s, flags=re.S)
    s = re.sub(r"{{.*?}}", " ", s, flags=re.S)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s, flags=re.S)
    s = re.sub(r"&[a-z#0-9]+;", " ", s)
    chunks = [c.strip() for c in re.split(r"\s{2,}|\n", s)]
    return [c for c in chunks if re.search(r"[A-Za-z]{2,}", c)]


def _check_settings(settings: list, where: str, errors: list[str]) -> None:
    ids = set()
    for st in settings:
        t = st.get("type")
        if t not in ALLOWED_TYPES:
            errors.append(f"{where}: setting type '{t}' not allowed")
            continue
        if t in ("header", "paragraph"):
            continue
        sid = st.get("id")
        if not sid:
            errors.append(f"{where}: setting of type {t} missing id")
        elif sid in ids:
            errors.append(f"{where}: duplicate setting id '{sid}'")
        ids.add(sid)
        if not st.get("label"):
            errors.append(f"{where}: setting '{sid}' missing label")
        if t in NO_DEFAULT and "default" in st:
            errors.append(f"{where}: '{sid}' ({t}) cannot have a default")
        if t == "richtext" and "default" in st and not str(st["default"]).lstrip().startswith(("<p", "<ul", "<ol", "<h")):
            errors.append(f"{where}: richtext '{sid}' default must be wrapped in <p>/<ul>/<ol>/<h*>")
        if t == "inline_richtext" and "default" in st and re.search(r"<(p|div|ul|ol|h\d)\b", str(st["default"])):
            errors.append(f"{where}: inline_richtext '{sid}' default cannot contain block tags")
        if t == "range":
            try:
                mn, mx, step, d = st["min"], st["max"], st["step"], st.get("default", st["min"])
                if (mx - mn) / step > 101:
                    errors.append(f"{where}: range '{sid}' has more than 101 steps")
                if not (mn <= d <= mx):
                    errors.append(f"{where}: range '{sid}' default outside min/max")
            except KeyError:
                errors.append(f"{where}: range '{sid}' needs min, max, step")
        if t in ("select", "radio") and not st.get("options"):
            errors.append(f"{where}: {t} '{sid}' needs options")


def validate_section(liquid: str) -> tuple[list[str], dict | None]:
    errors: list[str] = []
    m = re.search(r"{%-?\s*schema\s*-?%}(.*?){%-?\s*endschema\s*-?%}", liquid, flags=re.S)
    if not m:
        return ["missing {% schema %} block"], None
    try:
        schema = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        return [f"schema is not valid JSON: {e}"], None

    if len(schema.get("name", "")) > 25:
        errors.append("schema name must be <= 25 characters")
    _check_settings(schema.get("settings", []), "section", errors)
    types = set()
    for b in schema.get("blocks", []):
        bt = b.get("type")
        if bt in types:
            errors.append(f"duplicate block type '{bt}'")
        types.add(bt)
        if not b.get("name"):
            errors.append(f"block '{bt}' missing name")
        _check_settings(b.get("settings", []), f"block '{bt}'", errors)
    if not schema.get("presets"):
        errors.append("add a 'presets' entry so the section can also be added from the editor")

    outside_css = re.sub(r"{%-?\s*schema\s*-?%}.*?{%-?\s*endschema\s*-?%}", " ", liquid, flags=re.S)
    outside_css = re.sub(r"{%-?\s*(style|stylesheet)\s*-?%}.*?{%-?\s*end\1\s*-?%}", " ", outside_css, flags=re.S)
    outside_css = re.sub(r"<style\b.*?</style>", " ", outside_css, flags=re.S | re.I)
    if re.search(r"\|\s*font_face", outside_css):
        errors.append("font_face output must be inside {% style %}...{% endstyle %}; outside it, the @font-face CSS "
                      "prints as visible text on the page")
    if re.search(r"(@font-face|@media|[.#][\w-]+\s*\{[^}]*:[^}]*\})", re.sub(r"{{.*?}}|{%.*?%}", " ", outside_css, flags=re.S)):
        errors.append("CSS found outside {% style %}/<style>; it will render as visible text")

    leftovers = _visible_leftovers(liquid)
    if leftovers:
        errors.append("hardcoded visible text found (move it into settings/blocks): "
                      + " | ".join(leftovers[:15]))
    if re.search(r"<img[^>]+src=[\"']https?://", liquid):
        errors.append("hardcoded external <img src>; use image_picker + image_url/image_tag")
    if re.search(r"url\(\s*['\"]?https?://(?!fonts\.)", liquid):
        errors.append("hardcoded external url() in CSS; use an image_picker setting instead")
    return errors, schema


def validate_template(template_json: str, section_type: str, schema: dict | None) -> list[str]:
    errors: list[str] = []
    try:
        tpl = json.loads(template_json)
    except json.JSONDecodeError as e:
        return [f"template is not valid JSON: {e}"]
    secs = tpl.get("sections", {})
    order = tpl.get("order", [])
    if set(order) != set(secs):
        errors.append("'order' must list exactly the keys in 'sections'")
    ours = [k for k, v in secs.items() if v.get("type") == section_type]
    if not ours:
        errors.append(f"template does not use section type '{section_type}'")
    if schema:
        sec_ids = {s["id"] for s in schema.get("settings", []) if "id" in s}
        block_ids = {b["type"]: {s["id"] for s in b.get("settings", []) if "id" in s}
                     for b in schema.get("blocks", [])}
        for key in ours:
            sec = secs[key]
            for sid in sec.get("settings", {}):
                if sid not in sec_ids:
                    errors.append(f"template sets unknown section setting '{sid}'")
            blocks = sec.get("blocks", {})
            if set(sec.get("block_order", [])) != set(blocks):
                errors.append(f"section '{key}': block_order must match blocks keys")
            for bk, bv in blocks.items():
                bt = bv.get("type")
                if bt not in block_ids:
                    errors.append(f"block '{bk}' has unknown type '{bt}'")
                    continue
                for sid in bv.get("settings", {}):
                    if sid not in block_ids[bt]:
                        errors.append(f"block '{bk}' ({bt}) sets unknown setting '{sid}'")
    for m in re.finditer(r'"(https?://[^"]+\.(?:png|jpe?g|webp|gif)[^"]*)"', template_json):
        errors.append(f"image value must be a shopify://shop_images/... ref from upload_images, not {m.group(1)[:80]}")
    return errors