const LOCAL_SUPERVISOR_DEFAULT = "http://127.0.0.1:8000";
const REMOTE_SUPERVISOR_DEFAULT = "http://129.211.23.161/supervisor";

export type BackendTarget = "local" | "remote";

export function backendTarget(): BackendTarget {
  const target = (process.env.BACKEND_TARGET || "local").trim().toLowerCase();
  if (target === "local" || target === "remote") return target;
  throw new Error("BACKEND_TARGET 只能配置为 local 或 remote");
}

export function supervisorApiUrl(): string {
  const target = backendTarget();
  const configuredUrl = target === "remote"
    ? process.env.REMOTE_SUPERVISOR_API_URL
    : process.env.LOCAL_SUPERVISOR_API_URL;
  const defaultUrl = target === "remote"
    ? REMOTE_SUPERVISOR_DEFAULT
    : LOCAL_SUPERVISOR_DEFAULT;
  return (configuredUrl || defaultUrl).replace(/\/+$/, "");
}
