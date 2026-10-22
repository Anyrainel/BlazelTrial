import { AGENT_NAME, Brand } from "./Brand";

const KEY = "hazel.test_user_session";

export function loadSession(): boolean {
  try {
    return localStorage.getItem(KEY) === "true";
  } catch {
    return false;
  }
}

export function saveSession(loggedIn: boolean) {
  try {
    if (loggedIn) localStorage.setItem(KEY, "true");
    else localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: the session just lasts until reload */
  }
}

/** Demo-only entry to the configured default user's workspace; no authentication. */
export function Login({ onLogin }: { onLogin: () => void }) {
  return (
    <div className="login-screen">
      <header className="login-header">
        <Brand />
      </header>
      <div className="login-card">
        <h1>Meet {AGENT_NAME}</h1>
        <p className="login-sub">
          Your personal assistant, in the web app and in Slack. This demo uses one shared
          test user, with no password.
        </p>
        <button type="button" className="login-submit" onClick={onLogin}>
          Log in as test user
        </button>
      </div>
    </div>
  );
}
