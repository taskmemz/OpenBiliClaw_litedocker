export const LINUXDO_TASK_TAB_PARAM = "openbiliclaw_linuxdo_task";
// Keep the marker in the query string. Discourse can consume/clear a hash
// during SPA bootstrap before this document_idle content script starts,
// which would make an automated runner look like a normal browsing tab.
export const LINUXDO_TASK_TAB_URL = `https://linux.do/?${LINUXDO_TASK_TAB_PARAM}=1`;

export interface LinuxdoLocationLike {
  href?: string;
  hash?: string;
  search?: string;
}

function hasTaskParam(value: string | undefined): boolean {
  if (!value) return false;
  const normalized = value.startsWith("#") || value.startsWith("?") ? value.slice(1) : value;
  return new URLSearchParams(normalized).has(LINUXDO_TASK_TAB_PARAM);
}

export function isLinuxdoTaskTabLocation(
  locationLike: LinuxdoLocationLike | undefined = globalThis.location,
): boolean {
  return (
    hasTaskParam(locationLike?.hash) ||
    hasTaskParam(locationLike?.search) ||
    String(locationLike?.href ?? "").includes(`${LINUXDO_TASK_TAB_PARAM}=1`)
  );
}
