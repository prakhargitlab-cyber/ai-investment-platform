"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { authApi } from "../lib/portfolio-api";

type VerificationState = "VERIFYING" | "SUCCESS" | "INVALID";

export default function VerifyEmailPage() {
  const router = useRouter();
  const [state, setState] = useState<VerificationState>("VERIFYING");
  const [email, setEmail] = useState("");
  const [resendMessage, setResendMessage] = useState("");

  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get("token");
    if (!token) {
      queueMicrotask(() => setState("INVALID"));
      return;
    }

    let active = true;
    let redirectTimer: number | undefined;
    void authApi.verifyEmail(token)
      .then(() => {
        if (!active) return;
        setState("SUCCESS");
        redirectTimer = window.setTimeout(() => router.push("/"), 1500);
      })
      .catch(() => {
        if (active) setState("INVALID");
      });

    return () => {
      active = false;
      if (redirectTimer) window.clearTimeout(redirectTimer);
    };
  }, [router]);

  if (state === "SUCCESS") {
    return (
      <main className="signin-shell">
        <section className="signin-panel">
          <h1>Email verified</h1>
          <p>Email verified successfully.</p>
          <p>Redirecting you to sign in...</p>
          <Link className="button" href="/">Sign in now</Link>
        </section>
      </main>
    );
  }

  if (state === "INVALID") {
    return (
      <main className="signin-shell">
        <section className="signin-panel">
          <h1>Verification link expired</h1>
          <p>This verification link is invalid or expired.</p>
          <form className="signin-actions" onSubmit={(event) => {
            event.preventDefault();
            void authApi.resendVerification(email).then((result) => setResendMessage(result.message));
          }}>
            <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required placeholder="Email" />
            <button type="submit">Resend verification email</button>
          </form>
          {resendMessage ? <p role="status">{resendMessage}</p> : null}
          <Link href="/">Back to sign in</Link>
        </section>
      </main>
    );
  }

  return (
    <main className="signin-shell">
      <section className="signin-panel">
        <h1>Verifying your email...</h1>
      </section>
    </main>
  );
}
