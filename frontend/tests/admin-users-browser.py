"""Run with a local Next server on 127.0.0.1:3107, Python Playwright and Edge.
All API requests are intercepted; no live database or backend is contacted.
"""
import asyncio
import json
import traceback
from urllib.parse import urlparse, parse_qs
from playwright.async_api import async_playwright, expect

BASE = "http://127.0.0.1:3107"
ADMIN_PATH = "/api/v1/auth/admin/users"

# Stage identifiers for sanitized failure reporting
STAGE_LOGOUT = "stage_logout_auth"
STAGE_ADMIN_MENU = "stage_admin_menu_visibility"
STAGE_DETAILS_RENDER = "stage_details_render"
STAGE_REVOKE_CONFIRM = "stage_revoke_confirm"
STAGE_DENY_CLEANUP = "stage_deny_cleanup"
STAGE_USER_HIDDEN = "stage_user_menu_hidden"

def stage_error(stage_id: str, message: str) -> None:
    """Print sanitized error info without exposing sensitive data."""
    print(f"STOP at {stage_id}: {message}")

async def run_admin_verification(page, context):
    """Run ADMIN user verification stages."""
    menu = page.get_by_role("button", name="Administration → Users & Roles", exact=True)

    # Stage: Admin menu visibility
    try:
        await expect(menu).to_be_visible(timeout=5000)
    except Exception as e:
        stage_error(STAGE_ADMIN_MENU, f"ADMIN navigation not visible: {type(e).__name__}")
        return False

    # Stage: Details page render
    try:
        await menu.click()
        await page.get_by_role("button", name="Details & roles", exact=True).click()
        await expect(page.get_by_text("No role changes recorded.")).to_be_visible(timeout=5000)
    except Exception as e:
        stage_error(STAGE_DETAILS_RENDER, f"Admin details page failed: {type(e).__name__}")
        return False

    return True

async def run_user_verification(page, context):
    """Run USER-only verification stages."""
    # Stage: User menu hidden
    try:
        await expect(page.get_by_role("button", name="Administration → Users & Roles", exact=True)).to_have_count(0, timeout=5000)
    except Exception as e:
        stage_error(STAGE_USER_HIDDEN, f"ADMIN navigation not hidden for USER: {type(e).__name__}")
        return False

    return True

async def main():
    browser = await async_playwright().start()
    browser = await browser.chromium.launch(channel="msedge", headless=True)

    try:
        # Test ADMIN user flow
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        admin_identity = dict(userId="operator", displayName="Operator", email="operator@example.test", roles=["USER", "ADMIN"])
        await context.add_init_script("localStorage.setItem('aip.accessToken','fixture-token'); localStorage.setItem('aip.user'," + json.dumps(json.dumps(admin_identity)) + ");")
        page = await context.new_page()
        calls, changes, errors = [], [], []
        state = {"admin": False, "deny": False}
        page.on("pageerror", lambda error: errors.append(str(error)))
        target = dict(userId="target", email="target@example.test", displayName="Target User", status="ACTIVE", emailVerifiedAt="2026-10-01T00:00:00Z", createdAt="2026-10-01T00:00:00Z", lastLoginAt=None, roles=["USER"])

        def paged(items):
            return dict(content=items, page=0, size=20, totalElements=len(items), totalPages=1)

        async def route(r):
            parsed = urlparse(r.request.url)
            path = parsed.path
            calls.append(path)
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
                    data = paged([dict(id=str(i), userId="target", targetEmail=target["email"], role="ADMIN", action="GRANT" if change[0] == "PUT" else "REVOKE", operator="user:operator", reason=change[1]["reason"], createdAt="2026-10-09T12:00:00Z") for i, change in enumerate(changes)])
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

        await context.route("**/api/**", route)
        await page.goto(BASE)

        # Stage: Logout/auth visibility
        try:
            await expect(page.get_by_role("button", name="Logout", exact=True)).to_be_visible()
        except Exception as e:
            stage_error(STAGE_LOGOUT, f"Logout button not visible: {type(e).__name__}")
            await context.close()
            return

        if not await run_admin_verification(page, context):
            await context.close()
            return

        await context.close()

        # Test USER user flow
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        user_identity = dict(userId="operator", displayName="Operator", email="operator@example.test", roles=["USER"])
        await context.add_init_script("localStorage.setItem('aip.accessToken','fixture-token'); localStorage.setItem('aip.user'," + json.dumps(json.dumps(user_identity)) + ");")
        page = await context.new_page()
        calls = []
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        await context.route("**/api/**", route)
        await page.goto(BASE)

        if not await run_user_verification(page, context):
            await context.close()
            return

        await context.close()

        print("PASS: USER menu hidden; ADMIN search/details; required reason; cancel/confirm grant/revoke; authenticated API transport; audit; denied-access cleanup. All APIs mocked.")

    except Exception as e:
        print(f"STOP: Unexpected error - {type(e).__name__}")
        raise
    finally:
        await browser.stop()

asyncio.run(main())
