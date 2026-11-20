import { memo, type ReactNode } from 'react';
import type { PermissionPreset, Timeline } from '../types';
import { formatTokens, sessionTitle } from '../lib/format';
import { Icon } from './Icon';

export interface InspectorProps {
  timeline: Timeline | null;
  permissionPresets: PermissionPreset[];
  onClose: () => void;
  onOpenSettings: () => void;
  onPermission: (preset: string) => void;
}

const SHORTCUTS: Array<{ keys: string; label: string }> = [
  { keys: 'Ctrl N', label: 'New session' },
  { keys: 'Ctrl K', label: 'Command palette' },
  { keys: 'Ctrl ,', label: 'Settings' },
];

function contextPercent(used: number, total: number): number {
  if (!Number.isFinite(used) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.min(100, Math.max(0, (used / total) * 100));
}

function Row({ label, children, title }: { label: string; children: ReactNode; title?: string }) {
  return (
    <div className="inspector-row" title={title}>
      <span className="inspector-label">{label}</span>
      <span className="inspector-value">{children}</span>
    </div>
  );
}

function InspectorBase({
  timeline,
  permissionPresets,
  onClose,
  onOpenSettings,
  onPermission,
}: InspectorProps) {
  const preset = timeline
    ? permissionPresets.find((item) => item.name === timeline.permission_preset) ?? null
    : null;
  const hasCurrent = timeline
    ? permissionPresets.some((item) => item.name === timeline.permission_preset)
    : false;
  const percent = timeline ? contextPercent(timeline.context_tokens, timeline.context_window) : 0;

  return (
    <aside className="inspector" aria-label="Session details">
      <div className="inspector-head">
        <span className="inspector-label">SESSION</span>
        <div className="inspector-head-actions">
          <button
            className="inspector-close"
            type="button"
            onClick={onOpenSettings}
            title="Open settings"
            aria-label="Open settings"
          >
            <Icon name="settings" size={15} />
          </button>
          <button
            className="inspector-close"
            type="button"
            onClick={onClose}
            title="Close details"
            aria-label="Close details"
          >
            <Icon name="x" size={16} />
          </button>
        </div>
      </div>

      <div className="inspector-body">
        {timeline ? (
          <>
            <section className="inspector-section">
              <Row label="TITLE" title={timeline.title}>
                {sessionTitle(timeline.title)}
              </Row>
              <div className="inspector-row">
                <span className="inspector-label">STATE</span>
                <span className={`badge ${timeline.active ? 'live' : 'idle'}`}>
                  {timeline.active ? 'LIVE' : 'IDLE'}
                </span>
              </div>
              <Row label="ID" title={timeline.session_id}>
                {timeline.session_id}
              </Row>
            </section>

            <section className="inspector-section">
              <Row label="MODEL">{timeline.model ?? '—'}</Row>
              <Row label="PROVIDER">{timeline.provider ?? '—'}</Row>
              <Row label="PRESET">{timeline.preset || 'standard'}</Row>
            </section>

            <section className="inspector-section">
              <div className="inspector-label">CONTEXT</div>
              <div className="context-meter">
                <div className="context-fill" style={{ width: `${percent.toFixed(1)}%` }} />
              </div>
              <div className="inspector-row">
                <span className="inspector-label">USED</span>
                <span className="inspector-value">
                  {formatTokens(timeline.context_tokens)} / {formatTokens(timeline.context_window)} ·{' '}
                  {Math.round(percent)}%
                </span>
              </div>
            </section>

            <section className="inspector-section">
              <div className="inspector-label">USAGE</div>
              <div className="usage-grid">
                <div>
                  <span className="inspector-label">INPUT</span>
                  <span className="inspector-value">{formatTokens(timeline.usage.input_tokens)}</span>
                </div>
                <div>
                  <span className="inspector-label">OUTPUT</span>
                  <span className="inspector-value">{formatTokens(timeline.usage.output_tokens)}</span>
                </div>
                <div>
                  <span className="inspector-label">CACHED</span>
                  <span className="inspector-value">{formatTokens(timeline.usage.cached_input_tokens)}</span>
                </div>
              </div>
            </section>

            <section className="inspector-section">
              <div className="inspector-label">ACCESS</div>
              <div className="inspector-row">
                <span className="inspector-label">PERMISSION</span>
                <select
                  className="select"
                  value={timeline.permission_preset}
                  onChange={(event) => onPermission(event.target.value)}
                  aria-label="Permission preset"
                  title={preset?.description}
                >
                  {!hasCurrent ? (
                    <option value={timeline.permission_preset}>{timeline.permission_preset}</option>
                  ) : null}
                  {permissionPresets.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </div>
              <Row label="APPROVAL">{preset?.approval ?? '—'}</Row>
              {preset?.description ? <Row label="DETAIL">{preset.description}</Row> : null}
              <Row label="WORKING DIR" title={timeline.cwd}>
                {timeline.cwd || '—'}
              </Row>
              <div className="settings-actions">
                <button className="button ghost small" type="button" onClick={onOpenSettings}>
                  Manage access
                </button>
              </div>
            </section>

            <section className="inspector-section">
              <div className="inspector-label">SHORTCUTS</div>
              {SHORTCUTS.map((shortcut) => (
                <div className="inspector-row" key={shortcut.keys}>
                  <span className="inspector-label">{shortcut.label}</span>
                  <kbd>{shortcut.keys}</kbd>
                </div>
              ))}
            </section>
          </>
        ) : (
          <section className="inspector-section">
            <div className="inspector-label">No session</div>
            <div className="inspector-row">
              <span className="inspector-value">Select a session to inspect it.</span>
            </div>
          </section>
        )}
      </div>
    </aside>
  );
}

function sameInspector(a: InspectorProps, b: InspectorProps): boolean {
  if (a.permissionPresets !== b.permissionPresets) return false;
  const left = a.timeline;
  const right = b.timeline;
  if (!left || !right) return left === right;
  return (
    left.session_id === right.session_id &&
    left.title === right.title &&
    left.active === right.active &&
    left.provider === right.provider &&
    left.model === right.model &&
    left.preset === right.preset &&
    left.permission_preset === right.permission_preset &&
    left.cwd === right.cwd &&
    left.context_tokens === right.context_tokens &&
    left.context_window === right.context_window &&
    left.usage.input_tokens === right.usage.input_tokens &&
    left.usage.output_tokens === right.usage.output_tokens &&
    left.usage.cached_input_tokens === right.usage.cached_input_tokens
  );
}

export const Inspector = memo(InspectorBase, sameInspector);
