import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const verifyPage = readFileSync(new URL("../app/verify-email/page.tsx", import.meta.url), "utf8");
const workspace = readFileSync(new URL("../app/components/investment-workspace.tsx", import.meta.url), "utf8");
const resetPage = readFileSync(new URL("../app/reset-password/page.tsx", import.meta.url), "utf8");

test("verification success is an exclusive public confirmation with a scheduled sign-in redirect", () => {
  const success = verifyPage.match(/if \(state === "SUCCESS"\)[\s\S]*?\n  }\n\n  if \(state === "INVALID"\)/)?.[0] ?? "";
  assert.match(success, /Email verified successfully\./);
  assert.match(success, /Redirecting you to sign in\.\.\./);
  assert.match(success, /Sign in now/);
  assert.match(verifyPage, /window\.setTimeout\(\(\) => router\.push\("\/"\), 1500\)/);
  assert.doesNotMatch(success, /Resend verification email|Check your email|Verify email/);
});

test("invalid verification links expose only recovery actions", () => {
  const invalid = verifyPage.match(/if \(state === "INVALID"\)[\s\S]*?\n  }\n\n  return/ )?.[0] ?? "";
  assert.match(invalid, /Verification link expired/);
  assert.match(invalid, /This verification link is invalid or expired\./);
  assert.match(invalid, /Resend verification email/);
  assert.doesNotMatch(invalid, /Email verified successfully\./);
});

test("public verification route does not mount the authenticated workspace", () => {
  assert.match(verifyPage, /authApi\.verifyEmail\(token\)/);
  assert.doesNotMatch(verifyPage, /InvestmentWorkspace|portfolioApi|Your session expired/);
});

test("pending registration keeps resend available without normal-mode manual token entry", () => {
  assert.match(workspace, /Check your email and open the verification link\./);
  assert.match(workspace, /Resend verification email/);
  assert.match(workspace, /frontendConfig\.authDevLoginEnabled \? <input name="token"/);
});

test("logout clears authenticated state and returns to the canonical login route", () => {
  const logout = workspace.match(/function logout\(\) \{[\s\S]*?\n  }\n\n  if \(authLoading\)/)?.[0] ?? "";
  assert.match(logout, /removeItem\("aip\.accessToken"\)/);
  assert.match(logout, /removeItem\("aip\.user"\)/);
  assert.match(logout, /window\.history\.replaceState\(\{\}, "", "\/"\)/);
  assert.match(logout, /setError\(null\)/);
  assert.match(logout, /setAccessToken\(null\)/);
  assert.match(logout, /setAuthenticatedUser\(null\)/);
});

test("registration surfaces the safe active-account guidance and permits pending re-registration", () => {
  assert.match(workspace, /submit\(form\)\.catch\(\(err\) => setMessage\(getApiFailure\(err\)\.message\)\)/);
  assert.match(workspace, /We sent a verification link to your email address\./);
});

test("public sign-in exposes an enumeration-safe resend-verification recovery", () => {
  assert.match(workspace, /setMode\("RESEND"\)/);
  assert.match(workspace, /authApi\.resendVerification\(email\)/);
  assert.match(workspace, /If this account is awaiting verification, a new verification email has been sent\./);
  assert.doesNotMatch(workspace.match(/mode === "RESEND"[\s\S]*?return;/)?.[0] ?? "", /token/);
});

test("public password fields use inline accessible eye controls", () => {
  assert.match(workspace, /className="password-input"/);
  assert.match(workspace, /className="password-toggle"/);
  assert.match(workspace, /aria-label=\{visible \? "Hide password" : "Show password"\}/);
  assert.match(workspace, /name="confirmPassword"/);
  assert.doesNotMatch(workspace, /Show or hide password/);
  assert.match(resetPage, /className="password-toggle"/);
  assert.match(resetPage, /new URLSearchParams\(window\.location\.search\)\.get\("token"\)/);
  assert.doesNotMatch(resetPage, /token\}\s*<\//);
});

test("public auth cannot mount workspace requests with only a stale token", () => {
  assert.match(workspace, /if \(accessToken && authenticatedUser\) \{\s*void loadPortfolios\(\)/);
  assert.match(workspace, /if \(accessToken && authenticatedUser\) \{\s*void loadBrokers\(\)/);
  assert.match(workspace, /setError\(null\)/);
});
