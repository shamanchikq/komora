import type { BranchDeal, DealsOutcome, TrackedDeal } from "../types";
import { uah } from "../format";
import { isNotice, noticeFirm, noticeText, warningText } from "../copy";
import { packSize, tileCode } from "./DraftScreen";

/** «Акції» — Plan 4 Task 3, built from the habits screen's pattern: the draft's `.row`
 * card seen as a list of prices rather than of quantities, and the same two doors
 * (compose, `?startapp=deals`).
 *
 * Every sentence arrives from the backend as text — the per-product claim, the two
 * empty states, the promos note and the footer — so this file restates no deals rule.
 * What it decides is layout, and three small things the chat also decides and would
 * otherwise be said twice: what a degraded list is allowed to look like, how a price
 * pair reads, and what «перевірено» means in the phone's clock. */

export const DEALS_TITLE = "Акції";
export const BUILD_DEALS_BUTTON = "Зібрати кошик зі знижок";
export const ADD_BUTTON = "Додати";

export const MINE_TITLE = "Ваші звичні покупки";
export const BRANCH_TITLE = "Найбільші знижки в магазині";
export const COUPONS_TITLE = "Ваші купони";
export const PROMOS_TITLE = "Персональні пропозиції";

/** `degraded:branch`, the one warning that changes what the rest of the screen may
 * say. Mirrors `render.render_deals`. */
const DEGRADED_BRANCH = "degraded:branch";

/** What to write under «Найбільші знижки в магазині» when the list is empty.
 *
 * `null` means write nothing: an empty list because Silpo did not answer is not «no
 * deals», and the warning above already says which it was. The chat makes exactly this
 * distinction (`elif "degraded:branch" not in outcome.warnings`) and it is the repo's
 * never-claim-more-than-the-data-supports rule in its smallest form — an empty section
 * under a failed call reads as a fact about prices, and it is a fact about the network.
 */
export function branchEmptyText(outcome: DealsOutcome): string | null {
  if (outcome.branch.length > 0) return null;
  return outcome.warnings.includes(DEGRADED_BRANCH) ? null : outcome.empty_branch_text;
}

/** «39,99 ₴ замість 60,99 ₴ (−34 %)», with «/кг» on both prices of a weighted good.
 *
 * Mirrors the branch row in `render.render_deals`. A weighted product's price is per
 * kilogram on both sides of «замість», so the unit goes on both: putting it on neither
 * is the «0,15 × 999,00 ₴» defect from 2026-09-05, and putting it on one would read as
 * a comparison between two different things.
 */
export function branchPriceText(deal: BranchDeal): string {
  const per = deal.weighted ? "/кг" : "";
  return `${uah(deal.price)}${per} замість ${uah(deal.old_price)}${per} (−${deal.percent_off} %)`;
}

/** «Ціни перевірено 14.09 о 10:20» — Kyiv time, because that is the clock the prices
 * were read on and the one the chat prints.
 *
 * The phone's own time zone is not it: a household travelling would be told its prices
 * were checked at an hour nothing happened. `Intl` owns the conversion; a returned
 * `null` (no scan yet, or a timestamp that does not parse) means the line is not drawn
 * at all rather than drawn around a hole.
 */
export function pricesCheckedAt(iso: string | null): string | null {
  if (iso === null) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  const parts = new Intl.DateTimeFormat("uk-UA", {
    timeZone: "Europe/Kyiv",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    // Explicit, not `hour12: false`: that spells midnight «24:05» under some ICU
    // builds, and the chat's `%H:%M` never does.
    hourCycle: "h23",
  }).formatToParts(at);
  const of = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((part) => part.type === type)?.value ?? "";
  // Assembled rather than taken as formatted: locale output puts the date and the time
  // together in its own way, and the sentence around them is Ukrainian prose, not a
  // locale's idea of one.
  return `Ціни перевірено ${of("day")}.${of("month")} о ${of("hour")}:${of("minute")}`;
}

function TrackedRow({ deal }: { deal: TrackedDeal }) {
  return (
    <li className="row">
      <div className="tile" aria-hidden>
        {tileCode(deal.name)}
      </div>
      <div className="row-main">
        <div className="row-name">
          <span className="name">{deal.name}</span>
        </div>
        {/* The backend's own sentence, verbatim: it is the only thing that knows
            whether there is enough shelf history to say «нижче за звичайну». */}
        <div className="reason due">
          <i />
          <span>{deal.sentence}</span>
        </div>
      </div>
    </li>
  );
}

function BranchRow({
  deal,
  busy,
  onAdd,
}: {
  deal: BranchDeal;
  busy: boolean;
  onAdd: () => void;
}) {
  const size = packSize(deal);
  return (
    <li className="row">
      <div className="tile" aria-hidden>
        {tileCode(deal.name)}
      </div>
      <div className="row-main">
        <div className="row-name">
          <span className="name">{deal.name}</span>
          <button
            className="mute-toggle"
            disabled={busy}
            aria-label={`${ADD_BUTTON}: ${deal.name}`}
            onClick={onAdd}
          >
            {ADD_BUTTON}
          </button>
        </div>
        {size !== "" && <div className="meta-line mono">{size}</div>}
        <div className="meta-line mono">
          <span>{branchPriceText(deal)}</span>
        </div>
      </div>
    </li>
  );
}

function TextGroup({ title, items, note }: { title: string; items: string[]; note?: string }) {
  if (items.length === 0) return null;
  return (
    <>
      <h2 className="group-title">{title}</h2>
      <ul className="plain-list">
        {items.map((text, i) => (
          <li key={i}>{text}</li>
        ))}
      </ul>
      {note !== undefined && <p className="hint">{note}</p>}
    </>
  );
}

export function DealsScreen({
  outcome,
  busy,
  onAdd,
}: {
  outcome: DealsOutcome;
  busy: boolean;
  onAdd: (deal: BranchDeal) => void;
}) {
  // The backend's sentence when it sends one; the local formatter is the fallback for
  // a payload from before `scanned_text` existed.
  const checked = outcome.scanned_text ?? pricesCheckedAt(outcome.scanned_at);
  const branchEmpty = branchEmptyText(outcome);
  // Same split as the draft screen: a degraded mode is a notice, anything else the
  // backend put in the list still has to be seen, verbatim if need be.
  const notices = outcome.warnings.filter(isNotice);
  const rest = outcome.warnings.filter((code) => !isNotice(code));

  return (
    <section className="screen">
      <h1>{DEALS_TITLE}</h1>

      {notices.map((code) => (
        <div key={code} className={noticeFirm(code) ? "notice firm" : "notice quiet"}>
          {noticeText(code)}
        </div>
      ))}
      {rest.map((code) => (
        <div key={code} className="notice quiet">
          {warningText(code)}
        </div>
      ))}

      <h2 className="group-title">{MINE_TITLE}</h2>
      {outcome.mine.length > 0 ? (
        <ul className="lines">
          {outcome.mine.map((deal) => (
            <TrackedRow key={deal.product_key} deal={deal} />
          ))}
        </ul>
      ) : (
        <p className="empty">{outcome.empty_mine_text}</p>
      )}
      {checked !== null && <p className="hint">{checked}</p>}

      <h2 className="group-title">{BRANCH_TITLE}</h2>
      {outcome.branch.length > 0 && (
        <ul className="lines">
          {outcome.branch.map((deal) => (
            <BranchRow
              key={deal.product_id}
              deal={deal}
              busy={busy}
              onAdd={() => onAdd(deal)}
            />
          ))}
        </ul>
      )}
      {branchEmpty !== null && <p className="empty">{branchEmpty}</p>}

      <TextGroup title={COUPONS_TITLE} items={outcome.coupons} />
      <TextGroup title={PROMOS_TITLE} items={outcome.promos} note={outcome.promos_note} />

      <footer className="trust">{outcome.trust_text}</footer>
    </section>
  );
}
