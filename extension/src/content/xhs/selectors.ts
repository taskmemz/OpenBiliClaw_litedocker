/**
 * Single source of truth for the xhs note-card DOM selectors.
 *
 * These strings were previously duplicated byte-for-byte in `passive.ts`
 * (in-viewport passive collector) and `bootstrap.ts` (`__INITIAL_STATE__`
 * fallback anchor scrape). xhs ships an unstable, frequently-restructured
 * DOM, so a selector tweak has to happen in exactly one place — keeping two
 * copies in sync by hand is how they drift.
 *
 * Pure move: these are the exact strings both files already used; extracting
 * them changes no matching behavior.
 */

/**
 * Anchors that point at a note detail page.
 *
 * Xiaohongshu search started emitting ``/search_result/{note_id}`` card links
 * in the 2026-07 rollout. Keep this aligned with the platform adapter's note
 * identity routes; the query page itself is ``/search_result?keyword=...`` and
 * therefore cannot match the trailing-slash selector below.
 */
export const NOTE_ANCHOR_SELECTOR = [
  'a[href*="/explore/"]',
  'a[href*="/discovery/item/"]',
  'a[href*="/search_result/"]',
].join(", ");

/** Card container an anchor sits inside — walked up from the `<a>` via `closest`. */
export const NOTE_CARD_CONTAINER_SELECTOR =
  ".note-item, section, [class*='note'], [class*='card']";

/** Note title element within a card. */
export const NOTE_TITLE_SELECTOR = ".title, .note-title, [class*='title'] span, [class*='title']";

/** Author / nickname element within a card. */
export const NOTE_AUTHOR_SELECTOR =
  ".author-wrapper .name, .author .name, .user-name, [class*='author'] .name, .nickname";

/** Cover image within a card (falls back to the first `<img>`). */
export const NOTE_COVER_SELECTOR =
  "img.cover, .cover img, img[src*='xhscdn'], img[src*='sns-img'], img";
