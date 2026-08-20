/** 统一所有前端代理传给总控的用户标识。 */
export function supervisorUserId(request: Request): string {
  return (
    request.headers.get("oai-authenticated-user-email")
    || request.headers.get("x-user-id")
    || process.env.FRONTEND_USER_ID
    || "frontend-user"
  );
}
