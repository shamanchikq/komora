import { describe, expect, it } from "vitest";
import type { Line } from "../types";
import { qtyLabel } from "./DraftScreen";

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
