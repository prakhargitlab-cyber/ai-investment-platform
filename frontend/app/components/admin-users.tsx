"use client";

import { useEffect, useState } from "react";
import { adminApi, type AdminPage, type AdminUser, type RoleAudit } from "../lib/admin-api";
import { Badge, Button, Card, Field } from "./ui";
import "./admin-users.css";

export function AdminUsers({ operatorId, roles }: { operatorId: string; roles: string[] }) {
  if (!roles.includes("ADMIN")) return <p role="alert">Administrator access required.</p>;
  return <UsersAndRoles key={operatorId} operatorId={operatorId} />;
}

function UsersAndRoles({ operatorId }: { operatorId: string }) {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [users, setUsers] = useState<AdminPage<AdminUser> | null>(null);
  const [selected, setSelected] = useState<AdminUser | null>(null);
  const [audit, setAudit] = useState<AdminPage<RoleAudit> | null>(null);
  const [auditPage, setAuditPage] = useState(0);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [denied, setDenied] = useState(false);
  function failed(failure: unknown) {
    const value = failure as { message?: string; status?: number };
    if (value.status === 403 || value.status === 401) {
      setDenied(true); setUsers(null); setSelected(null); setAudit(null);
    }
    setError(value.message ?? "The request could not be completed.");
  }

  useEffect(() => {
    if (denied) return;
    let cancelled = false;
    setUsers(null);
    adminApi.users(search, page).then(result => { if (!cancelled) setUsers(result); })
      .catch(failure => { if (!cancelled) failed(failure); });
    return () => { cancelled = true; };
  }, [search, page, revision, denied]);

  const selectedId = selected?.userId;
  useEffect(() => {
    if (!selectedId || denied) return;
    let cancelled = false;
    setAudit(null);
    Promise.all([adminApi.user(selectedId), adminApi.audit(selectedId, auditPage)])
      .then(([user, history]) => { if (!cancelled) { setSelected(user); setAudit(history); } })
      .catch(failure => { if (!cancelled) failed(failure); });
    return () => { cancelled = true; };
  }, [selectedId, auditPage, revision, denied]);

  async function changeRole() {
    if (!selected || busy || !reason.trim()) return;
    const grant = !selected.roles.includes("ADMIN");
    if (!window.confirm(`${grant ? "Grant ADMIN to" : "Revoke ADMIN from"} ${selected.email}?\nReason: ${reason.trim()}`)) return;
    setBusy(true); setError(""); setSuccess("");
    try {
      await adminApi.change(selected.userId, grant, reason.trim());
      setSuccess(`ADMIN ${grant ? "granted" : "revoked"}. New permissions appear after the affected user signs in again. Revoked admin API access ends immediately.`);
      setReason(""); setRevision(value => value + 1);
    } catch (failure) { failed(failure); }
    finally { setBusy(false); }
  }

  if (denied) return <p role="alert">Administrator access is no longer available. Sign in again to refresh your permissions.</p>;
  return <div className="admin-users">
    {error && <p role="alert">{error} <Button variant="secondary" onClick={() => { setError(""); setRevision(value => value + 1); }}>Retry</Button></p>}
    {success && <p role="status">{success}</p>}
    <Card>
      <form onSubmit={event => { event.preventDefault(); setPage(0); setSearch(query); setError(""); }}>
        <Field label="Search users by email or name"><input type="search" maxLength={320} value={query} onChange={event => setQuery(event.target.value)} /></Field>
        <Button type="submit">Search</Button>
      </form>
      {!users ? <p role="status">Loading users…</p> : <>
        <p>{users.totalElements} users</p>
        <div className="admin-table-scroll"><table><thead><tr><th>User</th><th>Status</th><th>Roles</th><th>Actions</th></tr></thead><tbody>
          {users.content.map(user => <tr key={user.userId}>
            <td>{user.displayName}<br />{user.email}</td><td>{user.status ?? "Legacy"} · {user.emailVerifiedAt ? "Verified" : "Unverified"}</td>
            <td>{user.roles.map(role => <Badge key={role}>{role}</Badge>)}</td>
            <td><Button variant="secondary" disabled={busy} onClick={() => { setSelected(user); setAudit(null); setAuditPage(0); setReason(""); setSuccess(""); }}>Details & roles</Button></td>
          </tr>)}
        </tbody></table></div>
        {users.content.length === 0 && <p>No users found.</p>}
        <Button variant="secondary" disabled={page === 0} onClick={() => setPage(value => value - 1)}>Previous users</Button>
        <span> Page {page + 1} of {Math.max(1, users.totalPages)} </span>
        <Button variant="secondary" disabled={page + 1 >= users.totalPages} onClick={() => setPage(value => value + 1)}>Next users</Button>
      </>}
    </Card>
    {selected && <Card>
      <h2>{selected.email}</h2>
      <p>{selected.displayName} · {selected.status} · Roles: {selected.roles.join(", ")}</p>
      <p>Created: {new Date(selected.createdAt).toLocaleString()} · Last login: {selected.lastLoginAt ? new Date(selected.lastLoginAt).toLocaleString() : "Never"}</p>
      {selected.userId === operatorId ? <p>You cannot change your own roles.</p> : <form onSubmit={event => { event.preventDefault(); void changeRole(); }}>
        <Field label="Reason for role change" hint="Required for the audit history. Do not include passwords or secrets.">
          <textarea required maxLength={500} value={reason} disabled={busy} onChange={event => setReason(event.target.value)} />
        </Field>
        <Button type="submit" variant={selected.roles.includes("ADMIN") ? "danger" : "primary"}
          disabled={busy || !reason.trim() || (!selected.roles.includes("ADMIN") && (selected.status !== "ACTIVE" || !selected.emailVerifiedAt))}>
          {busy ? "Saving…" : selected.roles.includes("ADMIN") ? "Revoke ADMIN" : "Grant ADMIN"}
        </Button>
      </form>}
      <h3>Role-change audit history</h3>
      {!audit ? <p role="status">Loading history…</p> : <>
        {audit.content.length === 0 ? <p>No role changes recorded.</p> : <ol>{audit.content.map(entry => <li key={entry.id}>
          <strong>{entry.action} {entry.role}</strong> · {new Date(entry.createdAt).toLocaleString()}<br />
          Operator: {entry.operator}<p>{entry.reason ?? "No reason recorded (legacy)"}</p>
        </li>)}</ol>}
        <Button variant="secondary" disabled={auditPage === 0 || busy} onClick={() => setAuditPage(value => value - 1)}>Previous audit records</Button>
        <span> Page {auditPage + 1} of {Math.max(1, audit.totalPages)} </span>
        <Button variant="secondary" disabled={auditPage + 1 >= audit.totalPages || busy} onClick={() => setAuditPage(value => value + 1)}>Next audit records</Button>
      </>}
    </Card>}
  </div>;
}
