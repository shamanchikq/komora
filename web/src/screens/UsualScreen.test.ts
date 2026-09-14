import { describe, expect, it } from "vitest";
import type { Habit } from "../types";
import { canBuild, groupHabits } from "./UsualScreen";

function habit(key: string, over: Partial<Habit> = {}): Habit {
  return {
    product_key: key,
    name: key,
    sentence: `Ви купуєте «${key}» кожні ~7 днів, минуло 3 дні`,
    due: false,
    lapsed: false,
    due_on: "2026-09-14",
    last_bought: "2026-09-07",
    median_gap_days: 7,
    events: 6,
    muted: false,
    reorderable: true,
    weighted: false,
    unit: "900г",
    ...over,
  };
}

describe("groupHabits", () => {
  it("puts due first, keeps the backend's order inside a group, and muted last", () => {
    const list = [
      habit("a"),
      habit("b", { due: true }),
      habit("c", { muted: true }),
      habit("d", { due: true }),
    ];
    const groups = groupHabits(list);
    expect(groups.due.map((h) => h.product_key)).toEqual(["b", "d"]);
    expect(groups.tracked.map((h) => h.product_key)).toEqual(["a"]);
    expect(groups.muted.map((h) => h.product_key)).toEqual(["c"]);
  });

  it("never calls a muted habit due, whatever the flag says", () => {
    // The chat draws «вже пора» only on unmuted rows; a muted one that is due by date
    // belongs with the switched-off, not at the top of the screen.
    const groups = groupHabits([habit("m", { due: true, muted: true })]);
    expect(groups.due).toEqual([]);
    expect(groups.muted.map((h) => h.product_key)).toEqual(["m"]);
  });

  it("loses nothing", () => {
    const list = [habit("a"), habit("b", { due: true }), habit("c", { muted: true })];
    const { due, tracked, muted } = groupHabits(list);
    expect(due.length + tracked.length + muted.length).toBe(list.length);
  });
});

describe("canBuild", () => {
  it("offers a basket only when something could become a cart line", () => {
    expect(canBuild([habit("a")])).toBe(true);
    expect(canBuild([habit("a", { muted: true })])).toBe(false);
    // Counter meat keyed by a till article: a cadence, never a cart line.
    expect(canBuild([habit("lager:1", { reorderable: false })])).toBe(false);
    expect(canBuild([])).toBe(false);
  });
});
