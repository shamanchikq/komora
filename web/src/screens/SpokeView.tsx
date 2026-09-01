/** Prose outcomes: answers, prompts, refusals — the same words the chat would say.
 *
 * `needs_link` deserves its honesty stated plainly: account linking starts in the bot
 * chat, because the bot is the surface that already knows who is asking (see
 * api/app.py). The Mini App points there rather than pretending to link. */
export function SpokeView({ text, needsLink }: { text: string; needsLink: boolean }) {
  return (
    <section className="screen">
      <p className="lead spoken">{text}</p>
      {needsLink && (
        <div className="hint">
          Підключення акаунта Сільпо відбувається в чаті з ботом — відкрийте «Комору»
          у Telegram і надішліть будь-яке повідомлення.
        </div>
      )}
    </section>
  );
}
