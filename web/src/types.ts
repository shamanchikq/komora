/** What `api.minapp.serialise` emits. Money is Decimal-as-text — keep it text all the
 * way to the pixel, or "42.9" becomes 42.900000000000004 on its way to «₴». */

export type Money = string;

export interface Line {
  product_id: string;
  company_id: string;
  branch_id: string;
  description: string;
  category: string | null;
  name: string;
  qty: number;
  unit: string;
  unit_price: Money;
  old_price: Money | null;
  /** Computed server-side (Decimal math stays in Python); part of every serialisation. */
  line_total: Money;
  reason_kind: "stated" | "habit" | "deal" | "meal" | "sub";
  reason_text: string;
  substituted_from: string | null;
  optional: boolean;
  unavailable: boolean;
  /** Priced per kilogram — `unit_price` is ₴/kg and `qty` is kg. A search hit carries
   * no size field, so this doubles as the only signal for the «0,15 кг × 999 ₴/кг»
   * row shape the design specifies. */
  weighted: boolean;
  /** Smallest orderable weight; the stepper moves in these. */
  step: number | null;
  /** Silpo's remaining stock — the stepper's ceiling. null means unknown. */
  stock: number | null;
  /** The content of one pack as Silpo writes it — «900г», «0,5л», «10 шт». Since
   * 2026-09-14 a search hit carries one, so the row's mono slot finally has a real
   * pack size instead of `unit`. `null` on a product read before that, or when Silpo
   * sent none. On a weighted good it is the pricing unit («100г»), not a pack — the
   * chat ignores it there and so does this surface (`packSize`). */
  display_ratio: string | null;
  /** Price per `display_ratio`. Not money the user pays — the deals screen's number,
   * never a line total. */
  display_price: Money | null;
  /** Silpo's multi-buy prices — «від 2 шт — по 128,52 ₴». A note the savings pass
   * writes, never applied to a total: whether the condition is met is Silpo's to
   * decide at checkout. Only `type: "from"` has ever been observed; anything else is
   * carried and not interpreted. */
  special_prices: { price: Money; count: number; type: string }[];
  /** Already in the real Silpo cart, because a push landed it. A push can land
   * partly, and a partly-landed basket stays a draft so it can be retried — so the
   * screen's «нічого не зміниться» is false for exactly these rows. */
  synced: boolean;
}

/** One dish of a meal plan (Plan 4 D5). Drawn above the basket, never a cart line:
 * a dish is not a product. */
export interface MenuItem {
  day: string;
  dish: string;
}

export interface Cart {
  lines: Line[];
  menu: MenuItem[];
  total: Money;
  estimated_savings: Money;
  savings_notes: string[];
  coupon_notes: string[];
  removals: { product_id: string; name: string }[];
  warnings: string[];
}

export interface Preview {
  existing_count: number;
  existing_total: Money;
  adding_count: number;
  adding_total: Money;
  overlapping: string[];
  now_unavailable: string[];
  removing: string[];
  blocking_validations: string[];
  drift: [Money, Money] | null;
}

export interface Report {
  ok: boolean;
  added: string[];
  failed: [string, string][];
  removed: string[];
  remove_failed: [string, string][];
  checkout_web_link: string | null;
  checkout_mobile_link: string | null;
  blocking_validations: string[];
}

/** Not an `Outcome` on the backend either: a lookup this surface makes on its way to
 * an edit, not a turn in the conversation. A chat keyboard cannot draw a row of
 * tappable product names, so the bot keeps its one-step «⇄» and this stays ours. */
export interface Alternatives {
  kind: "alternatives";
  basket_id: number;
  position: number;
  current: Line;
  /** Best first, current product excluded. Empty means Silpo offered none. */
  options: Line[];
}

/** One tracked habit, as `api.minapp._habit_json` sends it. `sentence` is the engine's
 * own claim, verbatim — the screen restates no cadence rule. */
export interface Habit {
  product_key: string;
  name: string;
  sentence: string;
  /** Due by the engine's date and not lapsed. */
  due: boolean;
  /** Overdue by more than two intervals: shown, never offered as «вже пора». */
  lapsed: boolean;
  due_on: string;
  last_bought: string;
  median_gap_days: number;
  events: number;
  muted: boolean;
  /** False for counter goods keyed by a till article: a cadence, never a cart line. */
  reorderable: boolean;
  weighted: boolean;
  unit: string;
}

export interface Habits {
  kind: "habits";
  habits: Habit[];
  fresh_at: string | null;
  /** «Історія оновлена 14.09 о 10:20» — the chat's sentence, not re-derived here. */
  fresh_text: string;
  /** What the chat says when nothing passes the threshold. */
  empty_text: string;
  toast: string | null;
}

/** A tracked habit whose own product is cheaper than its own old price — the only
 * thing Plan 4 calls a deal (D2). `sentence` is the backend's claim, verbatim: the
 * comparison rule, including whether there is enough shelf history to say «нижче за
 * звичайну», lives in `render.deal_sentence` and nowhere else. */
export interface TrackedDeal {
  product_key: string;
  name: string;
  price: Money;
  old_price: Money | null;
  percent_off: number;
  /** Percent below the median shelf price at this branch, once four snapshots exist
   * to compute one (D3). `null` means no baseline — not «no difference». */
  below_usual: number | null;
  sentence: string;
  weighted: boolean;
  unit: string;
}

/** One discounted product from the branch-wide browse. Ranked by discount on the
 * backend — Silpo's own list order sorts by `displayPrice` and mixes per-100 g prices
 * with per-piece ones, so it is never a ranking. */
export interface BranchDeal {
  product_id: string;
  name: string;
  price: Money;
  old_price: Money;
  percent_off: number;
  weighted: boolean;
  display_ratio: string | null;
  external_product_id: number | null;
}

export type Outcome =
  | Alternatives
  | Habits
  | {
      kind: "draft";
      basket_id: number | null;
      title: string;
      budget_cap: number | null;
      cart: Cart;
      toast: string | null;
    }
  | { kind: "preview"; basket_id: number; preview: Preview }
  | { kind: "synced"; basket_id: number; report: Report }
  | {
      kind: "deals";
      mine: TrackedDeal[];
      branch: BranchDeal[];
      /** Prose about the account, never about a product in the cart. */
      coupons: string[];
      promos: string[];
      scanned_at: string | null;
      /** The chat's own freshness sentence for `scanned_at`, so the screen formats no
       * timestamp of its own; null when the tracked products were never re-priced. */
      scanned_text: string | null;
      /** `degraded:branch`, `degraded:coupons`, `degraded:promos` — a part Silpo did
       * not answer. An empty list under one of these is not «no deals». */
      warnings: string[];
      empty_mine_text: string;
      empty_branch_text: string;
      promos_note: string;
      trust_text: string;
      toast: string | null;
    }
  /** The proactive alert (J6). No route this surface calls can answer with it — it is
   * pushed into the chat — but `serialise` can emit it, and an outcome the union does
   * not know about is one an exhaustive switch cannot be trusted to have covered. */
  | { kind: "deal"; deals: TrackedDeal[] }
  | { kind: "spoke"; text: string; needs_link: boolean; toast: string | null };

export type AlternativesOutcome = Extract<Outcome, { kind: "alternatives" }>;
export type DraftOutcome = Extract<Outcome, { kind: "draft" }>;
export type PreviewOutcome = Extract<Outcome, { kind: "preview" }>;
export type SyncedOutcome = Extract<Outcome, { kind: "synced" }>;
export type SpokeOutcome = Extract<Outcome, { kind: "spoke" }>;
export type HabitsOutcome = Extract<Outcome, { kind: "habits" }>;
export type DealsOutcome = Extract<Outcome, { kind: "deals" }>;
