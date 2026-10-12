import { request } from "./portfolio-api";

export type AdminUser = {
  userId: string; email: string; displayName: string; status: string | null;
  emailVerifiedAt: string | null; createdAt: string; lastLoginAt: string | null; roles: string[];
};
export type RoleAudit = {
  id: string; userId: string; targetEmail: string; role: string; action: string;
  operator: string; reason: string | null; createdAt: string;
};
export type AdminPage<T> = { content: T[]; page: number; size: number; totalElements: number; totalPages: number };
const base = "/api/v1/auth/admin/users";
export const adminApi = {
  users: (search: string, page: number) => request<AdminPage<AdminUser>>(`${base}?search=${encodeURIComponent(search)}&page=${page}&size=20`),
  user: (id: string) => request<AdminUser>(`${base}/${id}`),
  audit: (id: string, page: number) => request<AdminPage<RoleAudit>>(`${base}/${id}/audit?page=${page}&size=20`),
  change: (id: string, grant: boolean, reason: string) => request<void>(`${base}/${id}/roles/ADMIN`, {
    method: grant ? "PUT" : "DELETE", body: JSON.stringify({ reason })
  })
};
