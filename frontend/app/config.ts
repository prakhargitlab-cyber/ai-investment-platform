export const frontendConfig = {
  // A relative URL lets one immutable image use the gateway origin in every
  // environment. Local `next dev` keeps its explicit URL in .env.example.
  apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "",
  authDevLoginEnabled: process.env.NEXT_PUBLIC_AUTH_DEV_LOGIN_ENABLED === "true"
};
