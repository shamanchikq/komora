import { describe, expect, it } from "vitest";
import { ApiError, describeError } from "./api";

/** The mapping, not the prose: what a failure is allowed to claim depends on what was
 * being attempted. The rule this guards is the repo's own — never claim more than the
 * data supports — applied to the one direction the backend cannot cover, because the
 * answer never arrived. */
describe("describeError", () => {
  it("never claims a failed write did nothing", () => {
    for (const error of [new ApiError(0), new ApiError(500), new TypeError("boom")]) {
      const text = describeError(error, "write");
      expect(text).toContain("не видно");
      expect(text).toContain("не подвоїть");
    }
  });

  it("says nothing was sent only where that is certain", () => {
    // 401 is decided before any handler runs, so the write provably did not happen.
    expect(describeError(new ApiError(401), "write")).toContain("через Telegram");
  });

  it("tells a failed read that the draft survived it", () => {
    expect(describeError(new ApiError(0), "read")).toContain("Чернетка на місці");
  });

  it("tells a failed compose there is no draft, because there is not", () => {
    expect(describeError(new ApiError(0), "draft")).toContain("чернетки немає");
  });

  it("distinguishes a server answer from no answer", () => {
    expect(describeError(new ApiError(500), "read")).toContain("на сервері");
    expect(describeError(new Error("network"), "read")).toContain("Щось пішло не так");
  });
});
