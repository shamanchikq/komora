import { describe, expect, it } from "vitest";
import { isTelegramHost } from "./telegram";

/** The SDK installs `window.Telegram.WebApp` wherever it is loaded — a plain browser
 * tab included — so the object's presence is not evidence of a Telegram client.
 * `platform` is, and this is the whole of that judgement. Getting it wrong hid the
 * fallback bar behind a MainButton that was a silent stub, leaving a browser with no
 * way to send anything at all. */
describe("isTelegramHost", () => {
  it("accepts the platforms a real client reports", () => {
    for (const p of ["ios", "android", "tdesktop", "weba", "webk", "macos"]) {
      expect(isTelegramHost(p)).toBe(true);
    }
  });

  it("rejects the stub the SDK installs off-platform", () => {
    // Measured in a browser against the vendored SDK, 2026-08-26.
    expect(isTelegramHost("unknown")).toBe(false);
  });

  it("rejects an absent or empty platform", () => {
    expect(isTelegramHost(undefined)).toBe(false);
    expect(isTelegramHost("")).toBe(false);
  });
});
