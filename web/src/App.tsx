import { useCallback, useEffect, useState } from "react";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import * as api from "./api";
import { AGENT_NAME, Brand } from "./Brand";
import { Login, loadSession, saveSession } from "./Login";
import { Browser, TABS, type TabId } from "./Browser";

const toThreadMessage = (m: api.Msg, i: number): ThreadMessageLike => ({
  id: `${i}`,
  role: m.role,
  content: [{ type: "text", text: m.text }],
  createdAt: m.created_at ? new Date(m.created_at) : undefined,
});

function UserMessage() {
  return (
    <MessagePrimitive.Root className="msg msg-user">
      <MessagePrimitive.Parts />
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="msg msg-assistant">
      <MessagePrimitive.Parts />
    </MessagePrimitive.Root>
  );
}

function Chat({ threadId, onSent }: { threadId: string; onSent: () => void }) {
  const [messages, setMessages] = useState<api.Msg[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.history(threadId).then(setMessages).catch(() => setMessages([]));
  }, [threadId]);

  const onNew = useCallback(
    async (message: AppendMessage) => {
      const text = message.content
        .map((p) => (p.type === "text" ? p.text : ""))
        .join("")
        .trim();
      if (!text) return;
      const now = new Date().toISOString();
      setMessages((prev) => [...prev, { role: "user", text, created_at: now }]);
      setIsRunning(true);
      setError(null);
      try {
        const { reply } = await api.send(threadId, text);
        setMessages((prev) => [...prev, { role: "assistant", text: reply, created_at: now }]);
        onSent();
      } catch (e) {
        setError(String(e));
      } finally {
        setIsRunning(false);
      }
    },
    [threadId, onSent],
  );

  const runtime = useExternalStoreRuntime({
    messages,
    isRunning,
    convertMessage: toThreadMessage,
    onNew,
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="thread">
        <ThreadPrimitive.Viewport className="viewport">
          <ThreadPrimitive.Empty>
            <div className="empty">
              <h2>Hi, I&apos;m {AGENT_NAME}. How can I help?</h2>
              <p>I remember what you tell me, across chats and in Slack.</p>
            </div>
          </ThreadPrimitive.Empty>
          <ThreadPrimitive.Messages components={{ UserMessage, AssistantMessage }} />
          {error && (
            <div className="error">Couldn&apos;t reach {AGENT_NAME}: {error}</div>
          )}
        </ThreadPrimitive.Viewport>
        <ComposerPrimitive.Root className="composer">
          <ComposerPrimitive.Input
            className="composer-input"
            placeholder={`Message ${AGENT_NAME}…`}
            autoFocus
          />
          <ComposerPrimitive.Send className="composer-send">Send</ComposerPrimitive.Send>
        </ComposerPrimitive.Root>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}

function Workspace({ onLogout }: { onLogout: () => void }) {
  const [view, setView] = useState<"chat" | TabId>("chat");
  const [threads, setThreads] = useState<api.Thread[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [mode, setMode] = useState<"chat" | "slack">("chat");

  const refresh = useCallback(() => {
    api.listThreads().then(setThreads).catch(() => setThreads([]));
  }, []);

  useEffect(refresh, [refresh]);

  const startNew = useCallback(async () => {
    setCurrent((await api.newThread()).id);
    setMode("chat");
    setView("chat");
  }, []);

  useEffect(() => {
    if (current === null) void startNew();
  }, [current, startNew]);

  const all =
    current && !threads.some((t) => t.id === current)
      ? [{ id: current, kind: "session", title: "New chat", updated_at: "" }, ...threads]
      : threads;
  const inMode = (m: "chat" | "slack") => all.filter((t) => (t.kind === "slack") === (m === "slack"));
  const visible = inMode(mode);
  const open = all.find((t) => t.id === current && visible.includes(t));

  const switchMode = (m: "chat" | "slack") => {
    setMode(m);
    setView("chat");
    const first = inMode(m)[0];
    if (!all.some((t) => t.id === current && inMode(m).includes(t)) && first) setCurrent(first.id);
  };

  return (
    <div className="app">
      <aside className="sidebar">
        <Brand />
        <div className="view-tabs" role="tablist" aria-label="Sections">
          {(["chat", "memory", "messages", "docs"] as const).map((id) => (
            <button key={id} role="tab" aria-selected={view === id} onClick={() => setView(id)}>
              {id === "chat" ? "Conversations" : TABS[id].label}
            </button>
          ))}
        </div>
        <div className="mode-switch" role="tablist" aria-label="Conversation source">
          {(["chat", "slack"] as const).map((m) => (
            <button key={m} role="tab" aria-selected={mode === m} onClick={() => switchMode(m)}>
              {m === "chat" ? "Chat" : "Slack"}
            </button>
          ))}
        </div>
        {mode === "chat" && (
          <button className="new-chat" onClick={startNew}>
            + New chat
          </button>
        )}
        <nav>
          {visible.map((t) => (
            <button
              key={t.id}
              className={`thread-link${view === "chat" && t.id === current ? " active" : ""}`}
              onClick={() => { setCurrent(t.id); setView("chat"); }}
              title={t.title}
            >
              {t.title}
            </button>
          ))}
          {visible.length === 0 && <p className="nav-empty">No Slack conversations yet.</p>}
        </nav>
        <div className="account">
          <span className="account-name">Test user</span>
          <button className="account-logout" onClick={onLogout}>
            Log out
          </button>
        </div>
      </aside>
      <main className="main">
        {view === "chat"
          ? open
            ? <Chat key={open.id} threadId={open.id} onSent={refresh} />
            : <div className="empty"><p>Select a conversation.</p></div>
          : <Browser key={view} tab={view} />}
      </main>
    </div>
  );
}

export function App() {
  const [loggedIn, setLoggedIn] = useState(loadSession);

  const login = () => {
    saveSession(true);
    setLoggedIn(true);
  };
  const logout = () => {
    saveSession(false);
    setLoggedIn(false);
  };

  return loggedIn ? <Workspace onLogout={logout} /> : <Login onLogin={login} />;
}
