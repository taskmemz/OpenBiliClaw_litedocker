export const WEIBO_TASK_TAB_PARAM = "openbiliclaw_weibo_task";
// Query markers survive the H5 site's hash/router rewrites.  Keep accepting
// the old hash form in ``isWeiboTaskTabLocation`` so an already-open task tab
// can finish safely across an extension update.
export const WEIBO_TASK_TAB_URL = `https://m.weibo.cn/?${WEIBO_TASK_TAB_PARAM}=1`;

export interface LocationLike {
  href?: string;
  hash?: string;
  search?: string;
}

export function isWeiboTaskTabLocation(
  locationLike: LocationLike | undefined = globalThis.location,
): boolean {
  const text = `${locationLike?.href ?? ""} ${locationLike?.hash ?? ""} ${locationLike?.search ?? ""}`;
  return text.includes(`${WEIBO_TASK_TAB_PARAM}=1`);
}
