import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity, AlertCircle, Bot, Check, ChevronDown, CircleHelp,
  Clock3, ExternalLink, Github, LayoutDashboard, LoaderCircle,
  MessageSquare, Radio, RefreshCw, Settings2, ShieldCheck,
  Sparkles, Square, Youtube, Zap
} from "lucide-react";
import { api } from "./api";

const DEFAULT_FALLBACKS = "Great stream today!\nLoving the content so far!\nKeep it up!";
const STORAGE_KEY = "stream-pilot-forms-v1";
const PLATFORM_KEY = "stream-pilot-active-platform-v1";

function defaults() {
  return {
    youtube: { target: "", interval: 300, mode: "gemini", manual_message: "", fallbacksText: DEFAULT_FALLBACKS },
    kick: { target: "", interval: 300, mode: "gemini", manual_message: "", fallbacksText: DEFAULT_FALLBACKS },
  };
}
function loadForms() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "null");
    return { ...defaults(), ...(saved || {}) };
  } catch { return defaults(); }
}
const platformMeta = {
  youtube: { name: "YouTube", accent: "red", auth: "Google", placeholder: "Paste a YouTube live stream URL or video ID", targetLabel: "YouTube stream URL or video ID", hint: "Supports youtu.be links, /live/, /shorts/, /embed/ and youtube.com/watch?v= links." },
  kick: { name: "Kick", accent: "green", auth: "Kick", placeholder: "Paste a Kick channel URL or channel slug", targetLabel: "Kick channel URL or channel slug", hint: "Paste a kick.com channel link or enter the channel slug directly." },
};

function App() {
  const [platform, setPlatform] = useState(() => sessionStorage.getItem(PLATFORM_KEY) === "kick" ? "kick" : "youtube");
  const [forms, setForms] = useState(loadForms);
  const [status, setStatus] = useState({ running: false, authenticated: false, logs: [] });
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [toast, setToast] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);
  const form = forms[platform];
  const meta = platformMeta[platform];

  const notify = useCallback((message, type = "success") => {
    setToast({ message, type });
    window.setTimeout(() => setToast(null), 4200);
  }, []);

  const updateForm = (patch) => {
    setForms((current) => {
      const next = { ...current, [platform]: { ...current[platform], ...patch } };
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      return next;
    });
  };

  const refreshStatus = useCallback(async (quiet = true) => {
    if (!quiet) setLoading(true);
    try {
      const data = await api.status(platform);
      setStatus(data);
      setLastUpdated(new Date());
    } catch (error) {
      if (!quiet) notify(error.message, "error");
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [platform, notify]);

  useEffect(() => {
    sessionStorage.setItem(PLATFORM_KEY, platform);
    refreshStatus(false);
    const timer = window.setInterval(() => refreshStatus(true), 2000);
    return () => window.clearInterval(timer);
  }, [platform, refreshStatus]);

  const saveConfig = async (showMessage = false) => {
    try {
      await api.config({
        platform,
        mode: form.mode,
        manual_message: form.manual_message,
        fallback_messages: form.fallbacksText.split("\n").map((line) => line.trim()).filter(Boolean),
      });
      if (showMessage) notify("Configuration applied.");
    } catch (error) { notify(error.message, "error"); }
  };

  const startAgent = async () => {
    setBusy("start");
    try {
      const payload = {
        platform,
        target: form.target.trim(),
        interval: Number(form.interval) || 300,
        mode: form.mode,
        manual_message: form.manual_message,
        fallback_messages: form.fallbacksText.split("\n").map((line) => line.trim()).filter(Boolean),
      };
      await api.start(payload);
      notify(`${meta.name} agent start requested.`);
      await refreshStatus(true);
    } catch (error) { notify(error.message, "error"); }
    finally { setBusy(""); }
  };

  const stopAgent = async () => {
    setBusy("stop");
    try {
      await api.stop(platform);
      notify(`${meta.name} agent stop requested.`);
      await refreshStatus(true);
    } catch (error) { notify(error.message, "error"); }
    finally { setBusy(""); }
  };

  const sentCount = useMemo(
    () => (status.logs || []).filter((line) => line.includes("Sent:")).length,
    [status.logs]
  );
  const logLines = status.logs || [];

  return (
    <div className={`app-shell ${platform === "kick" ? "kick-theme" : ""}`}>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Radio size={20} /></div>
          <div><strong>StreamHelm</strong><span>CHAT AUTOMATION</span></div>
        </div>
        <div className="workspace-label">WORKSPACE</div>
        <button className="nav-item active"><LayoutDashboard size={17} /> Dashboard</button>
        <div className="sidebar-divider" />
        <div className="workspace-label">PLATFORMS</div>
        <button className={`platform-nav ${platform === "youtube" ? "selected" : ""}`} onClick={() => setPlatform("youtube")}>
          <span className="platform-icon youtube"><Youtube size={17} /></span> YouTube
          {platform === "youtube" && <span className="nav-dot" />}
        </button>
        <button className={`platform-nav ${platform === "kick" ? "selected" : ""}`} onClick={() => setPlatform("kick")}>
          <span className="platform-icon kick"><Zap size={16} /></span> Kick
          {platform === "kick" && <span className="nav-dot" />}
        </button>
        <div className="sidebar-bottom">
          <div className="help-card">
            <div className="help-icon"><CircleHelp size={17} /></div>
            <strong>Need a hand?</strong>
            <p>Check the setup and troubleshooting guide.</p>
            <a href="https://github.com/Sankalp-03/yt-chat-render" target="_blank" rel="noreferrer">View repository <ExternalLink size={12} /></a>
          </div>
          <a className="github-link" href="https://github.com/Sankalp-03/yt-chat-render" target="_blank" rel="noreferrer"><Github size={16} /> GitHub repository <ExternalLink size={12} /></a>
          <div className="version">STREAMHELM <span>·</span> DASHBOARD</div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div className="breadcrumbs"><span>Workspace</span><span className="crumb-slash">/</span><strong>Dashboard</strong></div>
          <div className="topbar-right">
            <span className="secure-label"><ShieldCheck size={14} /> Session protected</span>
          </div>
        </header>

        <div className="page-wrap">
          <section className="page-heading">
            <div>
              <div className="eyebrow"><span className="eyebrow-line" /> STREAM MANAGEMENT</div>
              <h1>Live chat dashboard</h1>
              <p>Manage your chat agent, platform connection and message behavior.</p>
            </div>
            <button className="button button-quiet refresh-button" onClick={() => refreshStatus(false)} disabled={loading}>
              <RefreshCw size={15} className={loading ? "spin" : ""} /> Refresh
            </button>
          </section>

          <section className="status-grid">
            <div className="metric-card status-metric">
              <div className="metric-top"><span>AGENT STATUS</span><span className={`metric-icon ${status.running ? "green" : "neutral"}`}><Activity size={17} /></span></div>
              <div className="metric-main"><span className={`status-indicator ${status.running ? "online" : ""}`} />{status.running ? "Running" : "Stopped"}</div>
              <div className="metric-foot">{status.running ? "Your agent is active" : "Agent is currently idle"}</div>
            </div>
            <div className="metric-card">
              <div className="metric-top"><span>PLATFORM</span><span className="metric-icon purple"><Radio size={17} /></span></div>
              <div className="metric-main">{meta.name}</div>
              <div className="metric-foot">Selected workspace</div>
            </div>
            <div className="metric-card">
              <div className="metric-top"><span>AUTHENTICATION</span><span className={`metric-icon ${status.authenticated ? "green" : "amber"}`}><ShieldCheck size={17} /></span></div>
              <div className="metric-main">{status.authenticated ? "Connected" : "Not connected"}</div>
              <div className="metric-foot">{status.authenticated ? `${meta.auth} account authorized` : "Authentication required"}</div>
            </div>
            <div className="metric-card">
              <div className="metric-top"><span>MESSAGES SENT</span><span className="metric-icon blue"><MessageSquare size={17} /></span></div>
              <div className="metric-main">{sentCount}</div>
              <div className="metric-foot">Messages in current log</div>
            </div>
          </section>

          <section className="platform-banner">
            <div className={`platform-logo ${platform}`}>
              {platform === "youtube" ? <Youtube size={23} /> : <Zap size={22} />}
            </div>
            <div className="platform-copy"><strong>{meta.name} integration</strong><span>Configure and control your {meta.name} live chat agent.</span></div>
            <div className="platform-switch" role="group" aria-label="Choose platform">
              <button className={platform === "youtube" ? "chosen" : ""} onClick={() => setPlatform("youtube")}><Youtube size={15} /> YouTube</button>
              <button className={platform === "kick" ? "chosen" : ""} onClick={() => setPlatform("kick")}><Zap size={15} /> Kick</button>
            </div>
          </section>

          <div className="content-grid">
            <section className="panel config-panel">
              <div className="panel-heading">
                <div><div className="panel-title"><Settings2 size={17} /> Agent configuration</div><p>Set up the stream and customize how messages are sent.</p></div>
                <span className="step-tag">SETUP</span>
              </div>

              <div className="field-block">
                <div className="field-heading"><label htmlFor="target">{meta.targetLabel}</label><span className="required">REQUIRED</span></div>
                <input id="target" className="text-input" value={form.target} onChange={(e) => updateForm({ target: e.target.value })} placeholder={meta.placeholder} />
                <div className="field-hint">{meta.hint}</div>
              </div>

              <div className="field-block">
                <div className="field-heading"><label>Message generation mode</label><span className="optional">CHOOSE ONE</span></div>
                <div className="mode-options">
                  <button className={`mode-option ${form.mode === "gemini" ? "active" : ""}`} onClick={() => { updateForm({ mode: "gemini" }); api.config({ platform, mode: "gemini", manual_message: form.manual_message, fallback_messages: form.fallbacksText.split("\n").filter(Boolean) }).catch(e => notify(e.message, "error")); }}>
                    <span className="mode-icon gemini"><Sparkles size={17} /></span><span><strong>Gemini Chat</strong><small>Generate contextual messages with AI</small></span><span className="radio-mark">{form.mode === "gemini" && <Check size={11} />}</span>
                  </button>
                  <button className={`mode-option ${form.mode === "manual" ? "active" : ""}`} onClick={() => { updateForm({ mode: "manual" }); api.config({ platform, mode: "manual", manual_message: form.manual_message, fallback_messages: form.fallbacksText.split("\n").filter(Boolean) }).catch(e => notify(e.message, "error")); }}>
                    <span className="mode-icon manual"><MessageSquare size={17} /></span><span><strong>Manual Chat</strong><small>Send your own custom message</small></span><span className="radio-mark">{form.mode === "manual" && <Check size={11} />}</span>
                  </button>
                </div>
              </div>

              <div className={`field-block ${form.mode !== "manual" ? "field-disabled" : ""}`}>
                <div className="field-heading"><label htmlFor="manual-message">Manual chat message</label><span className="optional">OPTIONAL</span></div>
                <input id="manual-message" className="text-input" value={form.manual_message} disabled={form.mode !== "manual"} onChange={(e) => updateForm({ manual_message: e.target.value })} onBlur={() => saveConfig()} placeholder="Write the message you want the agent to send..." />
                <div className="field-hint">Your message is used on the next scheduled cycle. You can update it while the agent is running.</div>
              </div>

              <div className="two-column-fields">
                <div className="field-block">
                  <div className="field-heading"><label htmlFor="interval">Message interval</label></div>
                  <div className="input-with-suffix"><Clock3 size={16} /><input id="interval" type="number" min="15" value={form.interval} onChange={(e) => updateForm({ interval: e.target.value })} /><span>seconds</span></div>
                  <div className="field-hint">Minimum interval: 15 seconds.</div>
                </div>
                <div className="field-block">
                  <div className="field-heading"><label>Current mode</label></div>
                  <div className="readout-box"><span className={`mode-dot ${form.mode}`} />{form.mode === "gemini" ? "Gemini AI generation" : "Manual message"}</div>
                  <div className="field-hint">Checked before every message.</div>
                </div>
              </div>

              <div className={`field-block ${form.mode !== "gemini" ? "field-muted" : ""}`}>
                <div className="field-heading"><label htmlFor="fallbacks">Fallback messages</label><span className="optional">ONE PER LINE</span></div>
                <textarea id="fallbacks" className="text-input textarea" value={form.fallbacksText} onChange={(e) => updateForm({ fallbacksText: e.target.value })} onBlur={() => saveConfig()} />
                <div className="field-hint">Used by Gemini Chat when the chat is quiet or Gemini is unavailable.</div>
              </div>

              <div className="config-footer">
                <span><span className="save-dot" /> Changes are saved in this browser session</span>
                <button className="button button-secondary" onClick={() => saveConfig(true)}><Check size={15} /> Apply configuration</button>
              </div>
            </section>

            <div className="right-column">
              <section className="panel connection-panel">
                <div className="panel-heading compact"><div><div className="panel-title"><ShieldCheck size={17} /> Account connection</div><p>Authorize your platform account.</p></div></div>
                <div className="connection-row">
                  <div className={`connection-platform-icon ${platform}`}>{platform === "youtube" ? <Youtube size={19} /> : <Zap size={18} />}</div>
                  <div className="connection-info"><strong>{meta.auth} account</strong><span>{status.authenticated ? "Authentication active" : "Authorization needed"}</span></div>
                  <span className={`connection-state ${status.authenticated ? "connected" : "disconnected"}`}><span />{status.authenticated ? "Ready" : "Required"}</span>
                </div>
                <a className="button button-auth" href={platform === "youtube" ? "/auth" : "/kick/auth"}>
                  <ShieldCheck size={15} /> {status.authenticated ? `Re-authenticate ${meta.auth}` : `Connect ${meta.auth}`} <ExternalLink size={13} />
                </a>
                <div className="auth-note"><AlertCircle size={14} /> You'll be redirected to {platform === "youtube" ? "Google" : "Kick"} to authorize access.</div>
              </section>

              <section className="panel controls-panel">
                <div className="panel-heading compact"><div><div className="panel-title"><Bot size={17} /> Agent controls</div><p>Start or stop automated chat activity.</p></div><span className={`running-pill ${status.running ? "is-running" : ""}`}><span />{status.running ? "ACTIVE" : "IDLE"}</span></div>
                <div className="control-state"><div className={`control-state-icon ${status.running ? "active" : ""}`}>{status.running ? <Activity size={21} /> : <Square size={18} />}</div><div><strong>{status.running ? "Agent is running" : "Agent is stopped"}</strong><span>{status.running ? "Messages are being sent on schedule." : "Start the agent when you're ready."}</span></div></div>
                <div className="control-buttons">
                  <button className="button button-start" onClick={startAgent} disabled={!!busy || status.running || !status.authenticated}>
                    {busy === "start" ? <LoaderCircle className="spin" size={16} /> : <Zap size={16} />} Start agent
                  </button>
                  <button className="button button-stop" onClick={stopAgent} disabled={!!busy || !status.running}>
                    {busy === "stop" ? <LoaderCircle className="spin" size={16} /> : <Square size={14} />} Stop
                  </button>
                </div>
                {!status.authenticated && <div className="control-warning"><AlertCircle size={14} /> Connect your {meta.auth} account before starting.</div>}
              </section>

              <section className="panel logs-panel">
                <div className="panel-heading compact logs-heading"><div><div className="panel-title"><Activity size={17} /> Activity log</div><p>Recent events from your agent.</p></div><span className="live-label"><span /> LIVE</span></div>
                <div className="log-window">
                  {logLines.length ? logLines.slice(-60).map((line, index) => {
                    const isError = /error|failed/i.test(line);
                    const isSent = /Sent:/i.test(line);
                    return <div className="log-line" key={`${index}-${line}`}><span className="log-bullet">{isError ? "!" : isSent ? "↗" : "·"}</span><span className={isError ? "log-error" : isSent ? "log-sent" : ""}>{line}</span></div>;
                  }) : <div className="empty-logs"><Activity size={22} /><strong>No activity yet</strong><span>Agent events will appear here.</span></div>}
                </div>
                <div className="logs-footer"><span>{logLines.length} events shown</span><span>{lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : "Waiting for status..."}</span></div>
              </section>
            </div>
          </div>
          <footer className="page-footer"><span>StreamHelm <span className="footer-separator">/</span> YouTube & Kick live chat automation</span><span>Built for your stream workflow</span></footer>
        </div>
      </main>
      {toast && <div className={`toast ${toast.type}`}><span>{toast.type === "error" ? <AlertCircle size={17} /> : <Check size={17} />}</span>{toast.message}<button onClick={() => setToast(null)}>×</button></div>}
    </div>
  );
}

export default App;