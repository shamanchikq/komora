import type { Line } from "../types";
import { uah } from "../format";
import { CANCEL_BUTTON } from "../copy";

/** The picker behind «⇄»: what else Silpo has for one line, all at once.
 *
 * «⇄» could only ever move forward one product. A user who tapped past the one they
 * wanted had to cycle the whole list to reach it again, and every tap was a fresh
 * search — the ranked list was built server-side and thrown away each time. The chat
 * has to keep cycling (a Telegram keyboard cannot carry full product names), so this
 * is the thing the Mini App can do that the chat cannot.
 *
 * The current product is shown at the top, marked and not tappable: replacing is a
 * comparison, and the thing being replaced is half of it.
 */

/** Price as this row states it — per kilogram for a weighted good, where `unit_price`
 * is ₴/kg and a bare number would read as the price of the item. */
function priceLine(line: Line): string {
  const price = line.weighted ? `${uah(line.unit_price)}/кг` : uah(line.unit_price);
  return line.unit !== "" && !line.weighted ? `${line.unit} · ${price}` : price;
}

function Option({
  line,
  onPick,
  busy,
}: {
  line: Line;
  onPick: (() => void) | null;
  busy: boolean;
}) {
  const cheaper = line.old_price !== null && Number(line.old_price) > Number(line.unit_price);
  // The badge rides INSIDE the card, so every row is the same width and the current
  // pick reads as one of the set rather than as something bolted beside it.
  const body = (
    <>
      <span className="alt-head">
        <span className="alt-name">{line.name}</span>
        {onPick === null && <span className="alt-badge">зараз</span>}
      </span>
      <span className="alt-meta mono">
        {priceLine(line)}
        {cheaper && <s style={{ marginLeft: 6 }}>було {uah(line.old_price!)}</s>}
      </span>
    </>
  );

  if (onPick === null) {
    return (
      <li className="alt-row alt-current">
        <div className="alt-body">{body}</div>
      </li>
    );
  }
  return (
    <li className="alt-row">
      <button className="alt-body" disabled={busy} onClick={onPick}>
        {body}
      </button>
    </li>
  );
}

export function AlternativesSheet({
  current,
  options,
  busy,
  onPick,
  onBack,
}: {
  current: Line;
  options: Line[];
  busy: boolean;
  onPick: (productId: string) => void;
  onBack: () => void;
}) {
  return (
    <section className="screen">
      <h1>Інший варіант</h1>
      <p className="lead">
        Замість «{current.description || current.name}». Кількість і причина
        залишаться ті самі — зміниться лише товар.
      </p>

      <ul className="alts">
        <Option line={current} onPick={null} busy={busy} />
        {options.map((line) => (
          <Option
            key={line.product_id}
            line={line}
            busy={busy}
            onPick={() => onPick(line.product_id)}
          />
        ))}
      </ul>

      {options.length === 0 && (
        <p className="empty">
          Інших варіантів Сільпо зараз не пропонує — лишається те, що вже обрано.
        </p>
      )}

      <footer className="trust">
        Вибір змінює лише чернетку — у кошику Сільпо нічого не зміниться, поки ви не
        підтвердите.
      </footer>

      <button className="discard" disabled={busy} onClick={onBack}>
        {CANCEL_BUTTON}
      </button>
    </section>
  );
}
