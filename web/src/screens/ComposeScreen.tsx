/** The entry screen. The bot is still where conversation lives; this is the minimum
 * a Mini App needs to start a basket on its own. The primary action belongs to the
 * native MainButton (or the fallback bar outside Telegram) — not to a button here. */
export function ComposeScreen({
  busy,
  text,
  onText,
  onSubmit,
}: {
  busy: boolean;
  text: string;
  onText: (value: string) => void;
  onSubmit: (value: string) => void;
}) {
  const canSend = text.trim().length > 0 && !busy;

  return (
    <form
      className="screen compose"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSend) onSubmit(text.trim());
      }}
    >
      <div className="wordmark">Комора</div>
      <p className="lead">Кошик для «Сільпо» зі звичайного повідомлення.</p>

      <textarea
        value={text}
        onChange={(event) => onText(event.target.value)}
        placeholder="Що потрібно купити?"
        rows={3}
        // `handlers.MAX_TEXT`: the backend refuses a longer turn in words. Stopping
        // the paste here saves a round trip that could only ever say «задовге».
        maxLength={4096}
        aria-label="Що потрібно купити"
      />
      <div className="hint">Наприклад: молоко, хліб і щось до чаю</div>

      <footer className="trust">
        Чернетка живе в Коморі, поки ви не підтвердите — у кошику Сільпо нічого не
        зміниться, поки ви не скажете «надіслати».
      </footer>
    </form>
  );
}
