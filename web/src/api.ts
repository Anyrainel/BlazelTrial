// The only place the UI talks to the backend (FastAPI in src/chatbot/surfaces/http.py).

export type Thread = { id: string; kind: string; title: string; updated_at: string };
export type Msg = { role: "user" | "assistant"; text: string; created_at: string };

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export const listThreads = () => fetch("/threads").then(json<Thread[]>);
export const newThread = () => fetch("/threads", { method: "POST" }).then(json<Thread>);
export const history = (id: string) =>
  fetch(`/threads/${encodeURIComponent(id)}/messages`).then(json<Msg[]>);
export const send = (threadId: string, text: string) =>
  fetch("/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId, text }),
  }).then(json<{ reply: string }>);
