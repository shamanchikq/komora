import type { Preview } from "../types";
import { items, itemsAcc, uah } from "../format";
import { MIXED_TOTALS, REMOVAL_NOTE, validationText } from "../copy";

/** The confirmation sheet, per the approved design: a title that names every kind of
 * change («додати · оновити · прибрати»), two labelled figures that are never summed,
 * the green reassurance only when it is true, and — when something is being removed —
 * an inverted panel that is deliberately the heaviest thing on screen. */

function sheetTitle(preview: Preview): string {
  const addN = preview.adding_count - preview.overlapping.length;
  const ovN = preview.overlapping.length;
  const rN = preview.removing.length;

  if (ovN === 0 && rN === 0) return `Додати ${itemsAcc(addN)} у кошик Сільпо?`;
  if (addN === 0 && ovN === 0 && rN > 0) return `Прибрати ${itemsAcc(rN)} з кошика Сільпо?`;

  const parts: string[] = [];
  if (addN > 0) parts.push(`додати ${itemsAcc(addN)}`);
  if (ovN > 0) parts.push(`оновити ${itemsAcc(ovN)}`);
  if (rN > 0) parts.push(`прибрати ${itemsAcc(rN)}`);
  const joined = parts.join(" · ");
  return `${joined.charAt(0).toUpperCase()}${joined.slice(1)}?`;
}

export function confirmLabel(preview: Preview): string {
  const addN = preview.adding_count - preview.overlapping.length;
  const ovN = preview.overlapping.length;
  const rN = preview.removing.length;

  if (ovN === 0 && rN === 0) return `Додати ${itemsAcc(addN)}`;
  if (addN === 0 && ovN === 0 && rN > 0) return `Прибрати ${itemsAcc(rN)}`;

  const parts: string[] = [];
  if (addN > 0) parts.push(`Додати ${addN}`);
  if (ovN > 0) parts.push(`оновити ${ovN}`);
  if (rN > 0) parts.push(`прибрати ${rN}`);
  return parts.join(" · ");
}

export function PreviewSheet({ preview }: { preview: Preview }) {
  const addN = preview.adding_count - preview.overlapping.length;
  const rN = preview.removing.length;
  const validations = [...new Set(preview.blocking_validations)];

  return (
    <section className="screen">
      <div className="sheet-card">
        <h1>{sheetTitle(preview)}</h1>

        <p className="lead">
          {preview.existing_count > 0
            ? rN > 0
              ? `У вашому кошику Сільпо вже ${items(preview.existing_count)}. Решти не чіпаємо — прибираємо тільки те, що названо нижче.`
              : `У вашому кошику Сільпо вже ${items(preview.existing_count)} — не чіпаємо.`
            : "Ваш кошик Сільпо зараз порожній."}
        </p>

        {(preview.existing_count > 0 || preview.adding_count > 0) && (
          <div className="figures">
            {preview.existing_count > 0 && (
              <div className="figure">
                <span className="label">
                  Уже в кошику Сільпо · до сплати {items(preview.existing_count)}
                </span>
                <b className="mono">{uah(preview.existing_total)}</b>
              </div>
            )}
            {preview.adding_count > 0 && (
              <div className="figure">
                <span className="label">Надсилаємо з Комори · за цінами каталогу</span>
                <b className="mono">{uah(preview.adding_total)}</b>
              </div>
            )}
          </div>
        )}
        {preview.existing_count > 0 && preview.adding_count > 0 && (
          <p className="hint">{MIXED_TOTALS}</p>
        )}

        {rN === 0 && preview.adding_count > 0 && (
          <div className="reassure">
            <div>
              <b>Нічого не видалимо.</b> Ваші позиції в Сільпо залишаться на місці — ми
              лише додаємо.
            </div>
          </div>
        )}

        {rN > 0 && (
          <div className="removal-panel" role="alert">
            <h2>
              <i>−</i>Приберемо з кошика {itemsAcc(rN)}
            </h2>
            <ul>
              {preview.removing.map((name) => (
                <li key={name}>
                  <i>−</i>
                  <span>{name}</span>
                </li>
              ))}
            </ul>
            <div className="note">{REMOVAL_NOTE}</div>
          </div>
        )}

        {preview.overlapping.length > 0 && (
          <div className="plain-block">
            <b>
              {preview.overlapping.length === 1
                ? "1 позиція вже у вашому кошику"
                : `${preview.overlapping.length} уже у вашому кошику`}
            </b>
            {preview.overlapping.join(", ")}. Кількість буде <b>замінено</b>, а не додано.
          </div>
        )}

        {preview.now_unavailable.length > 0 && (
          <div className="plain-block">
            <b>
              {preview.now_unavailable.length === 1
                ? "1 позиція щойно зникла з наявності"
                : `${preview.now_unavailable.length} щойно зникли з наявності`}
            </b>
            {preview.now_unavailable.join(", ")}. Сільпо може їх не додати.
          </div>
        )}

        {preview.drift !== null && (
          <p className="drift-row">
            Ціни змінилися: було <s className="mono">{uah(preview.drift[0])}</s>, зараз{" "}
            <b className="mono">{uah(preview.drift[1])}</b>.
          </p>
        )}

        {validations.length > 0 && (
          <div className="blocker-panel">
            <b>Сільпо не дасть оформити замовлення:</b>
            <ul>
              {validations.map((code) => (
                <li key={code}>{validationText(code)}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {addN + preview.overlapping.length === 0 && rN > 0 && (
        <footer className="trust">Додавати нічого — це кошик, який лише прибирає.</footer>
      )}
    </section>
  );
}
