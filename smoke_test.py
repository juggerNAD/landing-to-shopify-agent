"""Run this before agent.py: checks auth, scopes, and that theme writes work.

    python smoke_test.py
"""
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from tools.shopify import Shopify  # noqa: E402

TEST_FILE = "sections/zz-smoke-test.liquid"
TEST_BODY = """<div>{{ section.settings.t }}</div>
{% schema %}{"name": "Smoke test", "settings": [{"type": "text", "id": "t", "label": "Text", "default": "ok"}]}{% endschema %}
"""


async def main() -> int:
    s = Shopify()
    ok = True

    def report(name, passed, detail=""):
        nonlocal ok
        ok &= bool(passed)
        print(f"[{'PASS' if passed else 'FAIL'}] {name} {detail}")

    try:
        shop = await s.gql("query { shop { name myshopifyDomain } currentAppInstallation { accessScopes { handle } } }")
        scopes = {x["handle"] for x in shop["currentAppInstallation"]["accessScopes"]}
        report("auth", True, f"-> {shop['shop']['name']} ({shop['shop']['myshopifyDomain']})")
    except Exception as e:
        report("auth", False, str(e)[:300])
        return 1

    needed = {"write_themes", "read_themes", "write_files", "read_files", "write_products",
              "read_products", "write_publications", "read_publications"}
    # write_x implies read_x, so only flag a read scope if its write is missing too
    missing = {n for n in needed if n not in scopes and n.replace("read_", "write_") not in scopes}
    report("scopes", not missing, f"missing: {sorted(missing)}" if missing else "")

    try:
        t = await s.theme()
        report("theme", True, f"-> {t['name']} ({t['role']})")
    except Exception as e:
        report("theme", False, str(e)[:300])
        return 1

    try:
        res = await s.upsert_theme_file(TEST_FILE, TEST_BODY)
        report("theme write", res["ok"], f"-> via {res.get('via')}" if res["ok"] else str(res)[:600])
        if res["ok"] and res.get("via") == "admin-api":
            await s.gql(
                """mutation($id: ID!, $f: [String!]!) { themeFilesDelete(themeId: $id, files: $f) {
                     deletedThemeFiles { filename } userErrors { message } } }""",
                {"id": t["id"], "f": [TEST_FILE]})
            print("       (test file removed)")
        elif res["ok"]:
            print(f"       ({TEST_FILE} left on the theme; it is unused and harmless)")
    except Exception as e:
        report("theme write", False, str(e)[:600])

    try:
        await s.gql("query { files(first: 1) { nodes { id } } }")
        report("files read", True)
    except Exception as e:
        report("files read", False, str(e)[:200])

    try:
        pubs = await s.gql("query { publications(first: 10) { nodes { name } } }")
        names = [p["name"] for p in pubs["publications"]["nodes"]]
        report("publications", "Online Store" in names, f"-> {names}")
    except Exception as e:
        report("publications", False, str(e)[:200])

    print("\nAll good - run agent.py" if ok else "\nFix the FAIL lines above first.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
