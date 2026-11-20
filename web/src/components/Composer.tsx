import { useLayoutEffect, useRef } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { Icon } from './Icon';

const MAX_INPUT_HEIGHT = 220;
const DEFAULT_PLACEHOLDER = 'Message DeepPyro…';

export interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  busy: boolean;
  disabled: boolean;
  placeholder?: string;
  modelSlot?: ReactNode;
  permissionSlot?: ReactNode;
  contextLabel?: string;
  onOpenPalette: () => void;
}

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  busy,
  disabled,
  placeholder,
  modelSlot,
  permissionSlot,
  contextLabel,
  onOpenPalette,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const composingRef = useRef(false);
  const placeholderText = placeholder ?? DEFAULT_PLACEHOLDER;
  const canSend = !disabled && value.trim().length > 0;

  // Auto-grow the textarea from one line up to the fixed maximum height.
  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    const nextHeight = Math.min(textarea.scrollHeight, MAX_INPUT_HEIGHT);
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY = textarea.scrollHeight > MAX_INPUT_HEIGHT ? 'auto' : 'hidden';
  }, [value]);

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    // Enter commits, Shift+Enter inserts a newline, IME confirmation never sends.
    if (composingRef.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (!canSend) return;
    event.preventDefault();
    onSend();
  };

  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          ref={textareaRef}
          className="composer-input"
          value={value}
          rows={1}
          placeholder={placeholderText}
          aria-label={placeholderText}
          spellCheck={false}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          onCompositionStart={() => {
            composingRef.current = true;
          }}
          onCompositionEnd={() => {
            composingRef.current = false;
          }}
        />
        <div className="composer-toolbar">
          <div className="composer-left">
            <button
              className="composer-icon"
              type="button"
              onClick={onOpenPalette}
              title="Commands"
              aria-label="Open commands"
            >
              <Icon name="command" size={15} />
            </button>
            {modelSlot}
            {permissionSlot}
          </div>
          <div className="composer-right">
            {contextLabel ? <span className="composer-context">{contextLabel}</span> : null}
            {busy ? (
              <button
                className="composer-stop"
                type="button"
                onClick={onStop}
                title="Stop generating"
                aria-label="Stop generating"
              >
                <Icon name="stop" size={16} />
              </button>
            ) : null}
            <button
              className={`composer-send ${busy && canSend ? 'queue' : ''}`}
              type="button"
              onClick={onSend}
              disabled={!canSend}
              title={busy ? 'Add to queue — it runs after the current turn' : 'Send'}
              aria-label={busy ? 'Add message to queue' : 'Send message'}
            >
              <Icon name="send" size={16} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
