import { useEffect, useState } from "react";

// Hash routes (#/cases/123) need no server rewrites and no router dependency.
export function useRoute(): string[] {
  const read = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const [parts, setParts] = useState(read);
  useEffect(() => {
    const update = () => setParts(read());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return parts;
}

export const go = (path: string) => (window.location.hash = path);
