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

export type Inventory = { categories: { id: string; label: string; count: number }[] };
export type DocFile = { name: string; bytes: number; modified: number };
export type DocView = { name: string; text: string; truncated: boolean };
export const docs = () => fetch("/agent/docs").then(json<DocFile[]>);
export const doc = (name: string) => fetch(`/agent/docs/${encodeURIComponent(name)}`).then(json<DocView>);
export type Artifact = Record<string, string | number | boolean | null>;
export type ArtifactPage = { items: Artifact[]; total: number; offset: number; limit: number };
export const inventory = () => fetch("/agent/inventory").then(json<Inventory>);
export const artifacts = (category: string, q: string, offset: number) =>
  fetch(`/agent/inventory/${encodeURIComponent(category)}?${new URLSearchParams({ q, offset: String(offset) })}`)
    .then(json<ArtifactPage>);
