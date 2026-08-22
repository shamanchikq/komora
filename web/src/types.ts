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
}

export interface Cart {
  lines: Line[];
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

export type Outcome =
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
  | { kind: "spoke"; text: string; needs_link: boolean; toast: string | null };

export type DraftOutcome = Extract<Outcome, { kind: "draft" }>;
export type PreviewOutcome = Extract<Outcome, { kind: "preview" }>;
export type SyncedOutcome = Extract<Outcome, { kind: "synced" }>;
export type SpokeOutcome = Extract<Outcome, { kind: "spoke" }>;
