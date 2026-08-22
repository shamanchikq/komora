/** Formatting, ported line for line from `bot/render.py` and `core/money.py` so both
 * surfaces say amounts the same way. Money stays a string until this file; the one
 * live bug this class of code prevents is «знижка 15.000 ₴». */

export function uah(raw: string): string {
  /** Ukrainian convention (core/money.py:uah): two decimals, comma, sign after.
   *
   * Rounds half-up on the magnitude, which is what `Decimal.quantize(ROUND_HALF_UP)`
   * does on the other side. Slicing the extra digits off instead disagreed with the
   * bot by a cent: a weighted line of 0,25 кг × 19,99 ₴ crosses the wire as "4.9975",
   * which chat calls «5,00 ₴». Cents are counted in BigInt because the whole point of
   * keeping money in a string is never to let it become a float — `1.005 * 100` is
   * 100.49999999999999, and that rounds the wrong way.
   */
  const text = raw.trim();
  const negative = text.startsWith("-");
  const body = negative ? text.slice(1) : text;
  // Anything that is not a plain decimal is shown as it arrived rather than thrown at
  // BigInt. Nothing serialises that way today; a formatter is a bad place to crash.
  if (!/^\d*(?:\.\d*)?$/.test(body)) return `${text} ₴`;

  const dot = body.indexOf(".");
  const whole = (dot === -1 ? body : body.slice(0, dot)) || "0";
  const frac = (dot === -1 ? "" : body.slice(dot + 1)).padEnd(3, "0");
  // The third digit alone decides: the remainder reaches half a cent exactly when it
  // is 5 or more, whatever follows it.
  let cents = BigInt(whole) * 100n + BigInt(frac.slice(0, 2));
  if (Number(frac[2]) >= 5) cents += 1n;

  const sign = negative ? "-" : "";
  return `${sign}${cents / 100n},${String(cents % 100n).padStart(2, "0")} ₴`;
}

export function quantityText(qty: number): string {
  /** Whole numbers stay whole; weights keep their decimals (0,5 кг, not 0,500). */
  let text = qty.toFixed(3);
  if (text.includes(".")) {
    text = text.replace(/0+$/, "").replace(/\.$/, "");
  }
  return text === "" ? "0" : text.replace(".", ",");
}

/** Ukrainian plural: 1 позиція, 2 позиції, 5 позицій, 11 позицій, 21 позиція. */
export function pl(n: number, one: string, few: string, many: string): string {
  const count = Math.abs(Math.trunc(n));
  const mod100 = count % 100;
  const mod10 = count % 10;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

export function items(n: number): string {
  return `${n} ${pl(n, "позиція", "позиції", "позицій")}`;
}

export function itemsAcc(n: number): string {
  /** Accusative — «Додати 3 позиції», «Прибрати 1 позицію». */
  return `${n} ${pl(n, "позицію", "позиції", "позицій")}`;
}

/** Thousands with a thin space, for caps and totals in labels: 2000 → «2 000». */
export function uiInt(n: number): string {
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, "\u2009");
}
