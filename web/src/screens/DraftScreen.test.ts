import { describe, expect, it } from "vitest";
import type { Line } from "../types";
import { packSize, qtyLabel } from "./DraftScreen";

function line(overrides: Partial<Line>): Line {
  return {
    product_id: "p",
    company_id: "c",
    branch_id: "b",
    description: "картопля",
    category: null,
    name: "Картопля",
    qty: 1,
    unit: "",
    unit_price: "30.00",
    old_price: null,
    line_total: "30.00",
    reason_kind: "stated",
    reason_text: "просили",
    substituted_from: null,
    optional: false,
    unavailable: false,
    weighted: false,
    step: null,
    stock: null,
    synced: false,
    display_ratio: null,
    display_price: null,
    special_prices: [],
    ...overrides,
  };
}

describe("qtyLabel", () => {
  it("writes a whole number of kilos as itself", () => {
    // `"step": 1` is what the captured fixtures hold, and deriving the precision from
    // it gave zero decimals — after which stripping trailing zeros ate the number:
    // ten kilos of potatoes read «1 кг» beside a row sum for ten.
    expect(qtyLabel(line({ weighted: true, step: 1, qty: 10 }))).toBe("10\u00A0кг");
    expect(qtyLabel(line({ weighted: true, step: 1, qty: 20 }))).toBe("20\u00A0кг");
  });

  it("keeps the decimals a weight actually has", () => {
    expect(qtyLabel(line({ weighted: true, step: 0.25, qty: 0.25 }))).toBe("0,25\u00A0кг");
    expect(qtyLabel(line({ weighted: true, step: 0.1, qty: 1.5 }))).toBe("1,5\u00A0кг");
    expect(qtyLabel(line({ weighted: true, step: null, qty: 0.1 }))).toBe("0,1\u00A0кг");
  });

  it("counts pieces without a unit", () => {
    expect(qtyLabel(line({ qty: 3 }))).toBe("3");
  });
});

describe("packSize", () => {
  /** Mirrors `render.line_text`. `displayRatio` is the pack Silpo prints on the shelf
   * label; `unit` is whatever the August-shaped hit happened to carry, which for a
   * search result was almost always nothing. */
  it("prefers the pack size a search hit now carries", () => {
    expect(packSize(line({ display_ratio: "900г", unit: "шт" }))).toBe("900г");
    expect(packSize(line({ display_ratio: "0,5л", unit: "" }))).toBe("0,5л");
  });

  it("falls back to the unit, and to saying nothing", () => {
    expect(packSize(line({ display_ratio: null, unit: "10 шт" }))).toBe("10 шт");
    expect(packSize(line({ display_ratio: null, unit: "" }))).toBe("");
    // An empty string from Silpo is not a size; the row guarded `unit` this way
    // before `display_ratio` existed and must keep guarding both.
    expect(packSize(line({ display_ratio: "", unit: "" }))).toBe("");
    expect(packSize(line({ display_ratio: "  ", unit: "" }))).toBe("");
  });

  it("never calls a weighted good's pricing unit a pack size", () => {
    // «100г» on a weighted product is the denominator of its price, not the content of
    // a package — the row writes «₴/кг» there and must not also claim a 100 g pack.
    expect(packSize(line({ weighted: true, display_ratio: "100г", unit: "" }))).toBe("");
    expect(packSize(line({ weighted: true, display_ratio: "100г", unit: "кг" }))).toBe("кг");
  });
});
