import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent, MouseEvent } from 'react';
import { Icon } from './Icon';
import { DirectoryPicker } from './DirectoryPicker';
import './new-session.css';

const FALLBACK_PRESET = 'standard';

export interface NewSessionProps {
  open: boolean;
  defaultCwd: string;
  workspaceMode?: boolean;
  recent: string[];
  presets: { id: string; name: string; summary?: string }[];
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onCreate: (input: { cwd: string; title: string; preset: string }) => void;
}

/** Split an absolute path into parent + final segment so the folder name always stays visible. */
function folderParts(path: string): { parent: string; name: string } {
  const text = path.replace(/[\\/]+$/, '');
  if (!text) return { parent: '', name: path };
  const match = /^(.*[\\/])([^\\/]+)$/.exec(text);
  if (!match) return { parent: '', name: text };
  return { parent: match[1], name: match[2] };
}

function sameFolder(left: string, right: string): boolean {
  return left.replace(/[\\/]+$/, '') === right.replace(/[\\/]+$/, '');
}

export function NewSession({
  open,
  defaultCwd,
  workspaceMode = false,
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
  const [pickerOpen, setPickerOpen] = useState(false);
  const chooseRef = useRef<HTMLButtonElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  const dialogTitleId = useId();
  const folderLabelId = useId();
  const titleId = useId();
  const presetId = useId();
  const presetSummaryId = useId();
  const firstPreset = presets[0]?.id ?? FALLBACK_PRESET;

  // Every open starts from the defaults.
  useLayoutEffect(() => {
    if (!open) return;
    setCwd(defaultCwd);
    setTitle('');
    setPreset(firstPreset);
    setPickerOpen(false);
  }, [open, defaultCwd, firstPreset]);

  // Land on the first thing worth doing: title when a folder is ready, folder otherwise.
  useEffect(() => {
    if (!open) return;
    const handle = window.setTimeout(() => {
      if (defaultCwd) titleRef.current?.focus();
      else chooseRef.current?.focus();
    }, 0);
    return () => window.clearTimeout(handle);
  }, [open, defaultCwd]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      // The folder picker owns Escape while it is open.
      if (pickerOpen) return;
      onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [open, pickerOpen, onClose]);

  if (!open) return null;

  const folder = cwd.trim();
  const parts = folder ? folderParts(folder) : null;
  const usesDefaultFolder = Boolean(defaultCwd) && sameFolder(folder, defaultCwd);
  const activePreset = presets.some((item) => item.id === preset) ? preset : firstPreset;
  const activeSummary = presets.find((item) => item.id === activePreset)?.summary;
  const presetOptions = presets.length > 0 ? presets : [{ id: FALLBACK_PRESET, name: 'Standard' }];
  const recentFolders = recent
    .map((dir) => dir.trim())
    .filter((dir, index, all) => dir.length > 0 && all.indexOf(dir) === index)
    .slice(0, 5);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || !folder) return;
    onCreate({ cwd: folder, title: title.trim(), preset: activePreset });
  };

  const handleBackdrop = (event: MouseEvent<HTMLDivElement>) => {
    if (busy || pickerOpen) return;
    if (event.target !== event.currentTarget) return;
    onClose();
  };

  return (
    <div className="ns-backdrop" role="presentation" onMouseDown={handleBackdrop}>
      <form
        className="ns-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={dialogTitleId}
        aria-busy={busy}
        onSubmit={submit}
        autoComplete="off"
      >
        <div className="ns-head">
          <h2 className="ns-title" id={dialogTitleId}>
            {workspaceMode ? 'Change working directory' : 'New session'}
          </h2>
          <button
            className="icon-button subtle ns-close"
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label="Close"
            title="Close"
          >
            <Icon name="x" size={16} />
          </button>
        </div>

        <div className="ns-body">
          <p className="ns-lead">
            {workspaceMode
              ? 'Start a new session in the folder you pick. Your current session keeps working where it is.'
              : 'Pick the folder this session will work in. Each session keeps its own working directory.'}
          </p>

          <div className="ns-field">
            <span className="ns-label" id={folderLabelId}>
              Folder
            </span>
            <div className="ns-folder" role="group" aria-labelledby={folderLabelId}>
              <Icon name="folder" size={15} className="ns-folder-icon" />
              {parts ? (
                <span className="ns-folder-path" title={folder}>
                  {parts.parent ? <span className="ns-folder-parent">{parts.parent}</span> : null}
                  <span className="ns-folder-name">{parts.name}</span>
                </span>
              ) : (
                <span className="ns-folder-path is-empty">No folder selected</span>
              )}
              <button
                ref={chooseRef}
                className="button ghost small ns-choose"
                type="button"
                onClick={() => setPickerOpen(true)}
                disabled={busy}
              >
                Choose…
              </button>
            </div>
            <div className="ns-folder-meta">
              {defaultCwd ? (
                usesDefaultFolder ? (
                  <span className="ns-folder-note">
                    <Icon name="check" size={12} /> Current workspace
                  </span>
                ) : (
                  <button className="ns-link" type="button" onClick={() => setCwd(defaultCwd)} disabled={busy}>
                    Use current workspace
                  </button>
                )
              ) : null}
              {!folder ? <span className="ns-folder-required">Pick a folder to continue</span> : null}
            </div>
          </div>

          {recentFolders.length > 0 ? (
            <div className="ns-recent">
              <span className="ns-recent-label">Recent folders</span>
              <div className="ns-recent-list" role="group" aria-label="Recent folders">
                {recentFolders.map((dir) => {
                  const recentParts = folderParts(dir);
                  const current = sameFolder(dir, folder);
                  return (
                    <button
                      key={dir}
                      className={`ns-recent-item${current ? ' is-current' : ''}`}
                      type="button"
                      title={dir}
                      disabled={busy}
                      onClick={() => setCwd(dir)}
                    >
                      <Icon name="folder" size={14} className="ns-recent-icon" />
                      {recentParts.parent ? <span className="ns-recent-parent">{recentParts.parent}</span> : null}
                      <span className="ns-recent-name">{recentParts.name || dir}</span>
                      {current ? <Icon name="check" size={13} className="ns-recent-check" /> : null}
                    </button>
                  );
                })}
              </div>
            </div>
          ) : null}

          <div className="ns-field">
            <div className="ns-label-row">
              <label className="ns-label" htmlFor={titleId}>
                Title
              </label>
              <span className="ns-label-note">Optional</span>
            </div>
            <input
              ref={titleRef}
              id={titleId}
              className="field-input"
              value={title}
              placeholder="Derived from your first message"
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
              onChange={(event) => setTitle(event.target.value)}
            />
          </div>

          <div className="ns-field">
            <label className="ns-label" htmlFor={presetId}>
              Preset
            </label>
            <select
              id={presetId}
              className="field-select"
              value={activePreset}
              disabled={busy}
              aria-describedby={presetSummaryId}
              onChange={(event) => setPreset(event.target.value)}
            >
              {presetOptions.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
            <span className="ns-hint" id={presetSummaryId}>
              {activeSummary ?? 'Presets choose the model, permissions, and tools for this session.'}
            </span>
          </div>

          {error ? (
            <div className="ns-error" role="alert">
              <Icon name="alert" size={14} className="ns-error-icon" />
              <span>{error}</span>
            </div>
          ) : null}
        </div>

        <div className="ns-actions">
          <button className="button ghost" type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            className={`button primary ns-submit${busy ? ' is-busy' : ''}`}
            type="submit"
            disabled={busy || !folder}
            aria-busy={busy}
          >
            {busy ? (
              <>
                <Icon name="loader" size={14} className="ns-spinner" />
                Starting…
              </>
            ) : workspaceMode ? (
              'Start session here'
            ) : (
              'Start session'
            )}
          </button>
        </div>

        <DirectoryPicker
          open={pickerOpen}
          initialPath={folder || null}
          onClose={() => setPickerOpen(false)}
          onSelect={(picked) => {
            setCwd(picked);
            setPickerOpen(false);
          }}
        />
      </form>
    </div>
  );
}
