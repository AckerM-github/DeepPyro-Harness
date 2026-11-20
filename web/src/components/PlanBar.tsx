import type { GoalState, TimelineState } from '../types';
import { Icon } from './Icon';
import './panels.css';

const PHASE_LABELS: Record<string, string> = {
  active: 'Active',
  paused: 'Paused',
  blocked: 'Blocked',
  complete: 'Complete',
  completed: 'Complete',
  done: 'Complete',
};

function titleCase(value: string): string {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : '';
}

function phaseLabel(phase: string): string {
  const key = phase.trim().toLowerCase();
  return PHASE_LABELS[key] ?? titleCase(key);
}

function phaseClass(phase: string): string {
  const key = phase.trim().toLowerCase();
  if (key === 'active' || key === 'paused' || key === 'blocked' || key === 'complete') {
    return 'pb-phase pb-phase-' + key;
  }
  return 'pb-phase';
}

function blockedMessage(goal: GoalState): string | null {
  const reason = goal.blocked_reason;
  if (reason && typeof reason === 'object' && 'message' in reason) {
    const message = (reason as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message.trim();
  }
  return null;
}

export interface PlanBarProps {
  state: TimelineState | null;
  onOpenDetails: () => void;
}

/** One quiet line above the composer: the live goal, or todo progress. */
export function PlanBar({ state, onOpenDetails }: PlanBarProps) {
  const goal = state?.goal ?? null;
  const objective = (goal?.objective ?? '').trim();
  const todos = (state?.todos ?? []).filter((item) => item.content.trim().length > 0);

  if (goal && objective) {
    const phase = (goal.phase ?? '').trim();
    const blocked = blockedMessage(goal);
    const rounds = typeof state?.rounds_started === 'number' ? state.rounds_started : null;
    const limit =
      typeof goal.max_goal_rounds === 'number' && goal.max_goal_rounds > 0 ? goal.max_goal_rounds : null;
    const progress =
      rounds !== null && limit !== null ? Math.min(1, Math.max(0, rounds / limit)) : null;

    return (
      <button
        className="pb-root"
        type="button"
        onClick={onOpenDetails}
        title={blocked ? objective + ' — ' + blocked : objective}
      >
        <span className="pb-icon">
          <Icon name="sparkles" size={14} />
        </span>
        <span className="pb-text">{objective}</span>
        {phase ? <span className={phaseClass(phase)}>{phaseLabel(phase)}</span> : null}
        {progress !== null ? (
          <span className="pb-meter" aria-hidden="true">
            <span className="pb-meter-fill" style={{ width: Math.round(progress * 100) + '%' }} />
          </span>
        ) : null}
      </button>
    );
  }

  const hasOpenWork = todos.some((item) => item.status === 'in_progress' || item.status === 'pending');
  if (todos.length > 0 && hasOpenWork) {
    const total = todos.length;
    const done = todos.filter((item) => item.status === 'completed').length;
    const current = todos.find((item) => item.status === 'in_progress');
    const progress = total > 0 ? Math.min(1, Math.max(0, done / total)) : 0;
    const countLabel = total === 1 ? done + ' of 1 task done' : done + ' of ' + total + ' tasks done';
    const label = current ? countLabel + ' · current: ' : countLabel;

    return (
      <button className="pb-root" type="button" onClick={onOpenDetails}>
        <span className="pb-icon pb-icon-quiet">
          <Icon name="check" size={14} />
        </span>
        <span className="pb-text">
          {label}
          {current ? <span className="pb-current">{current.content.trim()}</span> : null}
        </span>
        <span className="pb-meter" aria-hidden="true">
          <span className="pb-meter-fill" style={{ width: Math.round(progress * 100) + '%' }} />
        </span>
      </button>
    );
  }

  return null;
}
