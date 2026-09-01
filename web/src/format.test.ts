import { describe, expect, it } from "vitest";
import { items, itemsAcc, pl, quantityText, uah, uiInt } from "./format";

describe("uah", () => {
  /** The table is the point: every expectation is what `core/money.py: uah` prints
   * for the same string, checked against it. Money reaches this file with whatever
   * scale Decimal arithmetic left on it — a weighted line is `unit_price × qty`, so
   * three and four decimals are ordinary — and the two surfaces show the same cart. */
  it.each([
    ["0", "0,00 ₴"],
    ["42.9", "42,90 ₴"],
    ["243.000", "243,00 ₴"],
    ["149.8500", "149,85 ₴"],
    ["15.000", "15,00 ₴"],
    ["4.9975", "5,00 ₴"],
    ["69.9965", "70,00 ₴"],
    ["0.005", "0,01 ₴"],
    ["9.999", "10,00 ₴"],
    ["-3.999", "-4,00 ₴"],
  ])("writes %s as %s, the way core/money.py does", (raw, expected) => {
    expect(uah(raw)).toBe(expected);
  });

  it("rounds the half up rather than to the nearest even, and never through a float", () => {
    // `Math.round(1.005 * 100)` is 100: the product is 100.49999999999999. Decimal
    // says 1.01, so this must too.
    expect(uah("1.005")).toBe("1,01 ₴");
  });

  it("rounds on the third digit alone, whatever follows it", () => {
    expect(uah("4.99499999")).toBe("4,99 ₴");
    expect(uah("4.99500001")).toBe("5,00 ₴");
  });

  it("shows an unparseable amount as it arrived instead of throwing", () => {
    expect(uah("—")).toBe("— ₴");
  });
});

describe("quantityText", () => {
  it.each([
    [10, "10"],
    [100, "100"],
    [2, "2"],
    [1.5, "1,5"],
    [0.25, "0,25"],
    [0.1, "0,1"],
  ])("writes %s as %s", (qty, expected) => {
    expect(quantityText(qty)).toBe(expected);
  });

  it("keeps the zeros that are part of the number", () => {
    // The trailing-zero strip is for «0,500» -> «0,5»; it must not reach the tens.
    expect(quantityText(20)).toBe("20");
  });
});

describe("Ukrainian plurals", () => {
  it.each([
    [1, "1 позиція"],
    [2, "2 позиції"],
    [5, "5 позицій"],
    [11, "11 позицій"],
    [21, "21 позиція"],
  ])("counts %i as %s", (n, expected) => {
    expect(items(n)).toBe(expected);
  });

  it("declines the accusative for button labels", () => {
    expect(itemsAcc(1)).toBe("1 позицію");
    expect(itemsAcc(3)).toBe("3 позиції");
    expect(itemsAcc(5)).toBe("5 позицій");
  });

  it("is available to any noun, so no screen writes its own", () => {
    expect(pl(21, "one", "few", "many")).toBe("one");
    expect(pl(14, "one", "few", "many")).toBe("many");
  });
});

describe("uiInt", () => {
  it("groups thousands with a thin space", () => {
    expect(uiInt(2000)).toBe("2\u2009000");
    expect(uiInt(999)).toBe("999");
  });
});
