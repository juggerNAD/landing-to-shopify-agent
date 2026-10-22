"""Thin async wrapper around the Shopify Admin GraphQL API."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx


class ShopifyError(RuntimeError):
    pass


class Shopify:
    def __init__(self) -> None:
        self.store = os.environ["SHOPIFY_STORE"].replace("https://", "").strip("/")
        self.version = os.getenv("SHOPIFY_API_VERSION", "2026-07")
        self.token = os.getenv("SHOPIFY_ADMIN_TOKEN") or None
        self._theme: dict | None = None
        # "auto" tries the Admin API first and falls back to the Shopify CLI when
        # Shopify denies themeFilesUpsert (it requires an exemption for most apps).
        self.theme_write_mode = os.getenv("SHOPIFY_THEME_WRITE", "auto").lower()
        self.cli_theme_dir = Path("output") / "theme_src"

    # ---------- auth / transport ----------
    async def _get_token(self) -> str:
        if self.token:
            return self.token
        cid, secret = os.getenv("SHOPIFY_CLIENT_ID"), os.getenv("SHOPIFY_CLIENT_SECRET")
        if not (cid and secret):
            raise ShopifyError("Set SHOPIFY_ADMIN_TOKEN, or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET")
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"https://{self.store}/admin/oauth/access_token",
                             data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret})
            r.raise_for_status()
            self.token = r.json()["access_token"]
        return self.token

    async def gql(self, query: str, variables: dict | None = None, theme_access: bool = False) -> dict:
        if theme_access:
            # Theme Access passwords (shptka_) go through Shopify's theme-kit-access proxy,
            # which is how the Shopify CLI is allowed to write theme files.
            token = os.getenv("SHOPIFY_THEME_PASSWORD", "")
            url = f"https://theme-kit-access.shopifyapps.com/cli/admin/api/{self.version}/graphql.json"
            headers = {"X-Shopify-Access-Token": token, "X-Shopify-Shop": self.store}
        else:
            token = await self._get_token()
            url = f"https://{self.store}/admin/api/{self.version}/graphql.json"
            headers = {"X-Shopify-Access-Token": token}
        for attempt in range(4):
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(url, json={"query": query, "variables": variables or {}}, headers=headers)
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            errs = data.get("errors")
            if errs:
                if any("THROTTLED" in json.dumps(e) for e in errs) and attempt < 3:
                    await asyncio.sleep(2)
                    continue
                raise ShopifyError(json.dumps(errs)[:2000])
            return data["data"]
        raise ShopifyError("Shopify API kept failing after retries")

    # ---------- theme ----------
    async def theme(self) -> dict:
        if self._theme:
            return self._theme
        data = await self.gql("query { themes(first: 30) { nodes { id name role } } }")
        themes = data["themes"]["nodes"]
        wanted = os.getenv("SHOPIFY_THEME_ID")
        if wanted:
            gid = wanted if wanted.startswith("gid://") else f"gid://shopify/OnlineStoreTheme/{wanted}"
            match = [t for t in themes if t["id"] == gid]
        else:
            match = [t for t in themes if t["role"] == "MAIN"]
        if not match:
            raise ShopifyError(f"Theme not found. Available: {themes}")
        self._theme = match[0]
        return self._theme

    async def upsert_theme_file(self, filename: str, content: str) -> dict:
        theme = await self.theme()
        if self.theme_write_mode == "auto":
            try:
                res = await self._upsert_via_api(theme, filename, content, theme_access=False)
                self.theme_write_mode = "admin"
                return res
            except ShopifyError as e:
                if "exemption" not in str(e) and "Access denied" not in str(e) and "ACCESS_DENIED" not in str(e):
                    raise
                self.theme_write_mode = "theme_access" if os.getenv("SHOPIFY_THEME_PASSWORD") else "cli"
        if self.theme_write_mode == "admin":
            return await self._upsert_via_api(theme, filename, content, theme_access=False)
        if self.theme_write_mode == "theme_access":
            try:
                return await self._upsert_via_api(theme, filename, content, theme_access=True)
            except (ShopifyError, httpx.HTTPError) as e:
                if "userErrors" in str(e):
                    raise
                self.theme_write_mode = "cli"  # endpoint unavailable; fall back for the rest of the run
        out = await asyncio.to_thread(self._push_via_cli, theme, filename, content)
        if not out["ok"]:
            return out
        return await self._verify(theme, filename, content, via="shopify-cli")

    async def _upsert_via_api(self, theme: dict, filename: str, content: str, theme_access: bool) -> dict:
        data = await self.gql(
            """mutation($themeId: ID!, $files: [OnlineStoreThemeFilesUpsertFileInput!]!) {
                 themeFilesUpsert(themeId: $themeId, files: $files) {
                   upsertedThemeFiles { filename }
                   job { id }
                   userErrors { field message code filename }
                 } }""",
            {"themeId": theme["id"], "files": [{"filename": filename, "body": {"type": "TEXT", "value": content}}]},
            theme_access=theme_access)
        res = data["themeFilesUpsert"]
        if res["userErrors"]:
            msg = json.dumps(res["userErrors"])
            if "exemption" in msg or "ACCESS_DENIED" in msg:
                raise ShopifyError(msg)
            # Liquid/schema errors come back here - return them so the agent can fix the file.
            return {"ok": False, "errors": res["userErrors"]}
        if res.get("job"):
            for _ in range(30):
                j = await self.gql("query($id: ID!) { job(id: $id) { done } }", {"id": res["job"]["id"]},
                                   theme_access=theme_access)
                if j["job"]["done"]:
                    break
                await asyncio.sleep(1)
        return await self._verify(theme, filename, content, via="theme-access-api" if theme_access else "admin-api")

    async def _verify(self, theme: dict, filename: str, content: str, via: str) -> dict:
        # Shopify can accept a write but keep the old file if Liquid/JSON is
        # invalid, so confirm the stored checksum matches what we sent.
        expected = hashlib.md5(content.encode("utf-8")).hexdigest()
        check = await self.gql(
            """query($id: ID!, $f: [String!]) { theme(id: $id) {
                 files(filenames: $f, first: 1) { nodes { filename checksumMd5 size } } } }""",
            {"id": theme["id"], "f": [filename]})
        nodes = check["theme"]["files"]["nodes"]
        if not nodes:
            return {"ok": False, "errors": [f"{filename} was not stored - Shopify rejected it (invalid Liquid/schema/JSON?)"]}
        if nodes[0]["checksumMd5"] and nodes[0]["checksumMd5"] != expected:
            return {"ok": False, "errors": [f"{filename} on the theme does not match what was sent - the upload was rejected. "
                                            "Re-check schema validity (unique ids, setting types, defaults, block types)."]}
        return {"ok": True, "filename": filename, "theme": theme["name"], "via": via}

    # ---------- Shopify CLI fallback ----------
    def _cli(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        exe = shutil.which("shopify")
        if not exe:
            raise ShopifyError("Shopify CLI not found. Install it with: npm install -g @shopify/cli@latest")
        cmd = [exe, *args, "--store", self.store]
        env = {**os.environ, "SHOPIFY_CLI_NO_ANALYTICS": "1"}
        pw = os.getenv("SHOPIFY_THEME_PASSWORD")
        if pw:
            cmd += ["--password", pw]
            env["CI"] = "1"
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", env=env, **kw)
        try:
            out, err = proc.communicate("y\n", timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the whole process tree; on Windows the CLI runs node under a .cmd wrapper.
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            else:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                proc.communicate(timeout=10)
            except Exception:
                pass
            raise ShopifyError(f"shopify {' '.join(args[:2])} hung for {timeout}s (likely an interactive prompt) "
                               "and was stopped.")
        clean = lambda t: re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", t or "")
        return subprocess.CompletedProcess(cmd, proc.returncode, clean(out), clean(err))

    def _push_via_cli(self, theme: dict, filename: str, content: str) -> dict:
        theme_id = theme["id"].split("/")[-1]
        d = self.cli_theme_dir
        if not (d / "layout" / "theme.liquid").exists():
            d.mkdir(parents=True, exist_ok=True)
            p = self._cli("theme", "pull", "--theme", theme_id, "--path", str(d), timeout=600)
            if p.returncode != 0 or not (d / "layout" / "theme.liquid").exists():
                return {"ok": False, "errors": ["shopify theme pull failed: " + (p.stderr or p.stdout)[-1500:]]}
        target = d / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        p = self._cli("theme", "push", "--theme", theme_id, "--path", str(d), "--only", filename,
                      "--allow-live", "--nodelete", timeout=120)
        log = (p.stdout + "\n" + p.stderr).strip()
        # Drop the CLI's box-drawing borders so the agent can read the actual error.
        log = re.sub(r"[\u2500-\u257f]+", " ", log)
        log = re.sub(r"[ \t]{2,}", " ", log)
        if p.returncode != 0 or re.search(r"\berror\b", log, re.I):
            return {"ok": False, "errors": ["shopify theme push failed: " + log[-1500:]]}
        return {"ok": True}

    # ---------- files ----------
    @staticmethod
    def _clean_src(url: str) -> str:
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        p = urlparse(url)
        # Drop ?width=/&crop= etc. so Shopify fetches the original resolution.
        return f"{p.scheme}://{p.netloc}{p.path}"

    async def upload_images(self, urls: list[str]) -> dict:
        clean = list(dict.fromkeys(self._clean_src(u) for u in urls if u))
        mapping, errors = {}, []
        for i in range(0, len(clean), 10):
            batch = clean[i:i + 10]
            files = [{"originalSource": u, "contentType": "IMAGE",
                      "filename": PurePosixPath(urlparse(u).path).name} for u in batch]
            data = await self.gql(
                """mutation($files: [FileCreateInput!]!) { fileCreate(files: $files) {
                     files { id fileStatus }
                     userErrors { field message code } } }""", {"files": files})
            fc = data["fileCreate"]
            errors += fc["userErrors"]
            ids = {f["id"]: src for f, src in zip(fc["files"] or [], batch)}
            pending = dict(ids)
            for _ in range(40):
                if not pending:
                    break
                await asyncio.sleep(1.5)
                q = await self.gql(
                    """query($ids: [ID!]!) { nodes(ids: $ids) {
                         ... on MediaImage { id fileStatus image { url width height } fileErrors { message } } } }""",
                    {"ids": list(pending)})
                for n in q["nodes"]:
                    if not n:
                        continue
                    if n["fileStatus"] == "READY" and n.get("image"):
                        src = pending.pop(n["id"])
                        name = PurePosixPath(urlparse(n["image"]["url"]).path).name
                        mapping[src] = {"ref": f"shopify://shop_images/{name}",
                                        "cdn": n["image"]["url"],
                                        "w": n["image"]["width"], "h": n["image"]["height"]}
                    elif n["fileStatus"] == "FAILED":
                        src = pending.pop(n["id"])
                        errors.append({"src": src, "error": n.get("fileErrors")})
            for fid, src in pending.items():
                errors.append({"src": src, "error": "timed out waiting for READY"})

        # Let callers look up by the URL they passed in, not only the cleaned one.
        for u in urls:
            c = self._clean_src(u) if u else None
            if c in mapping and u not in mapping:
                mapping[u] = mapping[c]
        return {"images": mapping, "errors": errors}

    # ---------- product ----------
    async def upsert_product(self, title: str, handle: str, template_suffix: str,
                             price: str = "49.99", image_url: str | None = None,
                             description_html: str = "") -> dict:
        found = await self.gql(
            "query($q: String!) { products(first: 1, query: $q) { nodes { id handle } } }",
            {"q": f"handle:{handle}"})
        nodes = found["products"]["nodes"]
        if nodes:
            pid = nodes[0]["id"]
            upd = await self.gql(
                """mutation($p: ProductUpdateInput!) { productUpdate(product: $p) {
                     product { id handle } userErrors { field message } } }""",
                {"p": {"id": pid, "templateSuffix": template_suffix, "title": title, "status": "ACTIVE"}})
            if upd["productUpdate"]["userErrors"]:
                raise ShopifyError(json.dumps(upd["productUpdate"]["userErrors"]))
            created = False
        else:
            media = [{"originalSource": self._clean_src(image_url), "mediaContentType": "IMAGE"}] if image_url else None
            res = await self.gql(
                """mutation($p: ProductCreateInput!, $m: [CreateMediaInput!]) {
                     productCreate(product: $p, media: $m) {
                       product { id handle variants(first: 1) { nodes { id } } }
                       userErrors { field message } } }""",
                {"p": {"title": title, "handle": handle, "status": "ACTIVE",
                       "templateSuffix": template_suffix, "descriptionHtml": description_html,
                       "vendor": "Placeholder"},
                 "m": media})
            pc = res["productCreate"]
            if pc["userErrors"]:
                raise ShopifyError(json.dumps(pc["userErrors"]))
            pid = pc["product"]["id"]
            handle = pc["product"]["handle"]
            vid = pc["product"]["variants"]["nodes"][0]["id"]
            await self.gql(
                """mutation($pid: ID!, $v: [ProductVariantsBulkInput!]!) {
                     productVariantsBulkUpdate(productId: $pid, variants: $v) { userErrors { message } } }""",
                {"pid": pid, "v": [{"id": vid, "price": price, "compareAtPrice": None}]})
            created = True

        published = await self._publish(pid)
        theme = await self.theme()
        url = f"https://{self.store}/products/{handle}"
        if theme["role"] != "MAIN":
            url += f"?preview_theme_id={theme['id'].split('/')[-1]}"
        return {"product_id": pid, "handle": handle, "created": created,
                "published_to_online_store": published, "storefront_url": url,
                "editor_url": f"https://{self.store}/admin/themes/{theme['id'].split('/')[-1]}/editor"
                              f"?previewPath=/products/{handle}"}

    async def _publish(self, pid: str) -> bool | str:
        try:
            pubs = await self.gql("query { publications(first: 20) { nodes { id name } } }")
            online = [p for p in pubs["publications"]["nodes"] if p["name"] == "Online Store"]
            if not online:
                return "Online Store publication not found"
            r = await self.gql(
                """mutation($id: ID!, $in: [PublicationInput!]!) {
                     publishablePublish(id: $id, input: $in) { userErrors { message } } }""",
                {"id": pid, "in": [{"publicationId": online[0]["id"]}]})
            errs = r["publishablePublish"]["userErrors"]
            return True if not errs else json.dumps(errs)
        except ShopifyError as e:
            return f"could not publish (add read/write_publications scopes?): {e}"[:300]