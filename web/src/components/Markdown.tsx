import { useRef, useState, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Icon } from './Icon';

function languageOf(children: ReactNode): string {
  if (children !== null && typeof children === 'object' && 'props' in children) {
    const props = (children as { props?: { className?: unknown } }).props;
    if (props && typeof props.className === 'string') {
      const match = /language-([\w-]+)/.exec(props.className);
      if (match) return match[1];
    }
  }
  return 'text';
}

function CodeBlock({ children }: { children?: ReactNode }) {
  const ref = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);
  const language = languageOf(children);
  const copy = () => {
    const text = ref.current?.textContent ?? '';
    void navigator.clipboard
      ?.writeText(text)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
      })
      .catch(() => undefined);
  };
  return (
    <div className="code-block">
      <div className="code-block-bar">
        <span>{language}</span>
        <button type="button" onClick={copy} className="code-copy">
          <Icon name={copied ? 'check' : 'copy'} size={13} />
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre ref={ref}>{children}</pre>
    </div>
  );
}

// Model output is untrusted. react-markdown escapes raw HTML by default and
// skipHtml drops it instead of rendering; the element filter is defense in
// depth for a future rehype plugin. A reply therefore cannot instantiate a
// form/button/iframe/style/script or otherwise forge an approval UI.
const disallowedElements = [
  'base',
  'button',
  'embed',
  'form',
  'frame',
  'frameset',
  'iframe',
  'link',
  'meta',
  'object',
  'script',
  'style',
];

const components: Components = {
  pre({ children }) {
    return <CodeBlock>{children}</CodeBlock>;
  },
  a({ href, children }) {
    return (
      <a href={href} target="_blank" rel="noreferrer noopener">
        {children}
      </a>
    );
  },
  table({ children }) {
    return (
      <div className="table-wrap">
        <table>{children}</table>
      </div>
    );
  },
  img({ src, alt }) {
    return <img className="markdown-image" src={src} alt={alt ?? ''} loading="lazy" />;
  },
};

export function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={components}
        skipHtml
        disallowedElements={disallowedElements}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
