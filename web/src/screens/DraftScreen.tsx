import type { ReactNode } from "react";
import type { Cart, Line } from "../types";
import { items, pl, quantityText, uiInt, uah } from "../format";
import { CANCEL_BUTTON, isNotice, noticeFirm, noticeText, warningText } from "../copy";

/** The draft review — the approved design's row anatomy: tile chip, struck original
 * above a substitution, meta line that guards the empty `unit` (a search hit carries
 * no size field), a step-based stepper for weighted goods with Silpo's stock as its
 * ceiling, and ⇄ on every row that can still be sent. */

function tileCode(name: string): string {
  const word = name.split(/\s+/)[0] ?? "";
  return word.slice(0, 3).toUpperCase();
}

export function qtyLabel(line: Line): string {
  if (!line.weighted) return String(Number(line.qty));
  // `quantityText` already writes a weight the way both surfaces write one. This
  // derived the precision from `step` instead and then stripped trailing zeros from
  // a string that need not have had a decimal point at all: with Silpo's common
  // `"step": 1`, ten kilos of potatoes rendered as «1 кг».
  return `${quantityText(line.qty)}\u00A0кг`;
}

interface RowProps {
  line: Line;
  busy: boolean;
  onSwap: () => void;
  onSetQty: (qty: number) => void;
  onRemove: () => void;
}

function LineRow({ line, busy, onSwap, onSetQty, onRemove }: RowProps) {
  const excluded = line.unavailable;
  const atCeiling = !excluded && line.stock !== null && line.qty >= line.stock - 1e-9;
  const atFloor = !excluded && (line.weighted ? line.qty <= (line.step ?? 0.1) + 1e-9 : line.qty <= 1);
  const substituted = line.substituted_from !== null;

  return (
    <li className={excluded ? "row row-off" : "row"}>
      <div className="tile" aria-hidden>
        {tileCode(line.name)}
      </div>
      <div className="row-main">
        <div className="row-name">
          <span className="name">
            {substituted && <span className="orig-name">{line.substituted_from}<br /></span>}
            {line.name}
          </span>
          {!excluded && (
            <button className="swap" aria-label={`Інший варіант: ${line.name}`} onClick={onSwap}>
              ⇄
            </button>
          )}
        </div>

        <div className="meta-line mono">
          <span>
            {line.weighted
              ? `${uah(line.unit_price)}/кг`
              : line.unit !== ""
                ? `${line.unit} · ${uah(line.unit_price)}`
                : uah(line.unit_price)}
            {line.old_price !== null && (
              <s style={{ marginLeft: 6 }}>було {uah(line.old_price)}</s>
            )}
          </span>
          <b className="line-sum">{uah(line.line_total)}</b>
        </div>

        {!excluded && (
          <div className="stepper">
            <button
              aria-label={atFloor ? `Прибрати ${line.name}` : `Менше: ${line.name}`}
              disabled={busy}
              onClick={atFloor ? onRemove : () => onSetQty(nextQty(line, -1))}
            >
              {atFloor ? "✕" : "−"}
            </button>
            <span className={`mono qty-label${line.weighted ? " weighted" : ""}`}>
              {qtyLabel(line)}
            </span>
            <button
              aria-label={`Більше: ${line.name}`}
              disabled={busy || atCeiling}
              onClick={() => onSetQty(nextQty(line, +1))}
            >
              +
            </button>
          </div>
        )}

        {atCeiling && <div className="at-ceiling">Більше немає в наявності — це весь залишок.</div>}

        <div className={`reason${substituted ? " sub" : ""}`}>
          <i />
          <span>
            {excluded
              ? "Немає в Сільпо — не надсилаємо і не рахуємо"
              : substituted
                ? `Заміна на ваш запит замість «${line.substituted_from}»`
                : line.reason_text}
          </span>
        </div>
        {line.optional && !excluded && <div className="hint">○ необовʼязково</div>}
      </div>
    </li>
  );
}

function nextQty(line: Line, direction: -1 | 1): number {
  if (line.weighted) {
    const step = line.step ?? 0.1;
    return Math.round((line.qty + direction * step) * 1000) / 1000;
  }
  return Math.max(1, Math.round(line.qty) + direction);
}

export function DraftScreen({
  title,
  cart,
  budgetCap,
  busy,
  onSwap,
  onSetQty,
  onRemove,
  onTrim,
  onCancel,
}: {
  title: string;
  cart: Cart;
  budgetCap: number | null;
  busy: boolean;
  onSwap: (position: number) => void;
  onSetQty: (position: number, qty: number) => void;
  onRemove: (position: number) => void;
  onTrim: () => void;
  /** `null` for a draft that was never persisted — there is nothing to discard. */
  onCancel: (() => void) | null;
}) {
  const sendable = cart.lines.filter((line) => !line.unavailable);
  const excludedCount = cart.lines.length - sendable.length;
  const notices = cart.warnings.filter(isNotice);
  const failures = cart.warnings.filter((code) => !isNotice(code));
  const notes = [...cart.savings_notes, ...cart.coupon_notes].slice(0, 8);

  const optionalLines = sendable.filter((line) => line.optional);

  let budget: { over: boolean; fillPercent: number; caption: ReactNode } | null = null;
  if (budgetCap !== null && Number(cart.total) > 0) {
    const left = budgetCap - Number(cart.total);
    const fillPercent = Math.max(0, Math.min(1, Number(cart.total) / budgetCap)) * 100;
    budget =
      left >= 0
        ? {
            over: false,
            fillPercent,
            caption: (
              <>
                Бюджет <b>{uiInt(budgetCap)} ₴</b> — лишається <b>{uah(left.toFixed(2))}</b>.
              </>
            ),
          }
        : {
            over: true,
            fillPercent,
            caption: (
              <>
                Бюджет <b>{uiInt(budgetCap)} ₴</b> — перевищено на{" "}
                <b>{uah((-left).toFixed(2))}</b>. Це ваш вибір: надіслати можна попри це.
              </>
            ),
          };
  }

  return (
    <section className="screen">
      <h1>{title}</h1>

      {notices.map((code) => (
        <div key={code} className={noticeFirm(code) ? "notice firm" : "notice quiet"}>
          {noticeText(code)}
        </div>
      ))}
      {failures.map((code) => (
        <div key={code} className="notice quiet">
          {warningText(code)}
        </div>
      ))}

      {cart.lines.length === 0 && cart.removals.length === 0 && (
        <p className="empty">Нічого не вдалося підібрати.</p>
      )}

      <ul className="lines">
        {cart.lines.map((line, index) => (
          <LineRow
            key={`${line.product_id}-${index}`}
            line={line}
            busy={busy}
            onSwap={() => onSwap(index)}
            onSetQty={(qty) => onSetQty(index, qty)}
            onRemove={() => onRemove(index)}
          />
        ))}
      </ul>

      {cart.removals.length > 0 && (
        <div className="removals">
          <b>Приберемо з кошика Сільпо:</b> {cart.removals.map((r) => r.name).join(", ")}.{" "}
          <span className="hint">Це те, що Комора додала раніше.</span>
        </div>
      )}

      {budget !== null && (
        <div className="budget-block">
          <div className="budget-track">
            <div
              className={budget.over ? "budget-fill over" : "budget-fill"}
              style={{ width: `${budget.fillPercent}%` }}
            />
          </div>
          <div className={budget.over ? "budget-caption over" : "budget-caption"}>
            <i
              style={{
                width: 7,
                height: 7,
                borderRadius: 9,
                background: "currentColor",
                marginTop: 5,
                flex: "none",
              }}
            />
            <span>{budget.caption}</span>
          </div>
          {budget.over && optionalLines.length > 0 && (
            <button className="trim-btn" disabled={busy} onClick={onTrim}>
              Прибрати {optionalLines.length}{" "}
              {pl(optionalLines.length, "необовʼязкову позицію", "необовʼязкові позиції", "необовʼязкових позицій")}
            </button>
          )}
        </div>
      )}

      {Number(cart.estimated_savings) > 0 && (
        <div className="totals-note mono">Заощаджено ≈ {uah(cart.estimated_savings)}</div>
      )}

      {notes.length > 0 && (
        <details className="notes">
          <summary>Знижки та купони</summary>
          <ul>
            {notes.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        </details>
      )}

      <footer className="trust">
        Чернетка живе в Коморі, поки ви не підтвердите — у кошику Сільпо нічого не
        зміниться.
      </footer>

      {onCancel !== null && (
        <button className="discard" disabled={busy} onClick={onCancel}>
          {CANCEL_BUTTON}
        </button>
      )}

      <div className="summary">
        <div>
          <div className="count">{items(sendable.length)} до надсилання</div>
          {excludedCount > 0 && (
            <div className="excluded-count">{items(excludedCount)} недоступні</div>
          )}
        </div>
        <div>
          <div className="sum mono">{uah(cart.total)}</div>
          <div className="cat-hint">за цінами каталогу</div>
        </div>
      </div>
    </section>
  );
}
