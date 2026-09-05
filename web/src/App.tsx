/** Screen flow: compose → loading → draft review → sync sheet → synced, with prose
 * outcomes as the universal detour. The native MainButton carries each screen's
 * primary action; outside Telegram an in-page fallback bar does the same job. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, describeError, staysOnScreen } from "./api";
import type { Attempt } from "./api";
import { launchTarget } from "./deeplink";
import { telegramHost } from "./telegram";
import type {
  AlternativesOutcome,
  DraftOutcome,
  Outcome,
  PreviewOutcome,
  SyncedOutcome,
} from "./types";
import { ComposeScreen } from "./screens/ComposeScreen";
import { DraftScreen } from "./screens/DraftScreen";
import { PreviewSheet, confirmLabel, hasChanges } from "./screens/PreviewSheet";
import { SyncedScreen } from "./screens/SyncedScreen";
import { AlternativesSheet } from "./screens/AlternativesSheet";
import { SpokeView } from "./screens/SpokeView";
import { SEND_BUTTON, noAlternatives } from "./copy";

interface Banner {
  text: string;
  firm: boolean;
}

type View =
  | { name: "compose" }
  | { name: "loading" }
  | { name: "draft"; outcome: DraftOutcome }
  | { name: "preview"; outcome: PreviewOutcome }
  | { name: "synced"; outcome: SyncedOutcome }
  | { name: "alternatives"; outcome: AlternativesOutcome }
  | { name: "spoke"; text: string; needsLink: boolean };

const RETRY_BUTTON = "Спробувати ще раз";
const NOTICE_MS = 2600;

/** The two attempts with no screen behind them: they replace whatever was there with
 * the loading shell, so a failure has nowhere to stay and lands on compose. Every
 * other attempt is launched from a screen that is still true while it runs. */
const FROM_NOTHING: readonly Attempt[] = ["draft", "open"];

export default function App() {
  // A deep link is read once, before the first paint, so a launch that names a basket
  // opens on the skeleton rather than flashing the compose screen on its way there.
  // Lazy: `start_param` is frozen for the session, so reading it again every render
  // would be work that cannot produce a different answer.
  const [target] = useState(launchTarget);
  const [view, setView] = useState<View>(
    target === null ? { name: "compose" } : { name: "loading" },
  );
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  // One banner, two tones. A failure is firm; a spoken ANSWER to an edit («інших
  // варіантів нема») is quiet — it is news, not an alarm.
  const [banner, setBanner] = useState<Banner | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [lastDraft, setLastDraft] = useState<DraftOutcome | null>(null);

  const flash = useCallback((toast: string | null) => {
    if (toast === null) return;
    setNotice(toast);
    window.setTimeout(() => setNotice(null), NOTICE_MS);
  }, []);

  const route = useCallback(
    (outcome: Outcome) => {
      if (outcome.kind === "draft") {
        setView({ name: "draft", outcome });
        setLastDraft(outcome.basket_id === null ? null : outcome);
        flash(outcome.toast);
        return;
      }
      switch (outcome.kind) {
        case "alternatives":
          setView({ name: "alternatives", outcome });
          break;
        case "preview":
          setView({ name: "preview", outcome });
          break;
        case "synced":
          setView({ name: "synced", outcome });
          break;
        case "spoke":
          // The toast carries the *specific* refusal — «Ця чернетка недоступна» vs
          // «Чернетка вже неактуальна» — while `text` is the one sentence both share.
          // Dropping it made a foreign basket and a spent one read identically, which
          // is the one thing the deep-link checklist asks to be able to tell apart.
          flash(outcome.toast);
          setView({ name: "spoke", text: outcome.text, needsLink: outcome.needs_link });
          break;
      }
    },
    [flash],
  );

  const run = useCallback(
    async (action: () => Promise<Outcome>, attempt: Attempt) => {
      if (busy) return;
      setBusy(true);
      setBanner(null);
      // Composing is the one action that replaces the screen with a loading shell;
      // every other one is launched from a screen that is still true while it runs.
      if (FROM_NOTHING.includes(attempt)) setView({ name: "loading" });
      try {
        const outcome = await action();
        if (outcome.kind === "spoke" && staysOnScreen(outcome, attempt)) {
          // An answer, not a destination: «Сільпо зараз не відповідає» to a preview
          // leaves the draft exactly as it was, and the sheet a push came from is
          // still the sheet to retry it from — the same rule the catch below keeps.
          setBanner({ text: outcome.text, firm: true });
        } else {
          route(outcome);
        }
      } catch (exc) {
        setBanner({ text: describeError(exc, attempt), firm: true });
        // …which is why only the shell falls back to compose. A failed preview leaves
        // the draft exactly as it was; a failed push leaves the sync sheet describing
        // the same write. Dropping to compose threw away the basket the user was
        // confirming and took the retry with it — the screen that failed is the only
        // screen the action can be tried again from.
        if (FROM_NOTHING.includes(attempt)) setView({ name: "compose" });
      } finally {
        setBusy(false);
      }
    },
    [busy, route],
  );

  // Line-level edits keep the draft on screen: the response IS the updated draft.
  const edit = useCallback(
    async (action: () => Promise<Outcome>, attempt: Attempt = "edit") => {
      if (busy) return;
      setBusy(true);
      setBanner(null);
      try {
        const outcome = await action();
        if (outcome.kind === "draft") {
          setView({ name: "draft", outcome });
          setLastDraft(outcome.basket_id === null ? lastDraft : outcome);
          flash(outcome.toast);
        } else if (outcome.kind === "spoke" && !outcome.needs_link) {
          // Prose here is an ANSWER to the edit, not a destination. «Інших варіантів
          // для X Сільпо не пропонує» is the ordinary reply to ⇄ on a line Silpo has
          // one candidate for, and navigating to a full-screen sentence threw the
          // basket off the screen — with none of the scrollback that makes the same
          // outcome harmless in the chat. The backend says so itself by attaching a
          // toast: it expected the draft to still be there underneath.
          flash(outcome.toast);
          setBanner({ text: outcome.text, firm: false });
        } else {
          // `needs_link` is the exception: the Silpo session lapsed, so the draft the
          // user is looking at cannot be acted on until they re-link in the chat.
          // That is a destination.
          route(outcome);
        }
      } catch (exc) {
        // A line edit never leaves its screen, failed or not — the same rule `run`
        // now follows for everything but composing.
        setBanner({ text: describeError(exc, attempt), firm: true });
      } finally {
        setBusy(false);
      }
    },
    [busy, flash, lastDraft, route],
  );

  /** ⇄ — what else Silpo has for this line, as a list rather than one step forward.
   *
   * Deliberately not routed through `edit`: an EMPTY list must not become a screen.
   * That is the 2026-08-26 lesson restated — a ⇄ with nothing behind it used to
   * replace the whole draft with a sentence, and a Mini App has no scrollback to get
   * the basket back from. So "no alternatives" stays a quiet banner on the draft, and
   * only an actual choice is worth navigating to.
   */
  const openAlternatives = useCallback(
    async (basketId: number, position: number) => {
      if (busy) return;
      setBusy(true);
      setBanner(null);
      try {
        const outcome = await api.alternatives(basketId, position);
        if (outcome.kind === "alternatives") {
          if (outcome.options.length === 0) {
            setBanner({ text: noAlternatives(outcome.current.name), firm: false });
          } else {
            setView({ name: "alternatives", outcome });
          }
        } else if (outcome.kind === "spoke" && !outcome.needs_link) {
          flash(outcome.toast);
          setBanner({ text: outcome.text, firm: false });
        } else {
          route(outcome);
        }
      } catch (exc) {
        setBanner({ text: describeError(exc, "read"), firm: true });
      } finally {
        setBusy(false);
      }
    },
    [busy, flash, route],
  );

  /** Discard the draft. The bot puts «Скасувати» beside «Надіслати» on both the draft
   * and the confirmation keyboard; `api.cancel` existed here from the start and
   * nothing called it, so the Mini App could reach a draft and never be rid of one.
   * Routed through `run`, not `edit`: a discarded basket really is a destination. */
  const cancel = useCallback(
    (basketId: number) => void run(() => api.cancel(basketId), "edit"),
    [run],
  );

  const goCompose = useCallback(() => {
    setText("");
    setBanner(null);
    setView({ name: "compose" });
  }, []);

  const goBack = useCallback(() => {
    // Leaving the screen leaves its failure behind: an error that outlives the thing
    // it was about is just an alarming decoration.
    setBanner(null);
    if ((view.name === "preview" || view.name === "alternatives") && lastDraft !== null) {
      setView({ name: "draft", outcome: lastDraft });
    } else if (view.name !== "compose" && view.name !== "loading") {
      goCompose();
    }
  }, [view, lastDraft, goCompose]);

  // --- primary action -------------------------------------------------------------

  const primaryAction = (): (() => void) | null => {
    switch (view.name) {
      case "compose":
        if (text.trim() === "") return null;
        return () => void run(() => api.draft(text.trim()), "draft");
      case "loading":
      case "spoke":
      // The choices ARE the actions here; a MainButton would have to pick one for
      // the user, and picking is the whole point of the screen.
      case "alternatives":
        return null;
      case "draft":
        if (view.outcome.basket_id === null) return null;
        return () =>
          void run(() => api.preview(view.outcome.basket_id as number), "read");
      case "preview":
        // Everything the draft named can vanish between the two taps — the user
        // empties the cart in the Silpo app, or removes the very product this basket
        // was going to remove. `_preview` refuses that case now, so this is the belt
        // to its braces: never offer a button whose own label counts to zero.
        if (!hasChanges(view.outcome.preview)) return null;
        return () => void run(() => api.push(view.outcome.basket_id), "write");
      case "synced":
        if (view.outcome.report.ok) return null;
        return () => void run(() => api.push(view.outcome.basket_id), "write");
    }
  };

  const primaryLabel = (): string | null => {
    switch (view.name) {
      case "compose":
        return text.trim() === "" ? null : "Зібрати кошик";
      case "loading":
      case "spoke":
      case "alternatives":
        return null;
      case "draft":
        return view.outcome.basket_id === null ? null : SEND_BUTTON;
      case "preview":
        return hasChanges(view.outcome.preview) ? confirmLabel(view.outcome.preview) : null;
      case "synced":
        return view.outcome.report.ok ? null : RETRY_BUTTON;
    }
  };

  // Opening what the launch link named. Once, and only at launch: `start_param` is
  // frozen for the life of the session, so reading it again later would reopen the
  // same basket over whatever the user had moved on to.
  const opened = useRef(false);
  useEffect(() => {
    if (opened.current || target === null) return;
    opened.current = true;
    void run(() => api.open(target.id), "open");
  }, [run, target]);

  // A launch with no payload used to mean "compose", always — so the menu button
  // could not reach a draft the user already had, and typing here builds a new basket
  // that DISCARDS it (`create_from_cart`). The way back to a basket was to destroy it.
  // Asked once, silently: a failure or "no draft" both mean compose, which is already
  // on screen, so neither owes the user a banner.
  const textRef = useRef(text);
  textRef.current = text;
  const probed = useRef(false);
  useEffect(() => {
    if (probed.current || target !== null) return;
    probed.current = true;
    void (async () => {
      try {
        const outcome = await api.openActive();
        if (outcome.kind !== "draft" || outcome.basket_id === null) return;
        setLastDraft(outcome);
        // Only from an untouched compose screen: by the time this lands the user may
        // have started typing, or already sent something, and taking the screen from
        // under them would be worse than not restoring the draft at all.
        setView((current) =>
          current.name === "compose" && textRef.current === ""
            ? { name: "draft", outcome }
            : current,
        );
      } catch {
        // Nothing was asked for, so nothing is owed. Compose is a working screen.
      }
    })();
  }, [target]);

  // The banner sits at the top of the screen and the control that failed is usually
  // at the bottom of a long one. An error nobody scrolls to is the silence it replaced.
  const bannerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (banner !== null) bannerRef.current?.scrollIntoView({ block: "nearest" });
  }, [banner]);

  const actionRef = useRef<(() => void) | null>(null);
  actionRef.current = primaryAction();

  const inTelegramRef = useRef<boolean>(false);
  inTelegramRef.current = telegramHost() !== undefined;

  useEffect(() => {
    const wa = telegramHost();
    if (!wa) return;

    const handleMain = () => actionRef.current?.();
    wa.MainButton.onClick(handleMain);

    const handleBack = () => goBack();
    wa.BackButton.onClick(handleBack);

    return () => {
      wa.MainButton.offClick(handleMain);
      wa.BackButton.offClick(handleBack);
    };
  }, [goBack]);

  useEffect(() => {
    const wa = telegramHost();
    if (!wa) return;
    const label = primaryLabel();
    const mb = wa.MainButton;
    mb.setParams({ color: "#FF8522", text_color: "#2A1200" });
    if (label === null || busy) {
      mb.hide();
      return;
    }
    mb.setText(label);
    mb.enable();
    mb.show();
  });

  useEffect(() => {
    const wa = telegramHost();
    if (!wa) return;
    if (view.name === "compose" || view.name === "loading") {
      wa.BackButton.hide();
    } else {
      wa.BackButton.show();
    }
  }, [view]);

  // --- render ---------------------------------------------------------------------

  const fallbackBar = (): ReactNode => {
    const label = primaryLabel();
    if (inTelegramRef.current || label === null || busy) return null;
    return (
      <div className="fallback-bar">
        <button className="fallback-action" onClick={() => actionRef.current?.()}>
          {label}
        </button>
      </div>
    );
  };

  return (
    <main className={`app${busy ? " busy" : ""}`}>
      {notice !== null && (
        <div className="toast" role="status">
          {notice}
        </div>
      )}
      {banner !== null && (
        <div
          className={banner.firm ? "notice firm" : "notice quiet"}
          role={banner.firm ? "alert" : "status"}
          ref={bannerRef}
        >
          {banner.text}
        </div>
      )}

      {view.name === "compose" && (
        <ComposeScreen
          busy={busy}
          text={text}
          onText={setText}
          onSubmit={(value) => void run(() => api.draft(value), "draft")}
        />
      )}

      {view.name === "loading" && (
        <section className="screen">
          <h1>Збираємо чернетку…</h1>
          <div className="skeletons">
            {[82, 64, 74, 58, 70].map((width, i) => (
              <div className="skeleton" key={i}>
                <div className="bone" style={{ width: 40, height: 40, borderRadius: 9 }} />
                <div style={{ flex: 1 }}>
                  <div className="bone" style={{ width: `${width}%`, height: 11 }} />
                  <div
                    className="bone"
                    style={{ width: "45%", height: 11, marginTop: 7 }}
                  />
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {view.name === "draft" && (
        <DraftScreen
          title={view.outcome.title}
          cart={view.outcome.cart}
          budgetCap={view.outcome.budget_cap}
          busy={busy}
          onSwap={(position) =>
            view.outcome.basket_id !== null &&
            void openAlternatives(view.outcome.basket_id as number, position)
          }
          onSetQty={(position, qty) =>
            view.outcome.basket_id !== null &&
            void edit(() => api.setQty(view.outcome.basket_id as number, position, qty))
          }
          onRemove={(position) =>
            view.outcome.basket_id !== null &&
            void edit(() => api.removeLine(view.outcome.basket_id as number, position))
          }
          onTrim={() =>
            view.outcome.basket_id !== null &&
            void edit(() => api.trimOptional(view.outcome.basket_id as number))
          }
          onCancel={
            view.outcome.basket_id === null
              ? null
              : () => cancel(view.outcome.basket_id as number)
          }
        />
      )}

      {view.name === "preview" && (
        <PreviewSheet
          preview={view.outcome.preview}
          busy={busy}
          onCancel={() => cancel(view.outcome.basket_id)}
        />
      )}
      {view.name === "alternatives" && (
        <AlternativesSheet
          current={view.outcome.current}
          options={view.outcome.options}
          busy={busy}
          onPick={(productId) =>
            void edit(
              () =>
                api.chooseAlternative(
                  view.outcome.basket_id,
                  view.outcome.position,
                  productId,
                ),
              "read",
            )
          }
          onBack={goBack}
        />
      )}

      {view.name === "synced" && <SyncedScreen report={view.outcome.report} />}

      {view.name === "spoke" && (
        <SpokeView text={view.text} needsLink={view.needsLink} />
      )}

      {fallbackBar()}
    </main>
  );
}
