import { describe, expect, it } from "vitest";
import {
  budgetShown,
  isNotice,
  noticeFirm,
  noticeText,
  validationText,
  warningText,
} from "./copy";

/** Every warning code the backend can put in a cart. Keep this list beside the
 * constants that produce them: `promos.DEGRADED_COUPONS`, `resolve.DEGRADED_REPLACEMENTS`,
 * `verify.DEGRADED_VERIFY`, `pipeline.TIMESLOT_EXPIRED`, plus the two parameterised
 * ones built in `passes/`. */
const EMITTED = [
  "degraded:coupons",
  "degraded:replacements",
  "degraded:verification",
  "timeslot:expired",
  "over_budget:120.50",
  "not_found:Ікра (чорна)",
];

describe("warning codes", () => {
  it.each(EMITTED)("turns %s into prose", (code) => {
    // A stack overflow throws RangeError, so this is the whole regression: the
    // notice table was keyed by full code and read by kind, every `degraded:*` missed,
    // and the miss fell through to `warningText`, which handed it straight back.
    // The recursion threw during render, which in React means a blank screen.
    expect(() => noticeText(code)).not.toThrow();
    expect(noticeText(code)).not.toBe(code);
    expect(noticeText(code).length).toBeGreaterThan(0);
  });

  it("names each degraded mode rather than printing its code", () => {
    expect(noticeText("degraded:coupons")).toContain("Купони");
    expect(noticeText("degraded:replacements")).toContain("заміни");
    expect(noticeText("degraded:verification")).toContain("перевірити");
  });

  it("reads firm only where the user has something to do", () => {
    expect(noticeFirm("degraded:verification")).toBe(true);
    expect(noticeFirm("timeslot:expired")).toBe(true);
    expect(noticeFirm("degraded:coupons")).toBe(false);
    expect(noticeFirm("degraded:replacements")).toBe(false);
  });

  it("says the timeslot the same way through either door", () => {
    // The prose used to be written out twice, once per function.
    expect(warningText("timeslot:expired")).toBe(noticeText("timeslot:expired"));
    expect(noticeText("timeslot:expired")).toContain("Час доставки");
  });

  it("fills in the parameterised codes", () => {
    expect(warningText("not_found:Ікра (чорна)")).toBe("Не знайшлося: «Ікра (чорна)»");
    expect(warningText("over_budget:120.5")).toBe("Понад тижневий бюджет на 120,50 ₴");
  });

  it("shows an unknown code verbatim instead of swallowing it", () => {
    // Including one that merely looks familiar: a new `degraded:*` from the backend
    // must reach the screen as itself, not as a crash and not as silence.
    expect(noticeText("degraded:something-new")).toBe("degraded:something-new");
    expect(warningText("mystery")).toBe("mystery");
  });

  it("separates notices from the not-found list", () => {
    expect(isNotice("degraded:coupons")).toBe(true);
    expect(isNotice("timeslot:expired")).toBe(true);
    expect(isNotice("over_budget:10")).toBe(true);
    expect(isNotice("not_found:хліб")).toBe(false);
  });
});

describe("validations", () => {
  it("translates the codes seen live", () => {
    expect(validationText("order.cost.min")).toContain("менша за мінімальну");
    expect(validationText(" product.offer.stock.max ")).toContain("залишилось");
  });

  it("quotes an unknown code rather than hiding the obstacle", () => {
    expect(validationText("some.new.code")).toContain("some.new.code");
  });
});

describe("budgetShown", () => {
  /** `over_budget:` and the budget caption are the same sentence, and the caption is
   * drawn whenever a cap exists — which is the only time the warning is emitted. Both
   * were rendered, one above the other, about the same cart. Mirrors the backend's
   * `render.budget_shown`; the two must agree or the surfaces diverge again. */
  it("drops the overage warning wherever the caption states it", () => {
    expect(budgetShown(["over_budget:84.30", "degraded:coupons"], 1500)).toEqual([
      "degraded:coupons",
    ]);
  });

  it("keeps it when no cap is drawn, because then nothing else says it", () => {
    expect(budgetShown(["over_budget:84.30"], null)).toEqual(["over_budget:84.30"]);
  });

  it("leaves every other code alone", () => {
    const other = ["not_found:хліб", "timeslot:expired", "degraded:verification"];
    expect(budgetShown(other, 1500)).toEqual(other);
  });
});
