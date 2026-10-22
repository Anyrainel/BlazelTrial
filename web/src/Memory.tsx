import { useEffect, useState } from "react";
import * as api from "./api";

const label = (value: string) => value.replaceAll("_", " ");

export function Memory() {
  const [inventory, setInventory] = useState<api.Inventory | null>(null);
  const [category, setCategory] = useState("records");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<api.ArtifactPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let active = true;
    setError(null);
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

  return (
    <section className="memory-browser" aria-label="Memory and artifacts">
      <header className="memory-heading">
        <div><h1>Memory &amp; artifacts</h1><p>Inspect what Hazel has stored, including earlier versions and each conversation’s working state.</p></div>
        <button onClick={() => setRevision((n) => n + 1)}>Refresh</button>
      </header>
      <div className="memory-categories" aria-label="Artifact categories">
        {inventory?.categories.map((c) => (
          <button key={c.id} aria-pressed={category === c.id} onClick={() => { setCategory(c.id); setOffset(0); }}>
            {c.label} <span>{c.count}</span>
          </button>
        ))}
      </div>
      <label className="memory-search">Search this category
        <input type="search" value={query} onChange={(e) => { setQuery(e.target.value); setOffset(0); }} placeholder="Name, topic, document or conversation…" />
      </label>
      {error && <p role="alert" className="error">Could not load artifacts: {error}</p>}
      {!page && !error && <p role="status">Loading artifacts…</p>}
      {page && <>
        <p className="memory-count">{page.total} matching {page.total === 1 ? "item" : "items"} · Read only</p>
        {page.total === 0 && <div className="artifact-empty">Nothing stored in this category{query ? " matching your search" : " yet"}.</div>}
        {page.items.map((item) => {
          const body = item.text ?? item.content ?? item.excerpt;
          return <article className="artifact-card" key={String(item.id)}>
            <h2>{String(item.key ?? item.doc_id ?? item.tool_name ?? item.recipient ?? `Item ${item.id}`)}</h2>
            {item.superseded_by != null && <span className="artifact-badge">Earlier version → {item.superseded_by}</span>}
            {body != null && <pre>{String(body)}</pre>}
            <dl>{Object.entries(item).filter(([key, value]) => value != null && !["text", "content", "excerpt"].includes(key)).map(([key, value]) => (
              <div key={key}><dt>{label(key)}</dt><dd>{String(value)}</dd></div>
            ))}</dl>
          </article>;
        })}
        <div className="memory-pagination">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - page.limit))}>Previous</button>
          <span>{page.total ? `${offset + 1}–${Math.min(offset + page.limit, page.total)} of ${page.total}` : "0 items"}</span>
          <button disabled={offset + page.limit >= page.total} onClick={() => setOffset(offset + page.limit)}>Next</button>
        </div>
      </>}
      <details className="artifact-card"><summary>Document library ({inventory?.files.length ?? 0})</summary>
        {inventory?.files.length ? <ul>{inventory.files.map((file) => <li key={file}>{file}</li>)}</ul> : <p>No document files yet.</p>}
      </details>
      <details className="artifact-card"><summary>Capabilities &amp; instructions</summary>
        <section><h2>System instructions</h2><pre>{inventory?.system_instructions}</pre></section>
        {inventory?.capabilities.map((c) => <section key={c.name}><h2>{c.name}</h2><p>{c.description}</p><pre>{c.instructions}</pre></section>)}
      </details>
    </section>
  );
}
