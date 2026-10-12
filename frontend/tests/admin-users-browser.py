"""Run against a production Next server, Python Playwright and Edge.
ADMIN_TEST_BASE_URL defaults to http://127.0.0.1:3107. Set
ADMIN_TEST_EXPECT_INSECURE=true to verify the application UUID fallback over HTTP.
All API requests are intercepted; no live database or backend is contacted.
"""
import asyncio
import json
import os
import re
from urllib.parse import urlparse, parse_qs
from playwright.async_api import async_playwright, expect

BASE = os.environ.get("ADMIN_TEST_BASE_URL", "http://127.0.0.1:3107")
ADMIN_PATH = "/api/v1/auth/admin/users"

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        for is_admin in [False, True]:
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            identity = dict(userId="operator", displayName="Operator", email="operator@example.test", roles=["USER", "ADMIN"] if is_admin else ["USER"])
            await context.add_init_script("localStorage.setItem('aip.accessToken','fixture-token'); localStorage.setItem('aip.user'," + json.dumps(json.dumps(identity)) + ");")
            page = await context.new_page()
            calls, changes, errors, assets = [], [], [], []
            state = {"admin": False, "deny": False}
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: assets.append((response.url, response.status)) if "/_next/static/" in response.url else None)
            target = dict(userId="target", email="target@example.test", displayName="Target User", status="ACTIVE", emailVerifiedAt="2026-10-01T00:00:00Z", createdAt="2026-10-01T00:00:00Z", lastLoginAt=None, roles=["USER"])

            def paged(items):
                return dict(content=items, page=0, size=20, totalElements=len(items), totalPages=1)

            async def route(r):
                parsed = urlparse(r.request.url)
                path = parsed.path
                calls.append(path)
                assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", r.request.headers.get("x-correlation-id", "")), "Missing or invalid request correlation ID"
                data, status = [], 200
                if path.startswith(ADMIN_PATH):
                    assert r.request.headers.get("authorization") == "Bearer fixture-token"
                    if state["deny"]:
                        await r.fulfill(status=403, json={"message": "Administrator access required"})
                        return
                    current = dict(target, roles=["USER", "ADMIN"] if state["admin"] else ["USER"])
                    if r.request.method in ["PUT", "DELETE"]:
                        changes.append((r.request.method, r.request.post_data_json))
                        state["admin"] = r.request.method == "PUT"
                        await r.fulfill(status=204)
                        return
                    if path.endswith("/audit"):
                        data = paged([dict(id=str(i), userId="target", targetEmail=target["email"], role="ADMIN", action="GRANT" if method == "PUT" else "REVOKE", operator="user:operator", reason=body["reason"], createdAt="2026-10-09T12:00:00Z") for i, (method, body) in enumerate(changes)])
                    elif path == ADMIN_PATH:
                        search = parse_qs(parsed.query).get("search", [""])[0]
                        data = paged([] if search == "missing" else [current])
                    else:
                        data = current
                elif path.endswith("/dashboard"):
                    data = dict(currencyTotals={}, portfolios=[], incompleteValuationPortfolioIds=[])
                elif path.endswith("/summary"):
                    status, data = 404, {"message": "No persisted summary"}
                elif "ensure" in path:
                    data = {}
                await r.fulfill(status=status, json=data)

            # Intercept every API, including any background workspace requests.
            await context.route("**/api/**", route)
            await page.goto(BASE)
            capabilities = await page.evaluate("({secure:isSecureContext, native:typeof crypto.randomUUID})")
            if os.environ.get("ADMIN_TEST_EXPECT_INSECURE") == "true":
                assert capabilities == {"secure": False, "native": "undefined"}, capabilities
            await expect(page.get_by_role("button", name="Logout", exact=True)).to_be_visible()
            menu = page.get_by_role("button", name="Administration → Users & Roles", exact=True)
            if not is_admin:
                await expect(menu).to_have_count(0)
                assert not any(path.startswith(ADMIN_PATH) for path in calls)
            else:
                await menu.click()
                await page.get_by_role("button", name="Details & roles", exact=True).click()
                await expect(page.get_by_text("No role changes recorded.")).to_be_visible()
                reason = page.get_by_label("Reason for role change")
                grant = page.get_by_role("button", name="Grant ADMIN", exact=True)
                await expect(grant).to_be_disabled()
                await reason.fill("Approved ticket OPS-42")
                page.once("dialog", lambda dialog: dialog.dismiss())
                await grant.click()
                assert changes == []
                page.once("dialog", lambda dialog: dialog.accept())
                await grant.click()
                revoke = page.get_by_role("button", name="Revoke ADMIN", exact=True)
                await expect(revoke).to_be_visible()
                assert changes == [("PUT", {"reason": "Approved ticket OPS-42"})]
                await expect(page.get_by_text("Operator: user:operator")).to_be_visible()
                await reason.fill("Access window ended")
                page.once("dialog", lambda dialog: dialog.accept())
                await revoke.click()
                await expect(grant).to_be_visible()
                assert changes[-1] == ("DELETE", {"reason": "Access window ended"})
                search = page.get_by_label("Search users by email or name")
                await search.fill("missing")
                await page.get_by_role("button", name="Search", exact=True).click()
                await expect(page.get_by_text("No users found.")).to_be_visible()
                state["deny"] = True
                await search.fill("target")
                await page.get_by_role("button", name="Search", exact=True).click()
                await expect(page.get_by_text("Administrator access is no longer available. Sign in again to refresh your permissions.")).to_be_visible()
                await expect(page.get_by_role("button", name="Grant ADMIN", exact=True)).to_have_count(0)
            # Existing workspace reads localStorage in its initial render; a seeded
            # authenticated session differs from the server's signed-out markup.
            unexpected = [error for error in errors if not (error.startswith("Hydration failed because the server rendered HTML didn't match the client.") or error.startswith("Minified React error #418;"))]
            assert not unexpected, unexpected
            assert any(".js" in url for url, status in assets), "No production JavaScript loaded"
            assert any(".css" in url for url, status in assets), "No production CSS loaded"
            assert all(status == 200 for url, status in assets), assets
            print(f"PASS: {BASE} {'ADMIN' if is_admin else 'USER'} {capabilities}; {len(calls)} mocked requests with UUID v4 correlation IDs; {len(errors)} known hydration errors; {len(assets)} static assets HTTP 200")
            await context.close()
        await browser.close()
    print("PASS: USER menu hidden; ADMIN search/details; required reason; cancel/confirm grant/revoke; authenticated API transport; audit; denied-access cleanup. All APIs mocked.")

asyncio.run(main())
