import type { QueuedMessage } from '../types';
import { Icon } from './Icon';
import './panels.css';

export interface QueueDockProps {
  items: QueuedMessage[];
  onCancel: (queueId: string) => void;
}

/** Messages waiting behind the running turn, as a quiet drawer above the composer. */
export function QueueDock({ items, onCancel }: QueueDockProps) {
  if (items.length === 0) return null;

  return (
    <div className="qd-root">
      <div className="qd-head">
        <span className="qd-label">Queued · {items.length}</span>
      </div>
      <div className="qd-items">
        {items.map((item) => (
          <div className="qd-item" key={item.queue_id}>
            <span className="qd-text" title={item.text}>
              {item.text}
            </span>
            <button
              className="qd-remove"
              type="button"
              aria-label="Remove queued message"
              title="Remove queued message"
              onClick={() => onCancel(item.queue_id)}
            >
              <Icon name="x" size={12} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
