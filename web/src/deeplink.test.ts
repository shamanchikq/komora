import { describe, expect, it } from "vitest";
import { parseTarget } from "./deeplink";

describe("parseTarget", () => {
  it("reads the basket a link names", () => {
    expect(parseTarget("basket_42")).toEqual({ kind: "basket", id: 42 });
  });

  it("tolerates the whitespace a copied link can carry", () => {
    expect(parseTarget(" basket_7 ")).toEqual({ kind: "basket", id: 7 });
  });

  it("opens on compose rather than erroring when there is no link", () => {
    // Both of the ordinary launches: the menu button, and a browser during development.
    expect(parseTarget(undefined)).toBeNull();
    expect(parseTarget("")).toBeNull();
  });

  it("refuses a payload it does not recognise", () => {
    // A future kind must not be read as this one. Nothing here decides access — the
    // route the id reaches re-derives ownership — but a wrong screen is still wrong.
    for (const param of ["deal_42", "basket", "basket_", "basket_abc", "basket_-1", "42"]) {
      expect(parseTarget(param), param).toBeNull();
    }
  });

  it("refuses an id no basket could have", () => {
    expect(parseTarget("basket_0")).toBeNull();
    expect(parseTarget(`basket_${"9".repeat(40)}`)).toBeNull();
  });
});
