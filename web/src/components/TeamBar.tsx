import { useEffect, useRef, useState } from 'react';
import type { TeamMember, TeamState } from '../types';
import { Icon } from './Icon';
import './team.css';

const MEMBER_PHASE: Record<string, string> = {
  spawning: 'Starting',
  idle: 'Waiting',
  running: 'Working',
  completed: 'Done',
  failed: 'Failed',
  cancelled: 'Stopped',
};

const TASK_STATUS: Record<string, string> = {
  pending: 'To do',
  in_progress: 'In progress',
  blocked: 'Blocked',
  completed: 'Done',
  cancelled: 'Cancelled',
};

// Mirrors LIVE_PHASES on the backend: leaving team mode is refused while one
// of these teammates can still pick up work.
const LIVE_MEMBER_PHASES = ['spawning', 'running', 'idle'];

function memberTone(member: TeamMember): string {
  if (member.error || member.phase === 'failed') return ' failed';
  if (member.phase === 'running' || member.phase === 'spawning') return ' live';
  if (member.phase === 'completed') return ' done';
  return '';
}

function taskTone(status: string): string {
  if (status === 'completed') return ' done';
  if (status === 'in_progress') return ' live';
  if (status === 'blocked') return ' failed';
  return '';
}

function taskRank(status: string): number {
  if (status === 'in_progress') return 0;
  if (status === 'pending') return 1;
  if (status === 'blocked') return 2;
  return 3;
}

type TeamBarProps = {
  state: TeamState | null;
  enabled: boolean;
  busy: boolean;
  pending: boolean;
  onToggle: (enabled: boolean) => void;
};

export function TeamBar({ state, enabled, busy, pending, onToggle }: TeamBarProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  useEffect(() => {
    if (!enabled) setOpen(false);
  }, [enabled]);

  const members = state?.members ?? [];
  const tasks = state?.tasks ?? [];
  const running = members.filter((member) => member.phase === 'running' || member.phase === 'spawning').length;
  const liveMembers = members.filter((member) => LIVE_MEMBER_PHASES.includes(member.phase)).length;
  const doneTasks = tasks.filter((task) => task.status === 'completed').length;
  const openTasks = tasks.filter((task) => task.status === 'pending' || task.status === 'in_progress').length;
  const maxMembers = state?.max_members || 8;
  const live = enabled && Boolean(state?.active) && (running > 0 || openTasks > 0);
  const attached = Boolean(state?.active);

  if (!enabled) {
    return (
      <div className="team-root" ref={rootRef}>
        <button
          className={'team-bar off' + (pending ? ' pending' : '')}
          type="button"
          aria-label="Enable agent team"
          onClick={() => onToggle(true)}
          disabled={pending}
        >
          <span className="team-dot" aria-hidden="true" />
          <span className="team-bar-title">Agent team</span>
          <span className="team-bar-meta">
            {pending ? 'Switching…' : 'Off · run teammates and share tasks'}
          </span>
          <span className="team-bar-spacer" />
          <span className="team-enable">{pending ? 'Switching…' : 'Enable'}</span>
        </button>
      </div>
    );
  }

  const meta: string[] = [`${members.length}/${maxMembers} members`];
  if (running > 0) meta.push(`${running} running`);
  if (tasks.length > 0) meta.push(`${doneTasks}/${tasks.length} tasks`);
  const metaText = pending ? 'Switching…' : !state ? 'Loading members…' : attached ? meta.join(' · ') : 'Preview';

  const turnOffTitle = pending
    ? 'Switching team mode…'
    : busy
      ? 'Wait for the current turn to finish'
      : liveMembers > 0
        ? 'Wait for active teammates to settle'
        : 'Leave team mode for this session';

  const orderedTasks = [...tasks].sort((a, b) => taskRank(a.status) - taskRank(b.status));

  return (
    <div className="team-root" ref={rootRef}>
      <button
        className={'team-bar' + (pending ? ' pending' : '')}
        type="button"
        aria-expanded={open}
        aria-label="Agent team details"
        onClick={() => setOpen((value) => !value)}
      >
        <span className={'team-dot' + (live ? ' live' : '')} aria-hidden="true" />
        <span className="team-bar-title">Agent team</span>
        <span className="team-bar-meta">{metaText}</span>
        <span className="team-bar-spacer" />
        <Icon name="chevron-down" size={13} className={'team-chevron' + (open ? ' open' : '')} />
      </button>

      {open ? (
        <div className="team-pop" role="dialog" aria-label="Agent team">
          <section className="team-block">
            <div className="team-block-head">
              <h3>Members</h3>
              <span>
                {members.length} of {maxMembers}
              </span>
            </div>
            {members.length === 0 ? (
              <p className="team-empty">
                {attached
                  ? 'No teammates yet. Ask the model to split the work so it starts a team.'
                  : 'Send a message to attach this team session and load its teammates.'}
              </p>
            ) : (
              <ul className="team-list">
                {members.map((member) => (
                  <li className="team-member" key={member.id}>
                    <span className={'team-status' + memberTone(member)} aria-hidden="true" />
                    <span className="team-member-main">
                      <span className="team-member-name">{member.name}</span>
                      {member.description ? <span className="team-member-note">{member.description}</span> : null}
                      {member.last_reply ? <span className="team-member-note">{member.last_reply}</span> : null}
                      {member.error ? <span className="team-member-error">{member.error}</span> : null}
                    </span>
                    <span className="team-member-side">
                      <span className="team-member-phase">{MEMBER_PHASE[member.phase] ?? member.phase}</span>
                      {member.model ? <span className="team-member-model">{member.model}</span> : null}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="team-block">
            <div className="team-block-head">
              <h3>Shared tasks</h3>
              <span>{tasks.length === 0 ? 'none yet' : `${doneTasks} of ${tasks.length} done`}</span>
            </div>
            {tasks.length === 0 ? (
              <p className="team-empty">
                {attached
                  ? 'Tasks appear here when the model assigns work to teammates.'
                  : 'Shared tasks load when this session is attached.'}
              </p>
            ) : (
              <ul className="team-list">
                {orderedTasks.map((task) => (
                  <li className="team-task" key={task.id}>
                    <span className={'team-status' + taskTone(task.status)} aria-hidden="true" />
                    <span className="team-task-main">
                      <span className="team-task-subject">{task.subject}</span>
                      {task.owner ? <span className="team-task-owner">@{task.owner}</span> : null}
                    </span>
                    <span className="team-task-status">{TASK_STATUS[task.status] ?? task.status}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <div className="team-foot">
            <button
              className="team-toggle"
              type="button"
              onClick={() => onToggle(false)}
              disabled={pending || busy || liveMembers > 0}
              title={turnOffTitle}
            >
              {pending ? 'Switching…' : 'Turn off agent team'}
            </button>
            {busy || liveMembers > 0 ? (
              <p className="team-note">
                {busy ? 'Wait for the current turn to finish.' : 'Team mode stays on while teammates are active.'}
              </p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
