/** The one place the app talks to Telegram's WebApp API.
 *
 * Spec §8 platform rules, decided and non-negotiable: expand on launch; never 100vh —
 * viewportStableHeight; disableVerticalSwipes (the cart is one long scroll); native
 * MainButton for the primary action and BackButton for going back. Komora owns its
 * palette — the only signal read from Telegram is light/dark.
 *
 * Everything degrades outside Telegram (a plain browser during development): no
 * initData, no native buttons — the screens render their own fallbacks instead.
 */

export type ThemeSignal = "light" | "dark";

interface TgMainButton {
  setText(text: string): void;
  onClick(cb: () => void): void;
  offClick(cb: () => void): void;
  show(): void;
  hide(): void;
  enable(): void;
  disable(): void;
  showProgress(leaveActive?: boolean): void;
  hideProgress(): void;
  setParams(params: { color?: string; text_color?: string }): void;
}

interface TgBackButton {
  onClick(cb: () => void): void;
  offClick(cb: () => void): void;
  show(): void;
  hide(): void;
}

export interface WebApp {
  /** "ios" | "android" | "tdesktop" | "weba" | … in a real client, "unknown" in the
   * stub the SDK installs anywhere else. The one honest test for a Telegram host. */
  platform?: string;
  initData: string;
  /** The launch payload, already parsed by the SDK. `start_param` is read only to
   * decide which screen to open: it is the same value the backend sees inside the
   * signed `initData`, and what it names is checked there, never trusted here. */
  initDataUnsafe?: { start_param?: string };
  colorScheme: "light" | "dark";
  viewportStableHeight: number;
  ready(): void;
  expand(): void;
  disableVerticalSwipes?(): void;
  setHeaderColor?(color: string): void;
  setBackgroundColor?(color: string): void;
  setBottomBarColor?(color: string): void;
  openLink?(url: string): void;
  onEvent(event: string, cb: () => void): void;
  offEvent(event: string, cb: () => void): void;
  MainButton: TgMainButton;
  BackButton: TgBackButton;
}

declare global {
  interface Window {
    Telegram?: { WebApp?: WebApp };
  }
}

export function webApp(): WebApp | undefined {
  return window.Telegram?.WebApp;
}

/** Whether `platform` names a real Telegram client. Pure, so it is testable. */
export function isTelegramHost(platform: string | undefined): boolean {
  return platform !== undefined && platform !== "" && platform !== "unknown";
}

/** The WebApp **only when genuinely running inside Telegram**.
 *
 * The vendored SDK defines `window.Telegram.WebApp` wherever it is loaded, a plain
 * browser tab included, so the object's presence proves nothing — and every UI
 * decision that read it concluded "inside Telegram" everywhere. The fallback bar was
 * then never rendered, the native MainButton was a silent stub, and `ComposeScreen`
 * deliberately has no button of its own: the app outside Telegram had no control at
 * all. You could type a basket and never send it.
 *
 * Use this for anything that draws or drives UI. `webApp()` stays for reading data —
 * `initData` and `start_param` are empty off-platform, which is the correct answer.
 */
export function telegramHost(): WebApp | undefined {
  const wa = webApp();
  return wa !== undefined && isTelegramHost(wa.platform) ? wa : undefined;
}

/** Palette ground colours, pushed outward per spec so the chrome matches the page. */
const GROUND: Record<ThemeSignal, string> = {
  light: "#FCFAF6",
  dark: "#17130F",
};

let currentTheme: ThemeSignal = "light";
const themeListeners = new Set<() => void>();

export function initTelegram(): void {
  const wa = telegramHost();
  if (!wa) {
    // Browser development: follow the OS preference so both palettes stay reachable.
    apply("light");
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.matches) apply("dark");
    mq.addEventListener("change", (event) => apply(event.matches ? "dark" : "light"));
    syncViewport();
    return;
  }

  wa.ready();
  wa.expand();
  wa.disableVerticalSwipes?.();
  apply(wa.colorScheme);
  wa.onEvent("themeChanged", () => apply(telegramHost()?.colorScheme ?? "light"));
  syncViewport();
  wa.onEvent("viewportChanged", syncViewport);
}

function apply(theme: ThemeSignal): void {
  currentTheme = theme;
  document.documentElement.dataset.theme = theme;
  const ground = GROUND[theme];
  const wa = telegramHost();
  try {
    wa?.setHeaderColor?.(ground);
    wa?.setBackgroundColor?.(ground);
    wa?.setBottomBarColor?.(ground);
  } catch {
    // Older clients reject these calls; the palette on the page still holds.
  }
  for (const listener of [...themeListeners]) listener();
}

function syncViewport(): void {
  // Never 100vh: inside Telegram the stable height is what does not jump when the
  // collapsible toolbar hides. Off-platform the stub reports 0, which collapsed
  // `min-height: var(--vsh)` to nothing — so the real window is the answer there.
  const height = telegramHost()?.viewportStableHeight || window.innerHeight;
  document.documentElement.style.setProperty("--vsh", `${height}px`);
}

export function theme(): ThemeSignal {
  return currentTheme;
}

export function onThemeChange(listener: () => void): () => void {
  themeListeners.add(listener);
  return () => {
    themeListeners.delete(listener);
  };
}

/** Checkout links leave the webview. http(s) goes through `openLink` (real browser);
 * Silpo's `silpo://` deep link can only be handed to the OS directly. */
export function openExternal(url: string): void {
  const wa = telegramHost();
  if (url.startsWith("http")) {
    if (wa?.openLink) {
      wa.openLink(url);
    } else {
      window.open(url, "_blank", "noopener");
    }
    return;
  }
  window.location.href = url;
}
