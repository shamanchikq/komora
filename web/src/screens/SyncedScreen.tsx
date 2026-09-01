import type { Report } from "../types";
import { itemsAcc } from "../format";
import {
  APP_BUTTON,
  CHECKOUT_BUTTON,
  DONE_SUBTITLE,
  NO_LINK,
  validationText,
} from "../copy";
import { openExternal } from "../telegram";

/** What actually landed — three shapes, never four: a success title naming every
 * kind of change, the partial «Вийшло не все» (a failed removal styled against the
 * removal panel's own language), and checkout offered only when Silpo itself offers
 * it: both links, or blockers naming why there is none, or no link and no reason. */

export function doneTitle(report: Report): string {
  const added = report.added.length;
  const removed = report.removed.length;

  if (added > 0 && removed > 0) return `Додали ${itemsAcc(added)} і прибрали ${itemsAcc(removed)}`;
  if (added > 0) return `Додали ${itemsAcc(added)} у кошик Сільпо`;
  if (removed > 0) return `Прибрали ${itemsAcc(removed)} з кошика Сільпо`;
  return "Нічого додавати — кошик порожній";
}

export function SyncedScreen({ report }: { report: Report }) {
  const validations = [...new Set(report.blocking_validations)];
  const partial = !report.ok;

  return (
    <section className="screen">
      <h1>{partial ? "Вийшло не все." : doneTitle(report)}</h1>
      <p className="done-subtitle">{DONE_SUBTITLE}</p>

      {!partial && addedLine(report) !== null && <p className="lead">{addedLine(report)}</p>}

      {partial && (
        <>
          {report.added.length > 0 && <p className="lead">Додано: {report.added.join(", ")}</p>}
          {report.removed.length > 0 && <p className="lead">Прибрано: {report.removed.join(", ")}.</p>}
          {report.failed.length > 0 && (
            <div className="plain-block">
              <b>Не вдалося додати:</b>
              <ul className="fail-adds">
                {report.failed.map(([name, error], i) => (
                  <li key={i}>
                    {name} — {error}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {report.remove_failed.length > 0 && (
            <div className="removal-panel" role="alert">
              <h2>
                <i>−</i>Не вдалося прибрати — ці позиції лишилися в кошику:
              </h2>
              <ul>
                {report.remove_failed.map(([name, error], i) => (
                  <li key={i}>
                    <i>−</i>
                    <span>
                      {name} — {error}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="retry-hint">
            Можна спробувати ще раз — повторне надсилання нічого не подвоїть.
          </p>
        </>
      )}

      {/* The blocker panel and the links are alternatives, not companions. */}
      {validations.length > 0 ? (
        <div className="blocker-panel">
          <b>Оформити поки не вийде:</b>
          <ul>
            {validations.map((code) => (
              <li key={code}>{validationText(code)}</li>
            ))}
          </ul>
        </div>
      ) : report.checkout_web_link !== null || report.checkout_mobile_link !== null ? (
        <div className="checkout-actions">
          {report.checkout_web_link !== null && (
            <button
              className="checkout"
              onClick={() => openExternal(report.checkout_web_link!)}
            >
              {CHECKOUT_BUTTON}
            </button>
          )}
          {report.checkout_mobile_link !== null && (
            <button
              className={report.checkout_web_link !== null ? "checkout secondary" : "checkout"}
              onClick={() => openExternal(report.checkout_mobile_link!)}
            >
              {APP_BUTTON}
            </button>
          )}
        </div>
      ) : (report.added.length > 0 || report.removed.length > 0) && (
        <p className="hint">{NO_LINK}</p>
      )}
    </section>
  );
}

function addedLine(report: Report): string | null {
  if (report.added.length > 0) return `Додано ${itemsAcc(report.added.length)} у кошик Сільпо.`;
  if (report.removed.length > 0) return `Прибрано: ${report.removed.join(", ")}.`;
  return null;
}
