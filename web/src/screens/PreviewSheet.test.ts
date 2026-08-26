import { describe, expect, it } from "vitest";
import type { Preview } from "../types";
import { confirmLabel, hasChanges } from "./PreviewSheet";

function preview(overrides: Partial<Preview> = {}): Preview {
  return {
    existing_count: 0,
    existing_total: "0",
    adding_count: 0,
    adding_total: "0",
    overlapping: [],
    now_unavailable: [],
    removing: [],
    blocking_validations: [],
    drift: null,
    ...overrides,
  };
}

/** Both figures are read back from the live cart at confirmation time, so the sheet
 * can arrive with less to do than the draft said — or with nothing at all. */
describe("hasChanges", () => {
  it("is false for a sheet that asks for nothing", () => {
    // «Прибери хліб» is a whole basket: no lines, one removal. Take the bread out by
    // hand in the Silpo app first and the sheet has nothing left — it used to count
    // to zero and offer «Додати 0 позицій», over a button saying the same.
    expect(hasChanges(preview())).toBe(false);
  });

  it("is true when there is anything to add or take out", () => {
    expect(hasChanges(preview({ adding_count: 2 }))).toBe(true);
    expect(hasChanges(preview({ removing: ["Хліб Київський"] }))).toBe(true);
  });

  it("counts a line already in the cart as something to do", () => {
    // Its quantity is being replaced, which is a change even though nothing is new.
    const overlap = preview({ adding_count: 1, overlapping: ["Молоко"] });
    expect(hasChanges(overlap)).toBe(true);
    expect(confirmLabel(overlap)).toBe("оновити 1");
  });
});

describe("confirmLabel", () => {
  it("never asks the user to confirm a zero", () => {
    for (const p of [
      preview({ adding_count: 3 }),
      preview({ removing: ["Хліб"] }),
      preview({ adding_count: 2, overlapping: ["Молоко"], removing: ["Хліб"] }),
    ]) {
      expect(confirmLabel(p)).not.toMatch(/\b0\b/);
    }
  });

  it("names every kind of change it is about to make", () => {
    expect(confirmLabel(preview({ adding_count: 3 }))).toBe("Додати 3 позиції");
    expect(confirmLabel(preview({ removing: ["Хліб"] }))).toBe("Прибрати 1 позицію");
    expect(
      confirmLabel(preview({ adding_count: 3, overlapping: ["Молоко"], removing: ["Хліб"] })),
    ).toBe("Додати 2 · оновити 1 · прибрати 1");
  });
});
