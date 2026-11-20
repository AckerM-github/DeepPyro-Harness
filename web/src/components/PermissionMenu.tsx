import { useEffect, useRef, useState } from 'react';
import type { PermissionPreset } from '../types';
import { Icon } from './Icon';

export interface PermissionMenuProps {
  current: string;
  presets: PermissionPreset[];
  disabled?: boolean;
  onSelect: (preset: string) => void;
}

/** Composer-level preset switcher: the risk level belongs next to the input. */
export function PermissionMenu({ current, presets, disabled, onSelect }: PermissionMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const active = presets.find((preset) => preset.name === current);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && event.target instanceof Node && !rootRef.current.contains(event.target)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  return (
    <div className="composer-menu" ref={rootRef}>
      <button
        className="composer-chip"
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        title={active?.description ?? 'Permission preset'}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="key" size={12} />
        {current || 'permission'}
        <Icon name="chevron-down" size={12} className={open ? 'rotated' : ''} />
      </button>
      {open ? (
        <div className="composer-popover" role="listbox" aria-label="Permission preset">
          {presets.map((preset) => (
            <button
              key={preset.name}
              className={`composer-menu-item ${preset.name === current ? 'active' : ''}`}
              type="button"
              role="option"
              aria-selected={preset.name === current}
              onClick={() => {
                setOpen(false);
                onSelect(preset.name);
              }}
            >
              <span className="composer-menu-copy">
                <span className="composer-menu-title">{preset.name}</span>
                <span className="composer-menu-desc">{preset.description}</span>
              </span>
              {preset.name === current ? <Icon name="check" size={14} /> : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
