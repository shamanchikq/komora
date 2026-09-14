import type { Habit, HabitsOutcome } from "../types";
import { tileCode } from "./DraftScreen";

/** «Звичні покупки» — Plan 3 Task 6, per `design/2026-09-14-usual-screen.md`.
 *
 * A read-only list of what the engine tracks, one control per row (mute) and one for
 * the screen («Зібрати кошик», the native MainButton). Every sentence on it arrives
 * from the backend as text: the cadence claim, the freshness line and the empty state
 * are the chat's own words, so this file restates no habits rule — the defect class
 * every frontend review so far has found. What it does decide is order. */

export const USUAL_TITLE = "Звичні покупки";
export const MUTE_LABEL = "Не відстежувати";
export const UNMUTE_LABEL = "Відстежувати знову";
export const COUNTER_NOTE = "з прилавка — у кошик Сільпо не додається";
export const USUAL_FOOTER =
  "Комора бачить ваші чеки й замовлення в Сільпо, а не те, що вдома. «Зібрати кошик» " +
  "лише готує чернетку — у кошику Сільпо нічого не зміниться, поки ви не підтвердите.";

export interface HabitGroups {
  due: Habit[];
  tracked: Habit[];
  muted: Habit[];
}

/** Due first, then everything else still tracked, then what the user switched off.
 * `due` is the backend's flag — already false for a lapsed or a muted habit — and the
 * order inside each group is the backend's too (most confident first). */
export function groupHabits(habits: Habit[]): HabitGroups {
  const live = habits.filter((habit) => !habit.muted);
  return {
    due: live.filter((habit) => habit.due),
    tracked: live.filter((habit) => !habit.due),
    muted: habits.filter((habit) => habit.muted),
  };
}

/** Whether «Зібрати кошик» has anything to build from — the chat's `offer_draft`.
 * Only decides whether the button is offered; which habits go in is the backend's. */
export function canBuild(habits: Habit[]): boolean {
  return habits.some((habit) => habit.reorderable && !habit.muted);
}

function HabitRow({
  habit,
  busy,
  onToggle,
}: {
  habit: Habit;
  busy: boolean;
  onToggle: () => void;
}) {
  return (
    <li className={habit.muted ? "row row-muted" : "row"}>
      <div className="tile" aria-hidden>
        {tileCode(habit.name)}
      </div>
      <div className="row-main">
        <div className="row-name">
          <span className="name">{habit.name}</span>
          <button
            className="mute-toggle"
            disabled={busy}
            aria-pressed={habit.muted}
            onClick={onToggle}
          >
            {habit.muted ? UNMUTE_LABEL : MUTE_LABEL}
          </button>
        </div>
        {habit.unit !== "" && <div className="meta-line mono">{habit.unit}</div>}
        <div className={habit.due ? "reason due" : "reason"}>
          <i />
          <span>{habit.sentence}</span>
        </div>
        {!habit.reorderable && <div className="hint">{COUNTER_NOTE}</div>}
      </div>
    </li>
  );
}

function Group({
  title,
  habits,
  busy,
  onToggle,
}: {
  title: string;
  habits: Habit[];
  busy: boolean;
  onToggle: (habit: Habit) => void;
}) {
  if (habits.length === 0) return null;
  return (
    <>
      <h2 className="group-title">{title}</h2>
      <ul className="lines">
        {habits.map((habit) => (
          <HabitRow
            key={habit.product_key}
            habit={habit}
            busy={busy}
            onToggle={() => onToggle(habit)}
          />
        ))}
      </ul>
    </>
  );
}

export function UsualScreen({
  outcome,
  busy,
  onToggle,
}: {
  outcome: HabitsOutcome;
  busy: boolean;
  onToggle: (habit: Habit) => void;
}) {
  const { due, tracked, muted } = groupHabits(outcome.habits);

  return (
    <section className="screen">
      <h1>{USUAL_TITLE}</h1>
      <p className="done-subtitle">{outcome.fresh_text}</p>

      {outcome.habits.length === 0 ? (
        <p className="lead spoken">{outcome.empty_text}</p>
      ) : (
        <>
          <Group title="Вже пора" habits={due} busy={busy} onToggle={onToggle} />
          <Group
            title={due.length > 0 ? "Решта" : "Відстежую"}
            habits={tracked}
            busy={busy}
            onToggle={onToggle}
          />
          <Group title="Не відстежую" habits={muted} busy={busy} onToggle={onToggle} />
          <footer className="trust">{USUAL_FOOTER}</footer>
        </>
      )}
    </section>
  );
}
