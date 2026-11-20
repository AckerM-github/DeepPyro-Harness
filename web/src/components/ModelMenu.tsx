import { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import './panels.css';

export interface ModelProviderGroup {
  name: string;
  models: string[];
}

export interface ModelMenuProps {
  provider: string | null;
  model: string | null;
  providers: ModelProviderGroup[];
  disabled?: boolean;
  onSelect: (provider: string, model: string) => void;
}

/** Model picker chip for the composer toolbar; the list opens upward. */
export function ModelMenu({ provider, model, providers, disabled, onSelect }: ModelMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && event.target instanceof Node && !rootRef.current.contains(event.target)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setOpen(false);
      triggerRef.current?.focus({ preventScroll: true });
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const currentModel = model && model.trim() ? model : 'Choose model';
  const totalModels = providers.reduce((count, group) => count + group.models.length, 0);

  const choose = (nextProvider: string, nextModel: string) => {
    setOpen(false);
    onSelect(nextProvider, nextModel);
    triggerRef.current?.focus({ preventScroll: true });
  };

  return (
    <div className="mm-root" ref={rootRef}>
      <button
        ref={triggerRef}
        className="mm-trigger"
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={model ? 'Model: ' + model : 'Choose a model'}
        title={provider ? provider + ' · ' + currentModel : currentModel}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="mm-trigger-name">{currentModel}</span>
        <Icon name="chevron-down" size={12} className={open ? 'mm-chevron mm-chevron-open' : 'mm-chevron'} />
      </button>
      {open ? (
        <div className="mm-popover" role="listbox" aria-label="Choose a model">
          {totalModels === 0 ? <div className="mm-empty">No models available for this session.</div> : null}
          {providers.map((group) => (
            <div className="mm-group" key={group.name}>
              <div className="mm-group-label">{group.name}</div>
              {group.models.map((option) => {
                const selected = group.name === provider && option === model;
                return (
                  <button
                    key={group.name + '::' + option}
                    className={selected ? 'mm-item mm-item-selected' : 'mm-item'}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    onClick={() => choose(group.name, option)}
                  >
                    <span className="mm-item-name">{option}</span>
                    {selected ? <Icon name="check" size={14} className="mm-item-check" /> : null}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
