/** Where a launch link says to open.
 *
 * Telegram hands `?startapp=<payload>` to the app as `start_param`. The payload is
 * `<kind>_<value>`, or a bare kind where there is no value to carry — a nudge that
 * wants to open a deal or a habit adds a kind rather than a second parser (spec §8,
 * where deep links carry push notifications onto their target screen).
 *
 * A parsed target is a *request*, not a permission. The id inside it was chosen by
 * whoever opened the link, so the backend re-derives ownership on the route it hits —
 * `handlers._own_draft`, the same gate a tap in the chat goes through.
 */

import { webApp } from "./telegram";

/** `usual` and `deals` carry no value: both screens are the sender's own, looked up by
 * who opened the app — there is no id to name, so none to guess. */
export type Target =
  | { kind: "basket"; id: number }
  | { kind: "usual" }
  | { kind: "deals" };

const USUAL = "usual";
/** Mirrors `render.DEALS_START`. */
const DEALS = "deals";

export function parseTarget(param: string | null | undefined): Target | null {
  const trimmed = (param ?? "").trim();
  if (trimmed === USUAL) return { kind: "usual" };
  if (trimmed === DEALS) return { kind: "deals" };
  const match = /^basket_(\d+)$/.exec(trimmed);
  if (match === null) return null;
  const id = Number(match[1]);
  // A malformed id is not an error worth showing: the app opens on compose, which is
  // where it would have opened without a link at all.
  return Number.isSafeInteger(id) && id > 0 ? { kind: "basket", id } : null;
}

export function launchTarget(): Target | null {
  return parseTarget(webApp()?.initDataUnsafe?.start_param);
}
