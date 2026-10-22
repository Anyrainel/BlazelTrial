import { useState, type SyntheticEvent } from "react";
import { AGENT_NAME, Brand } from "./Brand";

const KEY = "hazel.slack_handle";

export const normalizeHandle = (raw: string) => raw.trim().replace(/^@+/, "");

export function loadHandle(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function saveHandle(handle: string | null) {
  try {
    if (handle) localStorage.setItem(KEY, handle);
    else localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: the session just lasts until reload */
  }
}

/** Demo-only sign-in: no password, the Slack handle is just a display identity. */
export function Login({ onLogin }: { onLogin: (handle: string) => void }) {
  const [handle, setHandle] = useState("");
  const clean = normalizeHandle(handle);

  const submit = (e: SyntheticEvent) => {
    e.preventDefault();
    if (clean) onLogin(clean);
  };

  return (
    <div className="login-screen">
      <header className="login-header">
        <Brand />
      </header>
      <form className="login-card" onSubmit={submit}>
        <h1>Meet {AGENT_NAME}</h1>
        <p className="login-sub">
          Your personal assistant, in the web app and in Slack. This is a demo, so there is no
          password.
        </p>
        <label className="login-label" htmlFor="slack-handle">
          Slack handle
        </label>
        <div className="login-field">
          <span className="login-at">@</span>
          <input
            id="slack-handle"
            value={handle}
            onChange={(e) => setHandle(e.target.value)}
            placeholder="your.handle"
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            autoFocus
          />
        </div>
        <button type="submit" className="login-submit" disabled={!clean}>
          Log in as test user
        </button>
      </form>
    </div>
  );
}
