import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent, MouseEvent } from 'react';
import { Icon } from './Icon';

const FALLBACK_PRESET = 'standard';

export interface NewSessionProps {
  open: boolean;
  defaultCwd: string;
  recent: string[];
  presets: { name: string; summary?: string }[];
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onCreate: (input: { cwd: string; title: string; preset: string }) => void;
}

export function NewSession({
  open,
  defaultCwd,
  recent,
  presets,
  busy,
  error,
  onClose,
  onCreate,
}: NewSessionProps) {
  const [cwd, setCwd] = useState(defaultCwd);
  const [title, setTitle] = useState('');
  const [preset, setPreset] = useState(FALLBACK_PRESET);
  const cwdRef = useRef<HTMLInputElement>(null);
  const firstPreset = presets[0]?.name ?? FALLBACK_PRESET;

  // Start every open of the dialog from the defaults.
  useLayoutEffect(() => {
    if (!open) return;
    setCwd(defaultCwd);
    setTitle('');
    setPreset(firstPreset);
  }, [open, defaultCwd, firstPreset]);

  useEffect(() => {
    if (!open) return;
    const handle = window.setTimeout(() => {
      cwdRef.current?.focus();
      cwdRef.current?.select();
    }, 0);
    return () => window.clearTimeout(handle);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const presetNames = presets.length > 0 ? presets.map((item) => item.name) : [FALLBACK_PRESET];
  const activePreset = presetNames.includes(preset) ? preset : firstPreset;
  const activeSummary = presets.find((item) => item.name === activePreset)?.summary;

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    onCreate({
      cwd: cwd.trim() || defaultCwd,
      title: title.trim(),
      preset: activePreset,
    });
  };

  const handleBackdrop = (event: MouseEvent<HTMLDivElement>) => {
    if (busy) return;
    if (event.target !== event.currentTarget) return;
    onClose();
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={handleBackdrop}>
      <form
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-session-title"
        onSubmit={submit}
        autoComplete="off"
      >
        <div className="modal-head">
          <h2 className="modal-title" id="new-session-title">
            New session
          </h2>
          <button
            className="icon-button subtle modal-close"
            type="button"
            onClick={onClose}
            title="Close"
            aria-label="Close"
          >
            <Icon name="x" size={16} />
          </button>
        </div>
        <div className="modal-body">
          <label className="field">
            <span className="field-label">Working directory</span>
            <input
              ref={cwdRef}
              className="field-input"
              value={cwd}
              placeholder={defaultCwd}
              spellCheck={false}
              autoComplete="off"
              onChange={(event) => setCwd(event.target.value)}
            />
          </label>
          {recent.length > 0 ? (
            <div className="recent-row">
              {recent.map((dir) => (
                <button
                  key={dir}
                  className="recent-chip"
                  type="button"
                  title={dir}
                  onClick={() => setCwd(dir)}
                >
                  {dir}
                </button>
              ))}
            </div>
          ) : null}
          <label className="field">
            <span className="field-label">Title (optional)</span>
            <input
              className="field-input"
              value={title}
              placeholder="Derived from the first message"
              autoComplete="off"
              onChange={(event) => setTitle(event.target.value)}
            />
          </label>
          <label className="field">
            <span className="field-label">Preset</span>
            <select
              className="field-select"
              value={activePreset}
              onChange={(event) => setPreset(event.target.value)}
            >
              {presetNames.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
            <span className="field-hint">
              {activeSummary ?? 'Presets choose the model, permissions, and tools for the session.'}
            </span>
          </label>
          {error ? (
            <div className="modal-error" role="alert">
              {error}
            </div>
          ) : null}
        </div>
        <div className="modal-actions">
          <button className="button ghost" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="button primary" type="submit" disabled={busy}>
            {busy ? 'Starting…' : 'Start session'}
          </button>
        </div>
      </form>
    </div>
  );
}
