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
import { Login, loadHandle, saveHandle } from "./Login";

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

function Workspace({ handle, onLogout }: { handle: string; onLogout: () => void }) {
  const [threads, setThreads] = useState<api.Thread[]>([]);
  const [current, setCurrent] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api.listThreads().then(setThreads).catch(() => setThreads([]));
  }, []);

  useEffect(refresh, [refresh]);

  const startNew = useCallback(async () => setCurrent((await api.newThread()).id), []);

  useEffect(() => {
    if (current === null) void startNew();
  }, [current, startNew]);

  const visible =
    current && !threads.some((t) => t.id === current)
      ? [{ id: current, kind: "session", title: "New chat", updated_at: "" }, ...threads]
      : threads;

  return (
    <div className="app">
      <aside className="sidebar">
        <Brand />
        <button className="new-chat" onClick={startNew}>
          + New chat
        </button>
        <nav>
          {visible.map((t) => (
            <button
              key={t.id}
              className={`thread-link${t.id === current ? " active" : ""}`}
              onClick={() => setCurrent(t.id)}
              title={t.title}
            >
              <span className="thread-kind">{t.kind === "slack" ? "Slack" : "Chat"}</span>
              {t.title}
            </button>
          ))}
        </nav>
        <div className="account">
          <span className="account-handle" title={`@${handle}`}>
            @{handle}
          </span>
          <button className="account-logout" onClick={onLogout}>
            Log out
          </button>
        </div>
      </aside>
      <main className="main">
        {current && <Chat key={current} threadId={current} onSent={refresh} />}
      </main>
    </div>
  );
}

export function App() {
  const [handle, setHandle] = useState<string | null>(loadHandle);

  const login = (h: string) => {
    saveHandle(h);
    setHandle(h);
  };
  const logout = () => {
    saveHandle(null);
    setHandle(null);
  };

  return handle ? <Workspace handle={handle} onLogout={logout} /> : <Login onLogin={login} />;
}
