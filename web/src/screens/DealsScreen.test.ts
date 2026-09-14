import { describe, expect, it } from "vitest";
import type { BranchDeal, DealsOutcome } from "../types";
import { branchEmptyText, branchPriceText, pricesCheckedAt } from "./DealsScreen";

function branchDeal(over: Partial<BranchDeal> = {}): BranchDeal {
  return {
    product_id: "42",
    name: "Сир Моцарела",
    price: "39.99",
    old_price: "60.99",
    percent_off: 34,
    weighted: false,
    display_ratio: "125г",
    external_product_id: 123456,
    ...over,
  };
}

function deals(over: Partial<DealsOutcome> = {}): DealsOutcome {
  return {
    kind: "deals",
    mine: [],
    branch: [],
    coupons: [],
    promos: [],
    scanned_at: null,
    scanned_text: null,
    warnings: [],
    empty_mine_text: "Серед ваших звичних покупок зараз нічого не подешевшало.",
    empty_branch_text: "Знижок у цьому магазині зараз не видно.",
    promos_note: "Активувати персональну пропозицію можна лише в застосунку Сільпо.",
    trust_text: "Знижка — це різниця між старою і теперішньою ціною Сільпо.",
    toast: null,
    ...over,
  };
}

describe("branchEmptyText", () => {
  /** The rule is the repo's own, in its smallest form: an empty list because Silpo did
   * not answer is a fact about the network, and «Знижок зараз не видно» states it as a
   * fact about prices. Mirrors `render.render_deals`, which suppresses the same
   * sentence under the same warning. */
  it("says there are no deals only when Silpo actually answered", () => {
    expect(branchEmptyText(deals())).toBe(deals().empty_branch_text);
  });

  it("stays silent when the branch list is the part that failed", () => {
    expect(branchEmptyText(deals({ warnings: ["degraded:branch"] }))).toBeNull();
    // A different degraded part says nothing about the branch list, so the empty
    // sentence is still the true one.
    expect(branchEmptyText(deals({ warnings: ["degraded:promos"] }))).not.toBeNull();
  });

  it("writes nothing at all when there is a list to draw", () => {
    expect(branchEmptyText(deals({ branch: [branchDeal()] }))).toBeNull();
    expect(
      branchEmptyText(deals({ branch: [branchDeal()], warnings: ["degraded:branch"] })),
    ).toBeNull();
  });
});

describe("branchPriceText", () => {
  it("writes the pair the chat writes", () => {
    expect(branchPriceText(branchDeal())).toBe("39,99 ₴ замість 60,99 ₴ (−34 %)");
  });

  it("puts the kilogram on both sides of «замість», never on one", () => {
    // A weighted product is priced per kilo on both sides. The 2026-09-05 defect was
    // this unit missing entirely — a price read as a piece price; naming it on one
    // side only would compare two different things.
    expect(branchPriceText(branchDeal({ weighted: true, price: "199.00", old_price: "249.00", percent_off: 20 }))).toBe(
      "199,00 ₴/кг замість 249,00 ₴/кг (−20 %)",
    );
  });

  it("rounds money the way both surfaces round it", () => {
    // `uah` half-up, as `core/money.py` does — a price that arrives with three digits
    // must not disagree with the chat by a cent.
    expect(branchPriceText(branchDeal({ price: "4.9975", old_price: "9.994" }))).toContain(
      "5,00 ₴ замість 9,99 ₴",
    );
  });
});

describe("pricesCheckedAt", () => {
  /** The clock the prices were read on is Kyiv's, and it is what the chat prints. The
   * phone's own zone would tell a household abroad its prices were checked at an hour
   * nothing happened. */
  it("reads a UTC timestamp in Kyiv time", () => {
    // 07:20Z is 10:20 in Kyiv in September (UTC+3).
    expect(pricesCheckedAt("2026-09-14T07:20:00+00:00")).toBe("Ціни перевірено 14.09 о 10:20");
  });

  it("keeps the two-digit shape a date can lose", () => {
    expect(pricesCheckedAt("2026-01-05T22:05:00+00:00")).toBe("Ціни перевірено 06.01 о 00:05");
  });

  it("draws no line rather than a line around a hole", () => {
    expect(pricesCheckedAt(null)).toBeNull();
    expect(pricesCheckedAt("не дата")).toBeNull();
  });
});
