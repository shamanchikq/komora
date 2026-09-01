/** The wording, from `bot/render.py` refined by the approved design's three
 * correctness passes. Two rules travel with it: every line shows its reason, and
 * warnings are rendered never dropped — an unknown code falls through verbatim. */

import { items, uah } from "./format";

export const SEND_BUTTON = "Надіслати в Сільпо";
export const PUSH_BUTTON = "Додати в кошик";
export const CANCEL_BUTTON = "Скасувати";
export const NOTHING_LEFT = "Надсилати вже нема чого";
export const CHECKOUT_BUTTON = "Оформити на сайті";
export const APP_BUTTON = "Оформити в застосунку";
export const NO_LINK = "Оформити замовлення — у застосунку або на сайті Сільпо.";

export const MIXED_TOTALS =
  "Це різні суми — остаточну рахує Сільпо вже в кошику, зі своїми знижками.";

export const REMOVAL_NOTE =
  "Це те, що Комора додала раніше. Решти у вашому кошику не торкаємось. " +
  "Сама вона це не скасує — якщо передумаєте, попросіть у чаті або додайте в Сільпо.";

export const ALREADY_SENT_ROW = "вже в кошику Сільпо";

/** Mirrors `handlers.NO_ALTERNATIVE`. Shown as a quiet banner ON the draft, never as
 * a screen of its own: a ⇄ that finds nothing must leave the basket where it was. */
export function noAlternatives(name: string): string {
  return `Інших варіантів для «${name}» Сільпо не пропонує.`;
}

/** Mirrors `render.ALREADY_SENT`. A push that lands partly leaves the basket open on
 * purpose — it has to be retriable — and an open basket is drawn as an ordinary draft
 * under a footer promising Silpo is untouched. For these rows it is not. */
export function alreadySentNote(n: number): string {
  return `${items(n)} з цієї чернетки вже в кошику Сільпо — повторне надсилання оновить кількість, а не додасть ще раз.`;
}

export const TRUST_FOOTER =
  "Чернетка живе в Коморі, поки ви не підтвердите — у кошику Сільпо нічого не зміниться.";

export const TRUST_FOOTER_PARTIAL =
  "Решта чернетки живе в Коморі, поки ви не підтвердите. Позначені ✓ позиції вже в кошику Сільпо — Комора сама їх не прибере.";

export const DONE_SUBTITLE =
  "Ціна в кошику Сільпо може відрізнятись від чернетки — знижки застосовує Сільпо, не Комора.";

/** Quiet vs firm: degraded modes inform; verification and the timeslot change what
 * the user should do, so they read firmer (the design's NOTICE table). */
const NOTICE: Record<string, { text: string; firm: boolean }> = {
  "degraded:coupons": { text: "Купони зараз недоступні — показано без них.", firm: false },
  "degraded:replacements": {
    text: "Сільпо не підказав заміни — деякі позиції могли бути кращими.",
    firm: false,
  },
  "degraded:verification": {
    text:
      "Не вдалося перевірити, чи товари відповідають запиту — перегляньте позиції уважніше.",
    firm: true,
  },
  timeslot: {
    text:
      "Час доставки у вашому кошику Сільпо вже недоступний. Кошик зберемо, але " +
      "оберіть новий час у застосунку Сільпо перед оформленням.",
    firm: true,
  },
};

/** By full code first, then by kind: `degraded:coupons` has its own entry, while
 * `timeslot:expired` is one of a kind that reads the same whatever its suffix.
 * Looking up the kind alone matched neither, and the miss used to fall through to
 * `warningText`, which handed `degraded:*` straight back here — a blank screen. */
function notice(code: string): { text: string; firm: boolean } | undefined {
  const [kind] = split(code);
  return NOTICE[code] ?? NOTICE[kind];
}

export function noticeText(code: string): string {
  return notice(code)?.text ?? warningText(code);
}

export function noticeFirm(code: string): boolean {
  return notice(code)?.firm ?? false;
}

/** The warnings still worth drawing once the budget caption exists.
 *
 * `over_budget:84.30` renders as «Понад тижневий бюджет на 84,30 ₴», which is the
 * budget caption's own sentence — and the caption is derived from `budgetCap` and
 * `cart.total`, while the warning is emitted only when a cap is set. So wherever one
 * is drawn the other is redundant, and both were. Ported from `render.budget_shown`.
 */
export function budgetShown(warnings: string[], budgetCap: number | null): string[] {
  if (budgetCap === null) return warnings;
  return warnings.filter((code) => !code.startsWith("over_budget:"));
}

/** Warnings that are notices rather than «не знайшлося» entries. */
export function isNotice(code: string): boolean {
  const [kind] = split(code);
  return kind === "degraded" || kind === "timeslot" || kind === "over_budget";
}

export function warningText(code: string): string {
  const [kind, rest] = split(code);
  if (kind === "not_found") return `Не знайшлося: «${rest}»`;
  if (kind === "over_budget") return `Понад тижневий бюджет на ${uah(rest)}`;
  // The notice table owns every other prose code, timeslot included — it used to be
  // written out a second time here, which is one edit away from two wordings.
  // Never calls back into `noticeText`: that edge was the recursion.
  return notice(code)?.text ?? code;
}

/** `calculation.validations[].message` is a **code**, not prose. The design's short
 * forms; only codes actually observed live belong here — a guessed key never fires,
 * so an unknown code still shows verbatim. */
const VALIDATIONS: Record<string, string> = {
  "order.cost.min": "Сума замовлення менша за мінімальну для цього магазину",
  "product.offer.stock.max": "Однієї позиції більше, ніж залишилось на складі",
  "timeslot.not_available": "Обраний час доставки більше не доступний",
};

export function validationText(code: string): string {
  const known = VALIDATIONS[code.trim()];
  return known ?? `Сільпо повідомляє про перешкоду: ${code}`;
}

function split(code: string): [string, string] {
  const at = code.indexOf(":");
  return at === -1 ? [code, ""] : [code.slice(0, at), code.slice(at + 1)];
}
