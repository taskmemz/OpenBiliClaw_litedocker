export const V2EX_TASK_TAB_PARAM = "openbiliclaw_v2ex_task";
export const V2EX_TASK_TAB_URL = `https://www.v2ex.com/#${V2EX_TASK_TAB_PARAM}=1`;

export interface LocationLike {
  hash?: string;
  search?: string;
}

function hasTaskParam(value: string | undefined): boolean {
  if (!value) return false;
  const normalized = value.startsWith("#") || value.startsWith("?") ? value.slice(1) : value;
  return new URLSearchParams(normalized).has(V2EX_TASK_TAB_PARAM);
}

export function isV2EXTaskTabLocation(
  locationLike: LocationLike | undefined = globalThis.location,
): boolean {
  return hasTaskParam(locationLike?.hash) || hasTaskParam(locationLike?.search);
}
