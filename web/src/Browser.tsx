import { useEffect, useState } from "react";
import * as api from "./api";

export type TabId = "memory" | "messages" | "docs";

type TabSpec = {
  label: string;
  description: string;
  categories: string[];
  search: string;
};

// What matters to the user, per tab. Internal stores (tool outputs, delivery receipts, model
// calls, legacy rows, ...) stay behind the inventory API and are not shown.
export const TABS: Record<TabId, TabSpec> = {
  memory: {
    label: "Memory",
    description: "What Hazel remembers about you, including earlier versions of what changed.",
    categories: ["records", "notes"],
    search: "Fact, preference or topic…",
  },
  messages: {
    label: "Messages",
    description: "Messages and LinkedIn posts Hazel wrote for you. Messages are logged, not delivered.",
    categories: ["outbound", "posts"],
    search: "Recipient or text…",
  },
  docs: {
    label: "Docs",
    description: "Documents available to Hazel in the workspace docs folder.",
    categories: [],
    search: "",
  },
};

const HIDDEN = new Set([
  "id", "text", "content", "excerpt", "key", "recipient", "superseded_by", "supersedes_id",
  "legacy_fact_id", "origin_session_id", "start_message_id", "end_message_id",
]);
const label = (value: string) => value.replaceAll("_", " ");

function heading(category: string, item: api.Artifact): string {
  if (category === "outbound") return `To ${item.recipient}`;
  if (category === "posts") return "LinkedIn post";
  if (category === "notes") return "Conversation note";
  return String(item.key ?? `Item ${item.id}`);
}

function Card({ category, item }: { category: string; item: api.Artifact }) {
  const body = item.text ?? item.content ?? item.excerpt;
  const meta = Object.entries(item).filter(([key, value]) => value != null && !HIDDEN.has(key));
  return (
    <article className="artifact-card">
      <h2>{heading(category, item)}</h2>
      {item.superseded_by != null && <span className="artifact-badge">Earlier version — since replaced</span>}
      {body != null && <pre>{String(body)}</pre>}
      <dl>
        {meta.map(([key, value]) => (
          <div key={key}><dt>{label(key)}</dt><dd>{String(value)}</dd></div>
        ))}
      </dl>
    </article>
  );
}

const size = (bytes: number) => (bytes < 1024 ? `${bytes} B` : `${Math.round(bytes / 1024)} KB`);

function DocCard({ file }: { file: api.DocFile }) {
  const [view, setView] = useState<api.DocView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = (open: boolean) => {
    if (open && !view) api.doc(file.name).then(setView).catch((e: unknown) => setError(String(e)));
  };
  return (
    <details className="artifact-card" onToggle={(e) => load(e.currentTarget.open)}>
      <summary>{file.name} <span className="artifact-badge">{size(file.bytes)}</span></summary>
      {error && <p role="alert" className="error">Could not load: {error}</p>}
      {!view && !error && <p role="status">Loading…</p>}
      {view && <pre>{view.text}{view.truncated && "\n\n… (preview truncated)"}</pre>}
    </details>
  );
}

function Docs() {
  const [files, setFiles] = useState<api.DocFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let active = true;
    api.docs().then((data) => { if (active) { setFiles(data); setError(null); } })
      .catch((e: unknown) => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [revision]);

  return (
    <Shell tab="docs" onRefresh={() => setRevision((n) => n + 1)}>
      {error && <p role="alert" className="error">Could not load: {error}</p>}
      {!files && !error && <p role="status">Loading…</p>}
      {files?.length === 0 && <div className="artifact-empty">No documents in the docs folder yet.</div>}
      {files && files.length > 0 && <p className="memory-count">{files.length} {files.length === 1 ? "document" : "documents"}</p>}
      {files?.map((f) => <DocCard key={f.name} file={f} />)}
    </Shell>
  );
}

function Shell({ tab, onRefresh, children }: { tab: TabId; onRefresh: () => void; children: React.ReactNode }) {
  const spec = TABS[tab];
  return (
    <section className="memory-browser" aria-label={spec.label}>
      <header className="memory-heading">
        <div><h1>{spec.label}</h1><p>{spec.description}</p></div>
        <button onClick={onRefresh}>Refresh</button>
      </header>
      {children}
    </section>
  );
}

function Artifacts({ tab }: { tab: Exclude<TabId, "docs"> }) {
  const spec = TABS[tab];
  const [inventory, setInventory] = useState<api.Inventory | null>(null);
  const [category, setCategory] = useState(spec.categories[0]);
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<api.ArtifactPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let active = true;
    api.inventory().then((data) => { if (active) setInventory(data); })
      .catch((e: unknown) => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [revision]);

  useEffect(() => {
    let active = true;
    setPage(null);
    setError(null);
    api.artifacts(category, query, offset).then((data) => { if (active) setPage(data); })
      .catch((e: unknown) => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [category, query, offset, revision]);

  const categories = spec.categories
    .map((id) => inventory?.categories.find((c) => c.id === id))
    .filter((c): c is NonNullable<typeof c> => c !== undefined);

  return (
    <Shell tab={tab} onRefresh={() => setRevision((n) => n + 1)}>
      <div className="memory-categories" role="tablist" aria-label={`${spec.label} categories`}>
        {categories.map((c) => (
          <button key={c.id} role="tab" aria-selected={category === c.id}
            onClick={() => { setCategory(c.id); setOffset(0); }}>
            {c.label} <span>{c.count}</span>
          </button>
        ))}
      </div>
      <label className="memory-search">Search
        <input type="search" value={query} onChange={(e) => { setQuery(e.target.value); setOffset(0); }} placeholder={spec.search} />
      </label>
      {error && <p role="alert" className="error">Could not load: {error}</p>}
      {!page && !error && <p role="status">Loading…</p>}
      {page && <>
        <p className="memory-count">{page.total} matching {page.total === 1 ? "item" : "items"}</p>
        {page.total === 0 && <div className="artifact-empty">Nothing here{query ? " matching your search" : " yet"}.</div>}
        {page.items.map((item) => <Card key={String(item.id)} category={category} item={item} />)}
        <div className="memory-pagination">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - page.limit))}>Previous</button>
          <span>{page.total ? `${offset + 1}–${Math.min(offset + page.limit, page.total)} of ${page.total}` : "0 items"}</span>
          <button disabled={offset + page.limit >= page.total} onClick={() => setOffset(offset + page.limit)}>Next</button>
        </div>
      </>}
    </Shell>
  );
}

export function Browser({ tab }: { tab: TabId }) {
  return tab === "docs" ? <Docs /> : <Artifacts tab={tab} />;
}
