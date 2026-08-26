/** Every request carries the launch payload as its credential: the backend verifies
 * the HMAC and answers for that user, or 401s. Same-origin by deployment (FastAPI
 * serves `web/dist`), so no CORS is involved.
 *
 * It also owns what a failure *means*, because that is a property of the call and not
 * of the screen that made it — see `describeError`. */

import { webApp } from "./telegram";
import type { Outcome } from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`API ${status}`);
    this.status = status;
  }
}

/** What the failed call was trying to do. The honest sentence depends on it: a read
 * that fails changed nothing, and a **write** that fails changed we-do-not-know-what
 * — the request may have reached Silpo and died on the way back.
 *
 * `edit` is the one that never involves Silpo at all: a stepper tap, a ✕, a trim and a
 * cancel are changes to Komora's own draft. Reporting «Сільпо не відповідає» for a
 * request that was never going to reach Silpo names the wrong party — the same class
 * of claim-more-than-the-data-supports the rest of this file exists to avoid. `read`
 * stays for ⇄, which really does search Silpo again. */
export type Attempt = "draft" | "open" | "read" | "edit" | "write";

const NEEDS_TELEGRAM =
  "Відкрийте Комору через Telegram — інакше вона не може довести, що це ви.";
const WRITE_UNKNOWN =
  "Не дочекались відповіді на надсилання — що саме потрапило в кошик Сільпо, звідси " +
  "не видно. Спробуйте ще раз: повторне надсилання нічого не подвоїть.";
const NO_DRAFT =
  "Сільпо не відповідає — чернетки немає. Це не через ваш запит; спробуйте за хвилину.";
const NO_ANSWER = "Сільпо не відповідає. Чернетка на місці — спробуйте за хвилину.";
const NO_REACH = "Комора не відповідає. Чернетка на місці — спробуйте за хвилину.";
const NO_OPEN = "Не вдалося відкрити цю чернетку. Спробуйте, будь ласка, ще раз.";
const SERVER = "Сталася помилка на сервері. Спробуйте, будь ласка, за кілька хвилин.";
const UNKNOWN = "Щось пішло не так. Спробуйте, будь ласка, ще раз.";

export function describeError(error: unknown, attempt: Attempt): string {
  // 401 is decided by the auth dependency before any handler runs, so this one case
  // is certain even on a write: nothing was sent.
  if (error instanceof ApiError && error.status === 401) return NEEDS_TELEGRAM;
  // Everything else about a write is not. Reporting «нічого не сталося» here would be
  // the same lie as reporting a partial sync as success — from the other side.
  if (attempt === "write") return WRITE_UNKNOWN;
  // Opening reads Komora's own database and never touches Silpo, so it cannot blame
  // Silpo for the failure the way the other two can.
  if (attempt === "open") return NO_OPEN;
  if (!(error instanceof ApiError)) return UNKNOWN;
  if (error.status === 0) {
    if (attempt === "draft") return NO_DRAFT;
    // Nothing left the phone, so nothing changed either way — but say whose silence
    // it was. A draft edit is Komora's alone.
    if (attempt === "edit") return NO_REACH;
    return NO_ANSWER;
  }
  return SERVER;
}

async function call(method: "GET" | "POST", path: string, body?: unknown): Promise<Outcome> {
  const initData = webApp()?.initData ?? "";
  let response: Response;
  try {
    response = await fetch(`/api/${path}`, {
      method,
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        Authorization: `tma ${initData}`,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0);
  }
  if (!response.ok) {
    throw new ApiError(response.status);
  }
  return (await response.json()) as Outcome;
}

const post = (path: string, body?: unknown) => call("POST", path, body);
/** The one read: everything else here acts. */
const get = (path: string) => call("GET", path);

export const api = {
  draft: (text: string) => post("draft", { text }),
  open: (basketId: number) => get(`baskets/${basketId}`),
  preview: (basketId: number) => post(`baskets/${basketId}/preview`),
  push: (basketId: number) => post(`baskets/${basketId}/push`),
  swap: (basketId: number, position: number) =>
    post(`baskets/${basketId}/swap`, { position }),
  setQty: (basketId: number, position: number, qty: number) =>
    post(`baskets/${basketId}/lines/${position}/qty`, { qty }),
  removeLine: (basketId: number, position: number) =>
    post(`baskets/${basketId}/lines/${position}/remove`),
  trimOptional: (basketId: number) => post(`baskets/${basketId}/trim`),
  cancel: (basketId: number) => post(`baskets/${basketId}/cancel`),
};
