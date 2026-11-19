import { useEffect, useState } from 'react';
import type { ProviderProbe, SettingsPayload } from '../types';
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
}

type SectionId = 'provider' | 'agent' | 'permissions' | 'about';
type ProviderConfig = SettingsPayload['provider']['providers'][number];
type AgentConfig = SettingsPayload['agent'];
type PermissionConfig = SettingsPayload['permissions']['presets'][number];

const SECTIONS: Array<{ id: SectionId; label: string; icon: IconName }> = [
  { id: 'provider', label: 'Provider', icon: 'plug' },
  { id: 'agent', label: 'Agent', icon: 'sparkles' },
  { id: 'permissions', label: 'Permissions', icon: 'key' },
  { id: 'about', label: 'About', icon: 'info' },
];

function probeLabel(probe: ProviderProbe): string {
  const status = probe.ok === true ? 'Connected' : probe.ok === false ? 'Failed' : (probe.status ?? 'Done');
  const latency = typeof probe.latency_ms === 'number' ? ` · ${Math.round(probe.latency_ms)} ms` : '';
  const detail = probe.message ?? probe.detail ?? '';
  return `${status}${latency}${detail ? ` · ${detail}` : ''}`;
}

function modelsOf(provider: ProviderConfig | null): string[] {
  if (!provider || !Array.isArray(provider.models)) return [];
  return provider.models.map((model) => model.id);
}

function ProviderCard({
  provider,
  busy,
  onSave,
  onTest,
}: {
  provider: ProviderConfig;
  busy: boolean;
  onSave: SettingsProps['onUpdateProvider'];
  onTest: SettingsProps['onTestProvider'];
}) {
  const [baseUrl, setBaseUrl] = useState(provider.base_url ?? '');
  const [apiKeyEnv, setApiKeyEnv] = useState(provider.api_key_env ?? '');
  const [apiKey, setApiKey] = useState('');

  const save = () => {
    const patch: { base_url?: string; api_key?: string; api_key_env?: string } = {
      base_url: baseUrl.trim(),
      api_key_env: apiKeyEnv.trim(),
    };
    const nextKey = apiKey.trim();
    if (nextKey) patch.api_key = nextKey;
    onSave(provider.name, patch);
    setApiKey('');
  };

  return (
    <section className="settings-section">
      <div className="settings-row">
        <span className="field-label">{provider.name}</span>
        <span className={`badge ${provider.api_key_set ? 'live' : 'idle'}`}>
          {provider.api_key_set ? 'KEY SET' : 'NO KEY'}
        </span>
      </div>
      <div className="settings-row">
        <span className="field-label">API</span>
        <span>{provider.api || '—'}</span>
      </div>
      <div className="settings-row">
        <span className="field-label">Models</span>
        <span>{Array.isArray(provider.models) ? provider.models.length : 0}</span>
      </div>
      <div className="field">
        <span className="field-label">Base URL</span>
        <input
          className="field-input"
          value={baseUrl}
          onChange={(event) => setBaseUrl(event.target.value)}
          placeholder="https://api.example.com/v1"
          spellCheck={false}
        />
      </div>
      <div className="field">
        <span className="field-label">API key env</span>
        <input
          className="field-input"
          value={apiKeyEnv}
          onChange={(event) => setApiKeyEnv(event.target.value)}
          placeholder="PROVIDER_API_KEY"
          spellCheck={false}
        />
      </div>
      <div className="field">
        <span className="field-label">API key {provider.api_key_set ? '(saved)' : '(not set)'}</span>
        <input
          className="field-input"
          type="password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          placeholder={provider.api_key_set ? 'Leave blank to keep the current key' : 'Paste a key'}
          autoComplete="off"
          spellCheck={false}
        />
      </div>
      <div className="settings-actions">
        <button className="button primary small" type="button" disabled={busy} onClick={save}>
          Save
        </button>
        <button
          className="button ghost small"
          type="button"
          disabled={busy}
          onClick={() => onTest(provider.name)}
        >
          Test
        </button>
      </div>
    </section>
  );
}

function ProviderSection({
  providers,
  currentProvider,
  currentModel,
  probe,
  busy,
  onUpdateAgent,
  onUpdateProvider,
  onTestProvider,
}: {
  providers: ProviderConfig[];
  currentProvider: string;
  currentModel: string;
  probe: ProviderProbe | null;
  busy: boolean;
  onUpdateAgent: SettingsProps['onUpdateAgent'];
  onUpdateProvider: SettingsProps['onUpdateProvider'];
  onTestProvider: SettingsProps['onTestProvider'];
}) {
  const [providerName, setProviderName] = useState(currentProvider);
  const [modelName, setModelName] = useState(currentModel);

  const selected = providers.find((provider) => provider.name === providerName) ?? null;
  const models = modelsOf(selected);
  const hasModel = models.includes(modelName);

  const changeProvider = (name: string) => {
    setProviderName(name);
    const next = providers.find((provider) => provider.name === name) ?? null;
    const nextModels = modelsOf(next);
    setModelName((previous) =>
      nextModels.includes(previous) ? previous : (nextModels[0] ?? ''),
    );
  };

  const apply = () => {
    const patch: { provider?: string; model?: string } = {};
    if (providerName) patch.provider = providerName;
    if (modelName) patch.model = modelName;
    if (patch.provider || patch.model) onUpdateAgent(patch);
  };

  return (
    <>
      <section className="settings-section">
        <h3>Routing</h3>
        <div className="settings-row">
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
              onChange={(event) => setModelName(event.target.value)}
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
        <div className="settings-actions">
          <button
            className="button primary small"
            type="button"
            disabled={busy || !providerName}
            onClick={apply}
          >
            Apply routing
          </button>
        </div>
      </section>

      {probe ? (
        <section className="settings-section">
          <div className="settings-row">
            <span className="field-label">Last test</span>
            <span>{probeLabel(probe)}</span>
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

      {providers.map((provider) => (
        <ProviderCard
          key={`${provider.name}|${provider.base_url}|${provider.api_key_env}|${provider.api_key_set ? 'set' : 'unset'}`}
          provider={provider}
          busy={busy}
          onSave={onUpdateProvider}
          onTest={onTestProvider}
        />
      ))}
    </>
  );
}

function AgentSection({
  agent,
  providers,
  busy,
  onUpdateAgent,
}: {
  agent: AgentConfig;
  providers: ProviderConfig[];
  busy: boolean;
  onUpdateAgent: SettingsProps['onUpdateAgent'];
}) {
  const [providerName, setProviderName] = useState(agent.provider);
  const [modelName, setModelName] = useState(agent.model);
  const [temperature, setTemperature] = useState(String(agent.temperature));
  const [maxTokens, setMaxTokens] = useState(String(agent.max_tokens));
  const [maxSteps, setMaxSteps] = useState(String(agent.max_steps));

  const selected = providers.find((provider) => provider.name === providerName) ?? null;
  const models = modelsOf(selected);
  const hasModel = models.includes(modelName);

  const changeProvider = (name: string) => {
    setProviderName(name);
    const next = providers.find((provider) => provider.name === name) ?? null;
    const nextModels = modelsOf(next);
    setModelName((previous) =>
      nextModels.includes(previous) ? previous : (nextModels[0] ?? ''),
    );
  };

  const toNumber = (raw: string, fallback: number): number => {
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : fallback;
  };

  const apply = () => {
    const patch: {
      provider?: string;
      model?: string;
      temperature?: number;
      max_tokens?: number;
      max_steps?: number;
    } = {
      temperature: Math.min(2, Math.max(0, toNumber(temperature, agent.temperature))),
      max_tokens: Math.max(1, Math.floor(toNumber(maxTokens, agent.max_tokens))),
      max_steps: Math.max(1, Math.floor(toNumber(maxSteps, agent.max_steps))),
    };
    if (providerName) patch.provider = providerName;
    if (modelName) patch.model = modelName;
    onUpdateAgent(patch);
  };

  return (
    <section className="settings-section">
      <h3>Agent defaults</h3>
      <div className="settings-row">
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
            onChange={(event) => setModelName(event.target.value)}
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
      <div className="settings-row">
        <label className="field">
          <span className="field-label">Temperature</span>
          <input
            className="field-input"
            type="number"
            min={0}
            max={2}
            step={0.05}
            value={temperature}
            onChange={(event) => setTemperature(event.target.value)}
          />
        </label>
        <label className="field">
          <span className="field-label">Max tokens</span>
          <input
            className="field-input"
            type="number"
            min={1}
            step={1}
            value={maxTokens}
            onChange={(event) => setMaxTokens(event.target.value)}
          />
        </label>
        <label className="field">
          <span className="field-label">Max steps</span>
          <input
            className="field-input"
            type="number"
            min={1}
            step={1}
            value={maxSteps}
            onChange={(event) => setMaxSteps(event.target.value)}
          />
        </label>
      </div>
      <div className="settings-actions">
        <button className="button primary small" type="button" disabled={busy} onClick={apply}>
          Apply
        </button>
      </div>
    </section>
  );
}

function PermissionsSection({
  presets,
  current,
  fallback,
  custom,
  busy,
  onUpdatePermission,
}: {
  presets: PermissionConfig[];
  current: string;
  fallback: string;
  custom: boolean;
  busy: boolean;
  onUpdatePermission: SettingsProps['onUpdatePermission'];
}) {
  const [choice, setChoice] = useState(current);

  return (
    <section className="settings-section">
      <h3>Permission presets</h3>
      <div className="settings-row">
        <span className="field-label">Current</span>
        <span className={`badge ${current ? 'live' : 'idle'}`}>{current || 'default'}</span>
        {custom ? <span className="badge idle">CUSTOM</span> : null}
      </div>
      {presets.length === 0 ? (
        <div className="settings-row">
          <span className="field-label">No permission presets were reported by the runtime.</span>
        </div>
      ) : (
        presets.map((preset) => (
          <label className="settings-row" key={preset.name}>
            <input
              type="radio"
              name="permission-preset"
              value={preset.name}
              checked={choice === preset.name}
              disabled={busy}
              onChange={() => setChoice(preset.name)}
            />
            <span className="field-label">{preset.label ?? preset.name}</span>
            <span>{preset.description || '—'}</span>
            <span className="badge">
              {preset.sandbox}/{preset.approval}
            </span>
          </label>
        ))
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
          type="button"
          disabled={busy || !choice || choice === current}
          onClick={() => onUpdatePermission(choice)}
        >
          Apply
        </button>
      </div>
    </section>
  );
}

function AboutSection({ payload }: { payload: SettingsPayload }) {
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
  const sessionsDir = payload.sessions?.dir ?? '';
  if (sessionsDir) rows.push({ label: 'Sessions directory', value: sessionsDir });
  if (Array.isArray(payload.commands)) {
    rows.push({ label: 'Commands', value: String(payload.commands.length) });
  }
  const web = payload.web as SettingsPayload['web'] | undefined;
  if (web) rows.push({ label: 'Web server', value: `${web.host}:${web.port}` });

  return (
    <section className="settings-section">
      <h3>About</h3>
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
}: SettingsProps) {
  const [section, setSection] = useState<SectionId>('provider');

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const providers: ProviderConfig[] =
    payload && Array.isArray(payload.provider?.providers) ? payload.provider.providers : [];
  const permissions: PermissionConfig[] =
    payload && Array.isArray(payload.permissions?.presets) ? payload.permissions.presets : [];
  const agent: AgentConfig | null = payload ? (payload.agent ?? null) : null;
  const routingProvider = payload?.provider?.current ?? agent?.provider ?? providers[0]?.name ?? '';
  const routingModel = payload?.provider?.model ?? agent?.model ?? '';
  const agentKey = agent
    ? `${agent.provider}|${agent.model}|${agent.temperature}|${agent.max_tokens}|${agent.max_steps}`
    : 'none';

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
      <div className="settings-panel">
        <nav className="settings-nav" aria-label="Settings sections">
          <div className="settings-nav-head">
            <span className="field-label">SETTINGS</span>
          </div>
          {SECTIONS.map((item) => (
            <button
              key={item.id}
              className={`settings-nav-item ${section === item.id ? 'active' : ''}`}
              type="button"
              onClick={() => setSection(item.id)}
            >
              <Icon name={item.icon} size={16} />
              <span>{item.label}</span>
            </button>
          ))}
        </nav>

        <div className="settings-main">
          <div className="settings-head">
            <h2>{SECTIONS.find((item) => item.id === section)?.label ?? 'Settings'}</h2>
            <div className="settings-actions">
              <button
                className="button ghost small"
                type="button"
                onClick={onReload}
                disabled={loading}
              >
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

          <div className="settings-body">
            {loading && payload ? <div className="settings-loading">Refreshing…</div> : null}
            {error ? (
              <div className="settings-error">
                <Icon name="alert" size={15} />
                <span>{error}</span>
              </div>
            ) : null}
            {!payload && loading ? <div className="settings-loading">Loading settings…</div> : null}
            {!payload && !loading ? (
              <div className="settings-loading">
                <span>Settings are unavailable.</span>
                <button className="button ghost small" type="button" onClick={onReload}>
                  Reload
                </button>
              </div>
            ) : null}

            {payload && section === 'provider' ? (
              <ProviderSection
                key={`${routingProvider}|${routingModel}`}
                providers={providers}
                currentProvider={routingProvider}
                currentModel={routingModel}
                probe={probe}
                busy={busy}
                onUpdateAgent={onUpdateAgent}
                onUpdateProvider={onUpdateProvider}
                onTestProvider={onTestProvider}
              />
            ) : null}

            {payload && section === 'agent' ? (
              agent ? (
                <AgentSection
                  key={agentKey}
                  agent={agent}
                  providers={providers}
                  busy={busy}
                  onUpdateAgent={onUpdateAgent}
                />
              ) : (
                <div className="settings-loading">No agent settings were reported.</div>
              )
            ) : null}

            {payload && section === 'permissions' ? (
              <PermissionsSection
                key={`${payload.permissions?.current ?? ''}|${permissions.length}`}
                presets={permissions}
                current={payload.permissions?.current ?? ''}
                fallback={payload.permissions?.default ?? ''}
                custom={payload.permissions?.custom === true}
                busy={busy}
                onUpdatePermission={onUpdatePermission}
              />
            ) : null}

            {payload && section === 'about' ? <AboutSection payload={payload} /> : null}
          </div>
        </div>
      </div>
    </div>
  );
}
