import { useEffect, useState } from "react";
import { auth } from "../api/client";
import type { Me, Role } from "../api/types";

/** Loads /api/me (also validates the token: a 401 sends the user to login). */
export function useMe(role: Role): Me | null {
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => {
    let alive = true;
    auth.me(role).then(
      (m) => alive && setMe(m),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, [role]);
  return me;
}
