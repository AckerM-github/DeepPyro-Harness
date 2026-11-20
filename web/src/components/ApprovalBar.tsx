import { useEffect, useRef } from 'react';
import type { Approval } from '../types';
import { Icon } from './Icon';
import './panels.css';

const TOOL_LABELS: Record<string, string> = {
  shell: 'Run a command',
  bash: 'Run a command',
  pwsh: 'Run a command',
  terminal: 'Run a command',
  command: 'Run a command',
  write: 'Edit a file',
  edit: 'Edit a file',
  apply_patch: 'Edit a file',
  read: 'Read a file',
  web_search: 'Use the web',
  web_fetch: 'Use the web',
};

function titleCase(value: string): string {
  return value
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

function toolLabel(tool: string): string {
  const name = tool.trim().toLowerCase();
  const known = TOOL_LABELS[name];
  if (known) return known;
  if (name.startsWith('web_')) return 'Use the web';
  const tail = name.includes('__') ? name.split('__').filter(Boolean).pop() ?? name : name;
  return titleCase(tail) || 'Use a tool';
}

export interface ApprovalBarProps {
  approval: Approval;
  onDecide: (id: string, decision: 'allow' | 'deny', scope: 'once' | 'session') => void;
}

export function ApprovalBar({ approval, onDecide }: ApprovalBarProps) {
  const primaryRef = useRef<HTMLButtonElement>(null);
  const summary = (approval.summary ?? '').trim();
  const detail = (approval.detail ?? '').trim();
  const label = toolLabel(approval.tool);

  // A permission request takes priority over the draft: hand it the keyboard.
  useEffect(() => {
    primaryRef.current?.focus({ preventScroll: true });
  }, [approval.approval_id]);

  return (
    <div className="ab-root" role="alertdialog" aria-label={'Permission needed: ' + label}>
      <div className="ab-card">
        <div className="ab-main">
          <div className="ab-head">
            <span className="ab-icon">
              <Icon name="alert" size={15} />
            </span>
            <span className="ab-title">Permission needed</span>
            <span className="ab-tool">{label}</span>
          </div>
          {summary ? <p className="ab-summary">{summary}</p> : null}
          {detail ? <p className="ab-detail">{detail}</p> : null}
        </div>
        <div className="ab-actions">
          <button
            ref={primaryRef}
            className="ab-btn ab-btn-primary"
            type="button"
            onClick={() => onDecide(approval.approval_id, 'allow', 'once')}
          >
            Allow once
          </button>
          <button
            className="ab-btn ab-btn-ghost"
            type="button"
            onClick={() => onDecide(approval.approval_id, 'allow', 'session')}
          >
            Allow for this session
          </button>
          <button
            className="ab-btn ab-btn-ghost ab-btn-deny"
            type="button"
            onClick={() => onDecide(approval.approval_id, 'deny', 'once')}
          >
            Deny
          </button>
        </div>
      </div>
    </div>
  );
}
