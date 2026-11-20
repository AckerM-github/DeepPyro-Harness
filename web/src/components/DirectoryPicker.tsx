import { Fragment, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { MouseEvent, ReactNode } from 'react';
import { api } from '../api';
import type { FsDirsPayload, FsRoot } from '../types';
import { Icon } from './Icon';
import './new-session.css';

export interface DirectoryPickerProps {
  open: boolean;
  /** Folder the picker opens in. When empty the picker starts from the filesystem roots. */
  initialPath: string | null;
  onClose: () => void;
  onSelect: (path: string) => void;
}

type LoadStatus = 'loading' | 'ready' | 'error';

interface Crumb {
  label: string;
  path: string | null;
}

function describeError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return 'The folder could not be read.';
}

function separatorOf(path: string): string {
  return path.includes('\\') ? '\\' : '/';
}

/** Parent folder for the up button; null means the root list is the next step up. */
function parentOf(path: string): string | null {
  const trimmed = path.replace(/[\\/]+$/, '');
  if (!trimmed) return null;
  const index = Math.max(trimmed.lastIndexOf('\\'), trimmed.lastIndexOf('/'));
  if (index < 0) return null;
  if (index === 0) return path.slice(0, 1);
  const parent = trimmed.slice(0, index);
  // Keep a Windows drive root as "C:\" rather than "C:".
  if (/^[A-Za-z]:$/.test(parent)) return parent + separatorOf(path);
  // A bare UNC host ("\\server") is not a folder.
  if (/^\\\\[^\\/]+$/.test(parent)) return null;
  return parent;
}

/** Clickable path segments, root first. */
function crumbsOf(path: string): Crumb[] {
  const separator = separatorOf(path);
  const crumbs: Crumb[] = [];
  let rest = path;
  let prefix = '';

  const drive = /^([A-Za-z]:)[\\/]?/.exec(path);
  if (drive) {
    prefix = drive[1];
    rest = path.slice(drive[0].length);
    crumbs.push({ label: prefix, path: prefix + separator });
  } else if (path.startsWith('\\\\')) {
    const unc = /^(\\\\[^\\/]+[\\/][^\\/]+)[\\/]?/.exec(path);
    if (unc) {
      prefix = unc[1];
      rest = path.slice(unc[0].length);
      crumbs.push({ label: prefix, path: prefix + separator });
    }
  } else if (path.startsWith('/')) {
    prefix = '/';
    rest = path.slice(1);
    crumbs.push({ label: '/', path: '/' });
  }

  let current = prefix ? (prefix.endsWith(separator) ? prefix : prefix + separator) : '';
  for (const part of rest.split(/[\\/]+/).filter(Boolean)) {
    current = current ? (current.endsWith(separator) ? current + part : current + separator + part) : part;
    crumbs.push({ label: part, path: current });
  }
  return crumbs;
}

export function DirectoryPicker({ open, initialPath, onClose, onSelect }: DirectoryPickerProps) {
  const [roots, setRoots] = useState<FsRoot[]>([]);
  const [rootsStatus, setRootsStatus] = useState<LoadStatus>('loading');
  const [rootsError, setRootsError] = useState('');
  const [path, setPath] = useState<string | null>(null);
  const [listing, setListing] = useState<FsDirsPayload | null>(null);
  const [listingStatus, setListingStatus] = useState<LoadStatus>('ready');
  const [listingError, setListingError] = useState('');
  const rootsRequest = useRef(0);
  const listingRequest = useRef(0);
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);
  const titleId = useId();

  const loadRoots = useCallback(async () => {
    const request = rootsRequest.current + 1;
    rootsRequest.current = request;
    setRootsStatus('loading');
    setRootsError('');
    try {
      const payload = await api.fsRoots();
      if (rootsRequest.current !== request) return;
      setRoots(payload.roots ?? []);
      setRootsStatus('ready');
    } catch (error) {
      if (rootsRequest.current !== request) return;
      setRootsStatus('error');
      setRootsError(describeError(error));
    }
  }, []);

  const navigate = useCallback(async (target: string) => {
    const request = listingRequest.current + 1;
    listingRequest.current = request;
    setPath(target);
    setListing(null);
    setListingStatus('loading');
    setListingError('');
    try {
      const payload = await api.fsDirs(target);
      if (listingRequest.current !== request) return;
      setListing(payload);
      setPath(payload.path || target);
      setListingStatus('ready');
    } catch (error) {
      if (listingRequest.current !== request) return;
      setListingStatus('error');
      setListingError(describeError(error));
    }
  }, []);

  const showRoots = useCallback(() => {
    listingRequest.current += 1;
    setPath(null);
    setListing(null);
    setListingStatus('ready');
    setListingError('');
    void loadRoots();
  }, [loadRoots]);

  // Reset and load before the first painted frame, so a reopened picker never
  // flashes the folder list from its previous visit.
  useLayoutEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    rootsRequest.current += 1;
    listingRequest.current += 1;
    setRoots([]);
    setRootsStatus('loading');
    setRootsError('');
    setPath(null);
    setListing(null);
    setListingStatus('ready');
    setListingError('');
    const initial = initialPath?.trim();
    if (initial) void navigate(initial);
    else void loadRoots();
    const handle = window.setTimeout(() => closeRef.current?.focus(), 0);
    return () => {
      window.clearTimeout(handle);
      const previous = restoreRef.current;
      restoreRef.current = null;
      previous?.focus();
    };
  }, [open, initialPath, loadRoots, navigate]);

  // Capture phase: Escape closes the picker only, never the dialog underneath.
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };
    window.addEventListener('keydown', handleKeyDown, true);
    return () => window.removeEventListener('keydown', handleKeyDown, true);
  }, [open, onClose]);

  if (!open) return null;

  const isRootsView = path === null;
  const loading = isRootsView ? rootsStatus === 'loading' : listingStatus === 'loading';
  const failed = isRootsView ? rootsStatus === 'error' : listingStatus === 'error';
  const errorText = isRootsView ? rootsError : listingError;
  const entries = listing?.entries ?? [];
  const parentTarget = path ? listing?.parent || parentOf(path) : null;
  const canGoUp = Boolean(path);
  const canSelect = Boolean(path) && listingStatus === 'ready' && !loading;
  const crumbs = path ? crumbsOf(path) : [];
  const currentCrumb = crumbs.length > 0 ? crumbs[crumbs.length - 1] : null;
  const shownCrumbs: Array<Crumb | null> = crumbs.length > 4 ? [crumbs[0], null, ...crumbs.slice(-2)] : crumbs;
  const separator = path ? separatorOf(path) : '/';

  const goUp = () => {
    if (!path || loading) return;
    if (parentTarget) void navigate(parentTarget);
    else showRoots();
  };

  const select = () => {
    if (!path || !canSelect) return;
    onSelect(path);
  };

  const retry = () => {
    if (isRootsView) void loadRoots();
    else if (path) void navigate(path);
  };

  const handleBackdrop = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    onClose();
  };

  const renderList = (): ReactNode => {
    if (loading) {
      return (
        <div className="dpick-skeleton" aria-hidden="true">
          <div className="dpick-skeleton-row" />
          <div className="dpick-skeleton-row" />
          <div className="dpick-skeleton-row" />
          <div className="dpick-skeleton-row" />
        </div>
      );
    }
    if (failed) {
      return (
        <div className="dpick-error" role="alert">
          <Icon name="alert" size={20} className="dpick-error-icon" />
          <span className="dpick-error-title">Couldn’t open this folder</span>
          <span className="dpick-error-detail">{errorText}</span>
          <button className="button ghost small" type="button" onClick={retry}>
            <Icon name="refresh" size={14} />
            Try again
          </button>
        </div>
      );
    }
    if (isRootsView) {
      if (roots.length === 0) return <div className="dpick-empty">No folders available.</div>;
      return roots.map((root) => (
        <button
          key={root.path}
          className="dpick-row"
          type="button"
          title={root.path}
          onClick={() => void navigate(root.path)}
        >
          <Icon name="folder" size={15} className="dpick-row-icon" />
          <span className="dpick-row-name">{root.label || root.path}</span>
          <span className="dpick-row-path">{root.path}</span>
          <Icon name="chevron-right" size={14} className="dpick-row-chevron" />
        </button>
      ));
    }
    if (entries.length === 0) return <div className="dpick-empty">No folders inside this one.</div>;
    return entries.map((entry) => (
      <button
        key={entry.path}
        className="dpick-row"
        type="button"
        title={entry.path}
        onClick={() => void navigate(entry.path)}
      >
        <Icon name="folder" size={15} className="dpick-row-icon" />
        <span className="dpick-row-name">{entry.name}</span>
        <Icon name="chevron-right" size={14} className="dpick-row-chevron" />
      </button>
    ));
  };

  return (
    <div className="dpick-backdrop" role="presentation" onMouseDown={handleBackdrop}>
      <div className="dpick-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="dpick-head">
          <h2 className="dpick-title" id={titleId}>
            Choose a folder
          </h2>
          <button
            ref={closeRef}
            className="icon-button subtle dpick-close"
            type="button"
            onClick={onClose}
            aria-label="Close folder picker"
            title="Close"
          >
            <Icon name="x" size={16} />
          </button>
        </div>

        <div className="dpick-nav">
          <button
            className="dpick-up"
            type="button"
            onClick={goUp}
            disabled={!canGoUp}
            aria-label="Go up one level"
            title="Up one level"
          >
            <Icon name="chevron-right" size={16} className="dpick-up-icon" />
          </button>
          {isRootsView ? (
            <span className="dpick-crumbs-label">Folders</span>
          ) : (
            <nav className="dpick-crumbs" aria-label="Folder path">
              {shownCrumbs.map((crumb, index) => {
                const gap = index > 0 ? (
                  <span className="dpick-sep" aria-hidden="true" key={`sep-${index}`}>
                    {separator}
                  </span>
                ) : null;
                if (!crumb) {
                  return (
                    <Fragment key={`gap-${index}`}>
                      {gap}
                      <span className="dpick-more">…</span>
                    </Fragment>
                  );
                }
                if (crumb === currentCrumb) {
                  return (
                    <Fragment key={crumb.path ?? crumb.label}>
                      {gap}
                      <span className="dpick-crumb is-current">{crumb.label}</span>
                    </Fragment>
                  );
                }
                return (
                  <Fragment key={crumb.path ?? crumb.label}>
                    {gap}
                    <button
                      className="dpick-crumb"
                      type="button"
                      title={crumb.path ?? crumb.label}
                      onClick={() => {
                        if (crumb.path) void navigate(crumb.path);
                      }}
                    >
                      {crumb.label}
                    </button>
                  </Fragment>
                );
              })}
            </nav>
          )}
        </div>

        <div
          className="dpick-list"
          role="group"
          aria-label={isRootsView ? 'Available folders' : 'Folders in this location'}
          aria-busy={loading}
        >
          {renderList()}
        </div>

        <div className="dpick-foot">
          <span className={`dpick-current${path ? '' : ' is-empty'}`} title={path ?? undefined}>
            {path ? (
              <>
                <Icon name="folder" size={13} />
                <span className="dpick-current-text">{path}</span>
              </>
            ) : (
              'No folder selected'
            )}
          </span>
          <div className="dpick-actions">
            <button className="button ghost small" type="button" onClick={onClose}>
              Cancel
            </button>
            <button className="button primary small" type="button" onClick={select} disabled={!canSelect}>
              Select this folder
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}