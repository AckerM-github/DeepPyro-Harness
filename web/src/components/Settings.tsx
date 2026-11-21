import { Fragment, useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent } from 'react';
import type { ProviderProbe, SettingsPayload } from '../types';
import { basename, sessionTitle, shortAgo } from '../lib/format';
import { Icon, type IconName } from './Icon';

export interface SettingsProps {
  open: boolean;
  payload: SettingsPayload | null;
  loading: boolean;
  error: string | null;
  busy: boolean;
  probe: ProviderProbe | null;
  onClose: () => void;
  onReload: () => void;
  onUpdateAgent: (patch: {
    provider?: string;
    model?: string;
    temperature?: number;
    max_tokens?: number;
    max_steps?: number;
  }) => void;
  onUpdateProvider: (name: string, patch: { base_url?: string; api_key?: string; api_key_env?: string }) => void;
  onTestProvider: (name: string) => void;
  onUpdatePermission: (preset: string) => void;
  initialSection?: SectionId;
}

export type SectionId =
  | 'provider'
  | 'agent'
  | 'permissions'
  | 'commands'
  | 'plugins'
  | 'sessions'
  | 'web'
  | 'about';

type ProviderConfig = SettingsPayload['provider']['providers'][number];
type AgentConfig = SettingsPayload['agent'];
type PermissionConfig = SettingsPayload['permissions']['presets'][number];

type SaveStage = 'idle' | 'saving' | 'saved' | 'error';

interface SaveOutcome {
  form: string;
  stage: 'saved' | 'error';
  message?: string;
}

interface SaveTracker {
  begin: (form: string) => void;
  stageOf: (form: string) => SaveStage;
  messageOf: (form: string) => string;
}

interface PendingSave {
  form: string;
  payload: SettingsPayload | null;
  sawBusy: boolean;
}

interface PluginItem {
  id: string;
  name: string;
  version: string;
  source: string;
  origin: string;
  description: string;
  enabled: boolean;
  error: string;
  provides: string[];
  state: string;
}

const LOAD_TIMEOUT_MS = 8000;
const SAVED_NOTICE_MS = 3200;
const TEST_TIMEOUT_MS = 30000;

interface SettingsSection {
  id: SectionId;
  label: string;
  icon: IconName;
  group: string;
  description: string;
}

// Grouped so the navigation reads like product areas rather than a flat
// configuration dump. Runtime controls live in the composer; this surface only
// holds defaults.
const SECTIONS: SettingsSection[] = [
  {
    id: 'provider',
    label: 'Providers',
    icon: 'plug',
    group: 'Model',
    description: 'Connections to model providers and the model used when a session starts.',
  },
  {
    id: 'agent',
    label: 'Agent',
    icon: 'sparkles',
    group: 'Model',
    description: 'Sampling and step limits applied to new sessions.',
  },
  {
    id: 'permissions',
    label: 'Permissions',
    icon: 'key',
    group: 'Workspace',
    description: 'What new sessions may read, write, and run without asking.',
  },
  {
    id: 'sessions',
    label: 'Sessions',
    icon: 'message',
    group: 'Workspace',
    description: 'Where sessions are stored and what the runtime reports about them.',
  },
  {
    id: 'commands',
    label: 'Commands',
    icon: 'command',
    group: 'Advanced',
    description: 'Slash commands registered by the runtime. Most actions are also available in the interface.',
  },
  {
    id: 'plugins',
    label: 'Plugins',
    icon: 'terminal',
    group: 'Advanced',
    description: 'Plugins loaded into this runtime and their settings.',
  },
  {
    id: 'web',
    label: 'Web runtime',
    icon: 'globe',
    group: 'Advanced',
    description: 'Listener addresses and runtime facts for this browser session.',
  },
  {
    id: 'about',
    label: 'About',
    icon: 'info',
    group: 'About',
    description: 'Versions, build information, and configuration paths.',
  },
];

function probeLabel(probe: ProviderProbe): string {
  const status = probe.ok === true ? 'Connected' : probe.ok === false ? 'Failed' : (probe.status ?? 'Done');
  const latency = typeof probe.latency_ms === 'number' ? ' · ' + Math.round(probe.latency_ms) + ' ms' : '';
  const detail = probe.message ?? probe.detail ?? '';
  return status + latency + (detail ? ' · ' + detail : '');
}

function probeBadge(probe: ProviderProbe): { className: string; label: string } {
  if (probe.ok === true) return { className: 'badge live', label: 'CONNECTED' };
  if (probe.ok === false) return { className: 'badge idle', label: 'FAILED' };
  return { className: 'badge idle', label: 'RESULT' };
}

function modelsOf(provider: ProviderConfig | null): string[] {
  if (!provider || !Array.isArray(provider.models)) return [];
  return provider.models.map((model) => model.id);
}

function asText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function readPluginItem(raw: Record<string, unknown>): PluginItem {
  const id = asText(raw.id);
  const provides = Array.isArray(raw.provides)
    ? raw.provides.filter((entry): entry is string => typeof entry === 'string' && entry.trim() !== '')
    : [];
  return {
    id,
    name: asText(raw.name) || id,
    version: asText(raw.version),
    source: asText(raw.source),
    origin: asText(raw.origin),
    description: asText(raw.description),
    enabled: raw.enabled === true,
    error: asText(raw.error),
    provides,
    state: asText(raw.state),
  };
}

function pluginStateBadge(item: PluginItem): string {
  const active = item.enabled && (item.state === 'ACTIVE' || item.state === '');
  return 'badge ' + (active ? 'live' : 'idle');
}

function pluginStateLabel(item: PluginItem): string {
  if (item.state) return item.state;
  return item.enabled ? 'ENABLED' : 'DISABLED';
}

function isEnvName(value: string): boolean {
  if (!value) return false;
  const first = value[0];
  const startsOk = (first >= 'A' && first <= 'Z') || (first >= 'a' && first <= 'z') || first === '_';
  if (!startsOk) return false;
  for (let index = 1; index < value.length; index += 1) {
    const char = value[index];
    const letter = (char >= 'A' && char <= 'Z') || (char >= 'a' && char <= 'z');
    const digit = char >= '0' && char <= '9';
    if (!letter && !digit && char !== '_') return false;
  }
  return true;
}

function validateBaseUrl(value: string): string {
  if (!value) return 'Base URL is required.';
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return 'Base URL must start with http:// or https://.';
    }
    if (!parsed.hostname) return 'Base URL must include a host name.';
  } catch {
    return 'Enter a valid http(s) URL, e.g. https://api.example.com/v1.';
  }
  return '';
}

function sessionUpdatedMs(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  return value > 100000000000 ? value : value * 1000;
}

function ProviderCard({
  provider,
  active,
  busy,
  externalBusy,
  tracker,
  onSave,
  onTest,
}: {
  provider: ProviderConfig;
  active: boolean;
  busy: boolean;
  externalBusy: boolean;
  tracker: SaveTracker;
  onSave: SettingsProps['onUpdateProvider'];
  onTest: SettingsProps['onTestProvider'];
}) {
  const fieldId = useId();
  const form = 'provider:' + provider.name;
  const saveStage = tracker.stageOf(form);
  const saveMessage = tracker.messageOf(form);

  const [baseUrl, setBaseUrl] = useState(provider.base_url ?? '');
  const [apiKeyEnv, setApiKeyEnv] = useState(provider.api_key_env ?? '');
  const [apiKey, setApiKey] = useState('');
  const [errors, setErrors] = useState<{ baseUrl?: string; apiKeyEnv?: string }>({});
  const [testing, setTesting] = useState(false);
  const testSawBusy = useRef(false);

  useEffect(() => {
    if (saveStage === 'saved') {
      setApiKey('');
      setErrors({});
    }
  }, [saveStage]);

  useEffect(() => {
    if (!testing) return;
    if (busy) {
      testSawBusy.current = true;
      return;
    }
    if (testSawBusy.current) {
      testSawBusy.current = false;
      setTesting(false);
    }
  }, [testing, busy]);

  useEffect(() => {
    if (!testing) return;
    const timer = window.setTimeout(() => {
      testSawBusy.current = false;
      setTesting(false);
    }, TEST_TIMEOUT_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [testing]);

  const baseUrlId = fieldId + '-base-url';
  const baseUrlErrorId = fieldId + '-base-url-error';
  const baseUrlHintId = fieldId + '-base-url-hint';
  const envId = fieldId + '-api-key-env';
  const envErrorId = fieldId + '-api-key-env-error';
  const envHintId = fieldId + '-api-key-env-hint';
  const keyId = fieldId + '-api-key';
  const keyHintId = fieldId + '-api-key-hint';

  const save = () => {
    const nextBaseUrl = baseUrl.trim();
    const nextApiKeyEnv = apiKeyEnv.trim();
    const nextErrors: { baseUrl?: string; apiKeyEnv?: string } = {};
    const baseUrlError = validateBaseUrl(nextBaseUrl);
    if (baseUrlError) nextErrors.baseUrl = baseUrlError;
    if (nextApiKeyEnv && !isEnvName(nextApiKeyEnv)) {
      nextErrors.apiKeyEnv = 'Use an environment variable name such as PROVIDER_API_KEY.';
    }
    setErrors(nextErrors);
    if (nextErrors.baseUrl || nextErrors.apiKeyEnv) return;
    const patch: { base_url?: string; api_key?: string; api_key_env?: string } = {
      base_url: nextBaseUrl,
      api_key_env: nextApiKeyEnv,
    };
    const nextKey = apiKey.trim();
    if (nextKey) patch.api_key = nextKey;
    tracker.begin(form);
    onSave(provider.name, patch);
  };

  const test = () => {
    if (testing) return;
    testSawBusy.current = false;
    setTesting(true);
    onTest(provider.name);
  };

  const baseUrlError = errors.baseUrl;
  const envError = errors.apiKeyEnv;

  return (
    <form
      className="settings-section"
      aria-label={provider.name + ' provider settings'}
      onSubmit={(event) => {
        event.preventDefault();
        save();
      }}
    >
      <div className="settings-card-head">
        <div className="settings-card-title">
          <h3>{provider.name}</h3>
          <span className={'settings-pill' + (provider.api_key_set ? ' on' : '')}>
            {provider.api_key_set ? 'Key set' : 'No key'}
          </span>
          {active ? <span className="settings-pill">In use</span> : null}
        </div>
        <span className="settings-card-meta">
          {provider.api || 'unknown API'} ·{' '}
          {Array.isArray(provider.models) ? provider.models.length : 0}{' '}
          {Array.isArray(provider.models) && provider.models.length === 1 ? 'model' : 'models'}
        </span>
      </div>
      <label className="field" htmlFor={baseUrlId}>
        <span className="field-label">Base URL</span>
        <input
          id={baseUrlId}
          className="field-input"
          value={baseUrl}
          onChange={(event) => {
            setBaseUrl(event.target.value);
            setErrors((previous) => ({ ...previous, baseUrl: undefined }));
          }}
          placeholder="https://api.example.com/v1"
          spellCheck={false}
          aria-invalid={baseUrlError ? true : undefined}
          aria-describedby={baseUrlError ? baseUrlErrorId : baseUrlHintId}
        />
        {baseUrlError ? (
          <span className="error-text" id={baseUrlErrorId} role="alert">
            {baseUrlError}
          </span>
        ) : (
          <span className="field-hint" id={baseUrlHintId}>
            Provider endpoint; must be an http(s) URL.
          </span>
        )}
      </label>
      <label className="field" htmlFor={envId}>
        <span className="field-label">API key env</span>
        <input
          id={envId}
          className="field-input"
          value={apiKeyEnv}
          onChange={(event) => {
            setApiKeyEnv(event.target.value);
            setErrors((previous) => ({ ...previous, apiKeyEnv: undefined }));
          }}
          placeholder="PROVIDER_API_KEY"
          spellCheck={false}
          aria-invalid={envError ? true : undefined}
          aria-describedby={envError ? envErrorId : envHintId}
        />
        {envError ? (
          <span className="error-text" id={envErrorId} role="alert">
            {envError}
          </span>
        ) : (
          <span className="field-hint" id={envHintId}>
            Environment variable that holds the key; leave blank to clear it.
          </span>
        )}
      </label>
      <label className="field" htmlFor={keyId}>
        <span className="field-label">API key {provider.api_key_set ? '(saved)' : '(not set)'}</span>
        <input
          id={keyId}
          className="field-input"
          type="password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          placeholder={provider.api_key_set ? 'Leave blank to keep the current key' : 'Paste a key'}
          autoComplete="off"
          spellCheck={false}
          aria-describedby={keyHintId}
        />
        <span className="field-hint" id={keyHintId}>
          {provider.api_key_set
            ? 'Entered keys are only sent when you save; canceling keeps the stored key.'
            : 'The key is sent only when you save this provider.'}
        </span>
      </label>
      <div className="settings-actions">
        <button
          className="button primary small"
          type="submit"
          disabled={saveStage === 'saving' || testing || externalBusy}
        >
          {saveStage === 'saving' ? 'Saving…' : 'Save changes'}
        </button>
        <button
          className="button ghost small"
          type="button"
          disabled={busy || testing || saveStage === 'saving'}
          onClick={test}
        >
          {testing ? 'Testing…' : 'Test connection'}
        </button>
        {saveStage === 'saved' ? (
          <span className="field-hint" role="status">
            Saved
          </span>
        ) : null}
        {testing ? (
          <span className="field-hint" role="status">
            Testing provider…
          </span>
        ) : null}
      </div>
      {saveStage === 'error' && saveMessage ? (
        <span className="error-text" role="alert">
          {saveMessage}
        </span>
      ) : null}
    </form>
  );
}

function ProviderSection({
  providers,
  currentProvider,
  currentModel,
  probe,
  busy,
  externalBusy,
  tracker,
  onUpdateAgent,
  onUpdateProvider,
  onTestProvider,
}: {
  providers: ProviderConfig[];
  currentProvider: string;
  currentModel: string;
  probe: ProviderProbe | null;
  busy: boolean;
  externalBusy: boolean;
  tracker: SaveTracker;
  onUpdateAgent: SettingsProps['onUpdateAgent'];
  onUpdateProvider: SettingsProps['onUpdateProvider'];
  onTestProvider: SettingsProps['onTestProvider'];
}) {
  const headingId = useId();
  const routingForm = 'routing';
  const routingStage = tracker.stageOf(routingForm);
  const routingMessage = tracker.messageOf(routingForm);

  const [providerName, setProviderName] = useState(currentProvider);
  const [modelName, setModelName] = useState(currentModel);
  const [routingError, setRoutingError] = useState('');

  const selected = providers.find((provider) => provider.name === providerName) ?? null;
  const models = modelsOf(selected);
  const hasModel = models.includes(modelName);

  const changeProvider = (name: string) => {
    setProviderName(name);
    setRoutingError('');
    const next = providers.find((provider) => provider.name === name) ?? null;
    const nextModels = modelsOf(next);
    setModelName((previous) => (nextModels.includes(previous) ? previous : (nextModels[0] ?? '')));
  };

  const apply = () => {
    if (!providerName) {
      setRoutingError('Choose a provider first.');
      return;
    }
    if (!modelName) {
      setRoutingError('Choose a model for the selected provider.');
      return;
    }
    setRoutingError('');
    tracker.begin(routingForm);
    onUpdateAgent({ provider: providerName, model: modelName });
  };

  return (
    <>
      <form
        className="settings-section settings-card"
        aria-labelledby={headingId + '-title'}
        onSubmit={(event) => {
          event.preventDefault();
          apply();
        }}
      >
        <div className="settings-card-head">
          <div className="settings-card-title">
            <h3 id={headingId + '-title'}>Default model</h3>
          </div>
          <span className="settings-card-meta">
            Used when a session starts. The model chip in the composer changes the current session.
          </span>
        </div>
        <div className="settings-fields">
          <label className="field">
            <span className="field-label">Provider</span>
            <select
              className="field-input"
              value={providerName}
              onChange={(event) => changeProvider(event.target.value)}
            >
              {providers.map((provider) => (
                <option key={provider.name} value={provider.name}>
                  {provider.name}
                </option>
              ))}
              {!providers.some((provider) => provider.name === providerName) ? (
                <option value={providerName}>{providerName || 'No provider'}</option>
              ) : null}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Model</span>
            <select
              className="field-input"
              value={modelName}
              onChange={(event) => {
                setModelName(event.target.value);
                setRoutingError('');
              }}
            >
              {models.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
              {!hasModel ? <option value={modelName}>{modelName || 'No model'}</option> : null}
            </select>
          </label>
        </div>
        {routingError ? (
          <span className="error-text" role="alert">
            {routingError}
          </span>
        ) : null}
        <div className="settings-actions">
          <button
            className="button primary small"
            type="submit"
            disabled={routingStage === 'saving' || externalBusy || !providerName}
          >
            {routingStage === 'saving' ? 'Saving…' : 'Save default'}
          </button>
          {routingStage === 'saved' ? (
            <span className="field-hint" role="status">
              Saved
            </span>
          ) : null}
        </div>
        {routingStage === 'error' && routingMessage ? (
          <span className="error-text" role="alert">
            {routingMessage}
          </span>
        ) : null}
      </form>

      {probe ? (
        <section className="settings-section settings-card" aria-label="Last provider test result">
          <div className="settings-card-head">
            <div className="settings-card-title">
              <h3>Last connection test</h3>
              <span className={probeBadge(probe).className}>{probeBadge(probe).label}</span>
            </div>
            <span className="settings-card-meta">{probeLabel(probe)}</span>
          </div>
        </section>
      ) : null}

      {providers.length === 0 ? (
        <section className="settings-section">
          <div className="settings-row">
            <span className="field-label">No providers were reported by the runtime.</span>
          </div>
        </section>
      ) : null}

      <div className="settings-group-head">
        <h3>Providers</h3>
        <span>Connections the runtime can use for new sessions.</span>
      </div>

      {providers.map((provider) => (
        <ProviderCard
          key={
            provider.name +
            '|' +
            provider.base_url +
            '|' +
            provider.api_key_env +
            '|' +
            (provider.api_key_set ? 'set' : 'unset')
          }
          provider={provider}
          active={provider.name === currentProvider}
          busy={busy}
          externalBusy={externalBusy}
          tracker={tracker}
          onSave={onUpdateProvider}
          onTest={onTestProvider}
        />
      ))}
    </>
  );
}

function AgentSection({
  agent,
  externalBusy,
  tracker,
  onUpdateAgent,
}: {
  agent: AgentConfig;
  externalBusy: boolean;
  tracker: SaveTracker;
  onUpdateAgent: SettingsProps['onUpdateAgent'];
}) {
  const fieldId = useId();
  const form = 'agent';
  const saveStage = tracker.stageOf(form);
  const saveMessage = tracker.messageOf(form);

  const [temperature, setTemperature] = useState(String(agent.temperature));
  const [maxTokens, setMaxTokens] = useState(String(agent.max_tokens));
  const [maxSteps, setMaxSteps] = useState(String(agent.max_steps));
  const [errors, setErrors] = useState<{
    temperature?: string;
    maxTokens?: string;
    maxSteps?: string;
  }>({});

  const apply = () => {
    const nextErrors: { temperature?: string; maxTokens?: string; maxSteps?: string } = {};
    const temperatureValue = Number(temperature);
    if (!temperature.trim() || !Number.isFinite(temperatureValue) || temperatureValue < 0 || temperatureValue > 2) {
      nextErrors.temperature = 'Enter a number between 0 and 2.';
    }
    const tokensValue = Number(maxTokens);
    if (!maxTokens.trim() || !Number.isInteger(tokensValue) || tokensValue < 1) {
      nextErrors.maxTokens = 'Enter a whole number of at least 1.';
    }
    const stepsValue = Number(maxSteps);
    if (!maxSteps.trim() || !Number.isInteger(stepsValue) || stepsValue < 1) {
      nextErrors.maxSteps = 'Enter a whole number of at least 1.';
    }
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;
    const patch: {
      temperature?: number;
      max_tokens?: number;
      max_steps?: number;
    } = {
      temperature: temperatureValue,
      max_tokens: tokensValue,
      max_steps: stepsValue,
    };
    tracker.begin(form);
    onUpdateAgent(patch);
  };

  return (
    <form
      className="settings-section"
      aria-labelledby={fieldId + '-title'}
      onSubmit={(event) => {
        event.preventDefault();
        apply();
      }}
    >
      <div className="settings-card-head">
        <div className="settings-card-title">
          <h3 id={fieldId + '-title'}>Sampling and limits</h3>
        </div>
        <span className="settings-card-meta">
          Chosen provider and model: {agent.provider} · {agent.model}
        </span>
      </div>
      <div className="settings-fields settings-fields-3">
        <label className="field" htmlFor={fieldId + '-temperature'}>
          <span className="field-label">Temperature</span>
          <input
            id={fieldId + '-temperature'}
            className="field-input"
            type="number"
            min={0}
            max={2}
            step={0.05}
            value={temperature}
            onChange={(event) => {
              setTemperature(event.target.value);
              setErrors((previous) => ({ ...previous, temperature: undefined }));
            }}
            aria-invalid={errors.temperature ? true : undefined}
            aria-describedby={errors.temperature ? fieldId + '-temperature-error' : undefined}
          />
          {errors.temperature ? (
            <span className="error-text" id={fieldId + '-temperature-error'} role="alert">
              {errors.temperature}
            </span>
          ) : null}
        </label>
        <label className="field" htmlFor={fieldId + '-max-tokens'}>
          <span className="field-label">Max tokens</span>
          <input
            id={fieldId + '-max-tokens'}
            className="field-input"
            type="number"
            min={1}
            step={1}
            value={maxTokens}
            onChange={(event) => {
              setMaxTokens(event.target.value);
              setErrors((previous) => ({ ...previous, maxTokens: undefined }));
            }}
            aria-invalid={errors.maxTokens ? true : undefined}
            aria-describedby={errors.maxTokens ? fieldId + '-max-tokens-error' : undefined}
          />
          {errors.maxTokens ? (
            <span className="error-text" id={fieldId + '-max-tokens-error'} role="alert">
              {errors.maxTokens}
            </span>
          ) : null}
        </label>
        <label className="field" htmlFor={fieldId + '-max-steps'}>
          <span className="field-label">Max steps</span>
          <input
            id={fieldId + '-max-steps'}
            className="field-input"
            type="number"
            min={1}
            step={1}
            value={maxSteps}
            onChange={(event) => {
              setMaxSteps(event.target.value);
              setErrors((previous) => ({ ...previous, maxSteps: undefined }));
            }}
            aria-invalid={errors.maxSteps ? true : undefined}
            aria-describedby={errors.maxSteps ? fieldId + '-max-steps-error' : undefined}
          />
          {errors.maxSteps ? (
            <span className="error-text" id={fieldId + '-max-steps-error'} role="alert">
              {errors.maxSteps}
            </span>
          ) : null}
        </label>
      </div>
      <div className="settings-actions">
        <button
          className="button primary small"
          type="submit"
          disabled={saveStage === 'saving' || externalBusy}
        >
          {saveStage === 'saving' ? 'Saving…' : 'Save defaults'}
        </button>
        {saveStage === 'saved' ? (
          <span className="field-hint" role="status">
            Saved
          </span>
        ) : null}
      </div>
      {saveStage === 'error' && saveMessage ? (
        <span className="error-text" role="alert">
          {saveMessage}
        </span>
      ) : null}
    </form>
  );
}

function PermissionsSection({
  presets,
  current,
  fallback,
  custom,
  externalBusy,
  tracker,
  onUpdatePermission,
}: {
  presets: PermissionConfig[];
  current: string;
  fallback: string;
  custom: boolean;
  externalBusy: boolean;
  tracker: SaveTracker;
  onUpdatePermission: SettingsProps['onUpdatePermission'];
}) {
  const fieldId = useId();
  const form = 'permissions';
  const saveStage = tracker.stageOf(form);
  const saveMessage = tracker.messageOf(form);
  const [choice, setChoice] = useState(current);

  const apply = () => {
    if (!choice) return;
    tracker.begin(form);
    onUpdatePermission(choice);
  };

  const groupLabelId = fieldId + '-group-label';

  return (
    <form
      className="settings-section"
      aria-labelledby={fieldId + '-title'}
      onSubmit={(event) => {
        event.preventDefault();
        apply();
      }}
    >
      <div className="settings-card-head">
        <div className="settings-card-title">
          <h3 id={fieldId + '-title'}>Default permission</h3>
          <span className={'settings-pill' + (current ? ' on' : '')}>{current || 'default'}</span>
          {custom ? <span className="settings-pill">Custom</span> : null}
        </div>
        <span className="settings-card-meta">Applied when a session starts; the composer changes the current session.</span>
      </div>
      <span className="sr-only" id={groupLabelId}>
        Default permission preset
      </span>
      {presets.length === 0 ? (
        <div className="settings-row">
          <span className="field-label">No permission presets were reported by the runtime.</span>
        </div>
      ) : (
        <div className="settings-choices" role="radiogroup" aria-labelledby={groupLabelId}>
          {presets.map((preset) => (
            <label className={'settings-choice' + (choice === preset.name ? ' selected' : '')} key={preset.name}>
              <input
                type="radio"
                name={fieldId + '-preset'}
                value={preset.name}
                checked={choice === preset.name}
                disabled={saveStage === 'saving'}
                onChange={() => setChoice(preset.name)}
              />
              <span className="settings-choice-body">
                <span className="settings-choice-title">{preset.label ?? preset.name}</span>
                <span className="settings-choice-desc">{preset.description || 'No description reported.'}</span>
                <span className="settings-choice-meta">
                  {preset.sandbox} · {preset.approval}
                </span>
              </span>
            </label>
          ))}
        </div>
      )}
      {fallback ? (
        <div className="settings-row">
          <span className="field-label">Default</span>
          <span>{fallback}</span>
        </div>
      ) : null}
      <div className="settings-actions">
        <button
          className="button primary small"
          type="submit"
          disabled={saveStage === 'saving' || externalBusy || !choice || choice === current}
        >
          {saveStage === 'saving' ? 'Saving…' : 'Save permission'}
        </button>
        {saveStage === 'saved' ? (
          <span className="field-hint" role="status">
            Saved
          </span>
        ) : null}
      </div>
      {saveStage === 'error' && saveMessage ? (
        <span className="error-text" role="alert">
          {saveMessage}
        </span>
      ) : null}
    </form>
  );
}

function CommandsSection({ commands }: { commands: SettingsPayload['commands'] }) {
  const headingId = useId();
  return (
    <section className="settings-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Commands</h3>
      <span className="field-hint">Read-only slash commands registered by the runtime.</span>
      {commands.length === 0 ? (
        <span className="field-hint">No slash commands were reported.</span>
      ) : (
        <div role="list">
          {commands.map((command) => (
            <div className="settings-row" role="listitem" key={command.name}>
              <span className="inline-code">/{command.name}</span>
              <span title={command.usage}>{command.summary || command.usage || '—'}</span>
              {Array.isArray(command.aliases) && command.aliases.length > 0 ? (
                <span>aliases: {command.aliases.join(', ')}</span>
              ) : null}
              {command.plugin ? <span className="badge">{command.plugin}</span> : null}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function PluginsSection({ plugins }: { plugins: SettingsPayload['plugins'] | undefined }) {
  const headingId = useId();
  const revision = plugins?.revision ?? '';
  const items = plugins && Array.isArray(plugins.items) ? plugins.items.map(readPluginItem) : [];
  return (
    <section className="settings-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Plugins</h3>
      <div className="settings-row">
        <span className="field-label">Installed</span>
        <span>{items.length}</span>
        {revision ? <span className="badge">REV {revision}</span> : null}
      </div>
      {items.length === 0 ? (
        <span className="field-hint">No plugins were reported by the runtime.</span>
      ) : (
        <div className="tool-list">
          {items.map((item) => (
            <div
              className="tool-card"
              key={item.id}
              style={{ padding: '9px 11px', display: 'flex', flexDirection: 'column', gap: 5 }}
            >
              <div className="settings-row">
                <span style={{ color: 'var(--text-primary)', fontWeight: 500 }}>{item.name}</span>
                <span className="badge">{item.version ? 'v' + item.version : 'NO VERSION'}</span>
                <span className={pluginStateBadge(item)}>{pluginStateLabel(item)}</span>
                <span className="inline-code" style={{ marginLeft: 'auto' }}>
                  {item.id}
                </span>
              </div>
              {item.description ? <span className="field-hint">{item.description}</span> : null}
              {item.error ? (
                <span className="error-text" role="alert">
                  {item.error}
                </span>
              ) : null}
              {item.source || item.origin || item.provides.length > 0 ? (
                <div className="settings-row">
                  {item.source ? <span>source: {item.source}</span> : null}
                  {item.origin ? <span>origin: {item.origin}</span> : null}
                  {item.provides.length > 0 ? <span>provides: {item.provides.join(', ')}</span> : null}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function SessionsSection({ sessions }: { sessions: SettingsPayload['sessions'] | undefined }) {
  const headingId = useId();
  const items = sessions && Array.isArray(sessions.items) ? sessions.items : [];
  const total = sessions && typeof sessions.count === 'number' ? sessions.count : items.length;
  const activeId = sessions?.active_session_id ?? null;
  return (
    <section className="settings-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Sessions</h3>
      <div className="settings-row">
        <span className="field-label">Directory</span>
        <span className="inline-code" title={sessions?.dir || ''}>
          {sessions?.dir || 'unknown'}
        </span>
      </div>
      <div className="settings-row">
        <span className="field-label">Total</span>
        <span>{total}</span>
        {items.length < total ? <span className="badge">SHOWING {items.length}</span> : null}
      </div>
      {activeId ? (
        <div className="settings-row">
          <span className="field-label">Active</span>
          <span className="inline-code">{activeId}</span>
        </div>
      ) : null}
      {items.length === 0 ? (
        <span className="field-hint">No recent sessions were reported.</span>
      ) : (
        <div className="tool-list">
          {items.map((item) => {
            const updated = sessionUpdatedMs(item.updated_at);
            const active = activeId !== null && item.id === activeId;
            return (
              <div
                className="tool-card"
                key={item.id}
                style={{ padding: '8px 11px', display: 'flex', flexDirection: 'column', gap: 4 }}
              >
                <div className="settings-row">
                  <span style={{ color: 'var(--text-primary)', fontWeight: 500 }}>
                    {sessionTitle(item.title)}
                  </span>
                  {active ? <span className="badge live">ACTIVE</span> : null}
                  {item.preset ? <span className="badge">{item.preset}</span> : null}
                  <span style={{ marginLeft: 'auto' }}>{updated ? shortAgo(updated) : 'unknown'}</span>
                </div>
                <div className="settings-row">
                  <span className="inline-code" title={item.id}>
                    {item.id}
                  </span>
                  <span>{item.turns} turns</span>
                  {item.cwd ? <span title={item.cwd}>in {basename(item.cwd)}</span> : null}
                </div>
                {item.preview ? <span className="field-hint">{item.preview}</span> : null}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function WebSection({ web }: { web: SettingsPayload['web'] | undefined }) {
  const headingId = useId();
  const host = web?.host || '';
  const port = web && typeof web.port === 'number' && web.port > 0 ? web.port : 0;
  const url = host && port ? 'http://' + host + ':' + port : '';
  return (
    <section className="settings-section" aria-labelledby={headingId}>
      <h3 id={headingId}>Web</h3>
      <div className="settings-row">
        <span className="field-label">Host</span>
        <span>{host || 'unknown'}</span>
      </div>
      <div className="settings-row">
        <span className="field-label">Port</span>
        <span>{port ? String(port) : 'unknown'}</span>
      </div>
      {url ? (
        <div className="settings-row">
          <span className="field-label">Local URL</span>
          <span className="inline-code">{url}</span>
        </div>
      ) : null}
      <span className="field-hint">The web host binds to loopback; changing the listener requires restarting the runtime.</span>
    </section>
  );
}

function AboutSection({ payload }: { payload: SettingsPayload }) {
  const headingId = useId();
  const rows: Array<{ label: string; value: string }> = [];
  const about = payload.about as SettingsPayload['about'] | undefined;
  if (about) {
    rows.push({ label: 'Version', value: about.version || 'unknown' });
    rows.push({ label: 'Python', value: about.python || 'unknown' });
    rows.push({ label: 'Platform', value: about.platform || 'unknown' });
    rows.push({ label: 'Plugins', value: String(about.plugins ?? 0) });
  }
  const configPath = payload.config?.path ?? '';
  if (configPath) rows.push({ label: 'Config path', value: configPath });
  const commands = Array.isArray(payload.commands) ? payload.commands.length : 0;
  rows.push({ label: 'Commands', value: String(commands) });

  return (
    <section className="settings-section" aria-labelledby={headingId}>
      <h3 id={headingId}>About</h3>
      {rows.length === 0 ? (
        <div className="settings-row">
          <span className="field-label">No build information was reported.</span>
        </div>
      ) : (
        rows.map((row) => (
          <div className="settings-row" key={row.label}>
            <span className="field-label">{row.label}</span>
            <span title={row.value}>{row.value}</span>
          </div>
        ))
      )}
    </section>
  );
}

export function Settings({
  open,
  payload,
  loading,
  error,
  busy,
  probe,
  onClose,
  onReload,
  onUpdateAgent,
  onUpdateProvider,
  onTestProvider,
  onUpdatePermission,
  initialSection,
}: SettingsProps) {
  const [section, setSection] = useState<SectionId>(initialSection ?? 'provider');
  const [loadTimedOut, setLoadTimedOut] = useState(false);
  const [pendingForms, setPendingForms] = useState<string[]>([]);
  const [saveOutcome, setSaveOutcome] = useState<SaveOutcome | null>(null);
  const pendingSavesRef = useRef<PendingSave[]>([]);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    if (!open || !initialSection) return;
    setSection(initialSection);
  }, [open, initialSection]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [open, onClose]);

  useEffect(() => {
    if (open) return;
    pendingSavesRef.current = [];
    setPendingForms([]);
    setSaveOutcome(null);
    setLoadTimedOut(false);
  }, [open]);

  useEffect(() => {
    if (!open || !loading || payload) {
      setLoadTimedOut(false);
      return;
    }
    if (loadTimedOut) return;
    const timer = window.setTimeout(() => {
      setLoadTimedOut(true);
    }, LOAD_TIMEOUT_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [open, loading, payload, loadTimedOut]);

  useEffect(() => {
    if (pendingSavesRef.current.length === 0) return;
    let changed = false;
    let outcome: SaveOutcome | null = null;
    const remaining: PendingSave[] = [];
    for (const entry of pendingSavesRef.current) {
      if (payload !== entry.payload) {
        outcome = { form: entry.form, stage: 'saved' };
        changed = true;
      } else if (busy) {
        if (!entry.sawBusy) {
          entry.sawBusy = true;
          changed = true;
        }
        remaining.push(entry);
      } else if (entry.sawBusy) {
        outcome = {
          form: entry.form,
          stage: 'error',
          message: 'Could not save these changes. Check the notification for the server message, then try again.',
        };
        changed = true;
      } else {
        remaining.push(entry);
      }
    }
    pendingSavesRef.current = remaining;
    if (changed) {
      setPendingForms(remaining.map((entry) => entry.form));
      if (outcome) setSaveOutcome(outcome);
    }
  }, [payload, busy, pendingForms]);

  useEffect(() => {
    if (!saveOutcome || saveOutcome.stage !== 'saved') return;
    const timer = window.setTimeout(() => {
      setSaveOutcome(null);
    }, SAVED_NOTICE_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [saveOutcome]);

  if (!open) return null;

  const beginSave = (form: string) => {
    pendingSavesRef.current = [
      ...pendingSavesRef.current.filter((entry) => entry.form !== form),
      { form, payload, sawBusy: false },
    ];
    setPendingForms(pendingSavesRef.current.map((entry) => entry.form));
    setSaveOutcome(null);
  };

  const tracker: SaveTracker = {
    begin: beginSave,
    stageOf: (form) => {
      if (pendingForms.includes(form)) return 'saving';
      if (saveOutcome && saveOutcome.form === form) return saveOutcome.stage;
      return 'idle';
    },
    messageOf: (form) => (saveOutcome && saveOutcome.form === form ? (saveOutcome.message ?? '') : ''),
  };

  const reload = () => {
    setLoadTimedOut(false);
    setSaveOutcome(null);
    onReload();
  };

  const externalBusy = busy && pendingForms.length === 0;
  const providers: ProviderConfig[] =
    payload && Array.isArray(payload.provider?.providers) ? payload.provider.providers : [];
  const permissions: PermissionConfig[] =
    payload && Array.isArray(payload.permissions?.presets) ? payload.permissions.presets : [];
  const agent: AgentConfig | null = payload ? (payload.agent ?? null) : null;
  const commands = payload && Array.isArray(payload.commands) ? payload.commands : [];
  const plugins = payload ? (payload.plugins as SettingsPayload['plugins'] | undefined) : undefined;
  const sessions = payload ? (payload.sessions as SettingsPayload['sessions'] | undefined) : undefined;
  const web = payload ? (payload.web as SettingsPayload['web'] | undefined) : undefined;
  const routingProvider = payload?.provider?.current ?? agent?.provider ?? providers[0]?.name ?? '';
  const routingModel = payload?.provider?.model ?? agent?.model ?? '';
  const agentKey = agent
    ? agent.provider +
      '|' +
      agent.model +
      '|' +
      agent.temperature +
      '|' +
      agent.max_tokens +
      '|' +
      agent.max_steps
    : 'none';
  const activeSection = SECTIONS.find((item) => item.id === section) ?? SECTIONS[0];

  let liveMessage = '';
  if (loadTimedOut) {
    liveMessage = 'Settings are taking longer than expected to load.';
  } else if (saveOutcome) {
    liveMessage = saveOutcome.stage === 'saved' ? 'Settings saved.' : 'Settings could not be saved.';
  } else if (loading && !payload) {
    liveMessage = 'Loading settings.';
  }

  const moveTab = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
      nextIndex = (index + 1) % SECTIONS.length;
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
      nextIndex = (index - 1 + SECTIONS.length) % SECTIONS.length;
    } else if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = SECTIONS.length - 1;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    const targetIndex = nextIndex;
    setSection(SECTIONS[targetIndex].id);
    window.requestAnimationFrame(() => {
      tabRefs.current[targetIndex]?.focus();
    });
  };

  return (
    <div
      className="settings-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Settings"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="settings-panel" aria-busy={!payload && loading ? true : undefined}>
        <nav className="settings-nav" aria-label="Settings navigation">
          <div className="settings-nav-head">
            <span className="settings-brand">Settings</span>
          </div>
          <div
            role="tablist"
            aria-orientation="vertical"
            aria-label="Settings sections"
            className="settings-nav-list"
          >
            {SECTIONS.map((item, index) => {
              const startsGroup = index === 0 || SECTIONS[index - 1].group !== item.group;
              return (
                <Fragment key={item.id}>
                  {startsGroup ? <div className="settings-nav-group">{item.group}</div> : null}
                  <button
                    ref={(element) => {
                      tabRefs.current[index] = element;
                    }}
                    id={'settings-tab-' + item.id}
                    className={'settings-nav-item' + (section === item.id ? ' active' : '')}
                    type="button"
                    role="tab"
                    aria-selected={section === item.id}
                    aria-controls={'settings-panel-' + item.id}
                    tabIndex={section === item.id ? 0 : -1}
                    onClick={() => setSection(item.id)}
                    onKeyDown={(event) => moveTab(event, index)}
                  >
                    <Icon name={item.icon} size={16} />
                    <span>{item.label}</span>
                  </button>
                </Fragment>
              );
            })}
          </div>
        </nav>

        <div className="settings-main">
          <div className="settings-head">
            <div className="settings-title">
              <h2>{activeSection.label}</h2>
              <p className="settings-subtitle">{activeSection.description}</p>
            </div>
            <div className="settings-actions">
              <button className="button ghost small" type="button" onClick={reload} disabled={loading || busy}>
                <Icon name="refresh" size={14} />
                Reload
              </button>
              <button
                className="button ghost small"
                type="button"
                onClick={onClose}
                title="Close settings"
                aria-label="Close settings"
              >
                <Icon name="x" size={16} />
              </button>
            </div>
          </div>

          <div className="sr-only" role="status" aria-live="polite">
            {liveMessage}
          </div>

          <div
            className="settings-body"
            role="tabpanel"
            id={'settings-panel-' + section}
            aria-labelledby={'settings-tab-' + section}
          >
            {error ? (
              <div className="settings-error" role="alert">
                <Icon name="alert" size={15} />
                <span>{error}</span>
              </div>
            ) : null}
            {error && !payload ? (
              <div className="settings-actions">
                <button className="button primary small" type="button" onClick={reload}>
                  Retry
                </button>
              </div>
            ) : null}

            {!payload && !error ? (
              loadTimedOut ? (
                <>
                  <div className="settings-error" role="alert">
                    <Icon name="alert" size={15} />
                    <span>
                      Settings are taking longer than expected to load. The runtime may still be starting.
                    </span>
                  </div>
                  <div className="settings-actions">
                    <button className="button primary small" type="button" onClick={reload}>
                      Retry
                    </button>
                  </div>
                </>
              ) : loading ? (
                <div className="transcript-skeleton" aria-hidden="true">
                  {[0, 1, 2].map((row) => (
                    <div className="skeleton-row" key={row}>
                      <div className="skeleton-avatar" />
                      <div className="skeleton-lines">
                        <span style={{ width: '38%' }} />
                        <span style={{ width: '92%' }} />
                        <span style={{ width: '70%' }} />
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="settings-loading">
                  <span>Settings are unavailable.</span>
                  <button className="button ghost small" type="button" onClick={reload}>
                    Reload
                  </button>
                </div>
              )
            ) : null}

            {payload ? (
              <>
                {loading ? (
                  <div className="settings-loading" role="status">
                    Refreshing…
                  </div>
                ) : null}

                {section === 'provider' ? (
                  <ProviderSection
                    key={'routing|' + routingProvider + '|' + routingModel}
                    providers={providers}
                    currentProvider={routingProvider}
                    currentModel={routingModel}
                    probe={probe}
                    busy={busy}
                    externalBusy={externalBusy}
                    tracker={tracker}
                    onUpdateAgent={onUpdateAgent}
                    onUpdateProvider={onUpdateProvider}
                    onTestProvider={onTestProvider}
                  />
                ) : null}

                {section === 'agent' ? (
                  agent ? (
                    <AgentSection
                      key={agentKey}
                      agent={agent}
                      externalBusy={externalBusy}
                      tracker={tracker}
                      onUpdateAgent={onUpdateAgent}
                    />
                  ) : (
                    <div className="settings-loading">No agent settings were reported.</div>
                  )
                ) : null}

                {section === 'permissions' ? (
                  <PermissionsSection
                    key={(payload.permissions?.current ?? '') + '|' + permissions.length}
                    presets={permissions}
                    current={payload.permissions?.current ?? ''}
                    fallback={payload.permissions?.default ?? ''}
                    custom={payload.permissions?.custom === true}
                    externalBusy={externalBusy}
                    tracker={tracker}
                    onUpdatePermission={onUpdatePermission}
                  />
                ) : null}

                {section === 'commands' ? <CommandsSection commands={commands} /> : null}
                {section === 'plugins' ? <PluginsSection plugins={plugins} /> : null}
                {section === 'sessions' ? <SessionsSection sessions={sessions} /> : null}
                {section === 'web' ? <WebSection web={web} /> : null}
                {section === 'about' ? <AboutSection payload={payload} /> : null}
              </>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
