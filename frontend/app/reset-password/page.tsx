"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { authApi } from "../lib/portfolio-api";

export default function ResetPasswordPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [message, setMessage] = useState("");
  const [visible, setVisible] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (password !== confirmPassword) { setMessage("Passwords do not match."); return; }
    const token = new URLSearchParams(window.location.search).get("token");
    if (!token) { setMessage("This reset link is invalid or expired."); return; }
    try {
      await authApi.confirmPasswordReset(token, password);
      setMessage("Password reset successfully. Redirecting you to sign in...");
      window.setTimeout(() => router.push("/"), 1500);
    } catch { setMessage("This reset link is invalid or expired."); }
  }

  return <main className="signin-shell"><section className="signin-panel"><h1>Reset password</h1><p>{message || "Choose a new password."}</p><form className="signin-actions" onSubmit={submit}>
    <label>Password<span className="password-input"><input value={password} onChange={(e) => setPassword(e.target.value)} type={visible ? "text" : "password"} minLength={12} required /><button className="password-toggle" type="button" aria-label={visible ? "Hide password" : "Show password"} onClick={() => setVisible(!visible)}>{visible ? "◉" : "◌"}</button></span></label>
    <label>Confirm password<span className="password-input"><input value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} type={visible ? "text" : "password"} minLength={12} required /><button className="password-toggle" type="button" aria-label={visible ? "Hide password" : "Show password"} onClick={() => setVisible(!visible)}>{visible ? "◉" : "◌"}</button></span></label>
    <button type="submit">Reset password</button>
  </form><Link href="/">Back to sign in</Link></section></main>;
}
