import { useRef, useState, type FormEvent } from "react";
import { Link, useTitle } from "../router";
import { BRAIN_API_URL, SITE } from "../config";

interface Hit {
  source: string;
  title: string | null;
  url: string | null;
  author: string | null;
  created_at: number | null;
  license: string | null;
  excerpt: string;
}

const SOURCE_LABEL: Record<string, string> = {
  stackexchange: "GIS Stack Exchange",
  discourse: "OSGeo QGIS forum",
  github: "QGIS GitHub issue",
};

const EXAMPLES = [
  "QGIS crashes opening a large GeoPackage",
  "layer datasource broken after moving project",
  "reproject British National Grid EPSG:27700",
];

// Used only when VITE_BRAIN_API_URL is unset (local dev, preview deploys). Clearly
// labelled on screen so nobody mistakes it for live results.
const SAMPLE: Hit[] = [
  {
    source: "stackexchange",
    title: "Fixing broken layer paths after moving a QGIS project",
    url: "https://gis.stackexchange.com/questions/tagged/qgis",
    author: null,
    created_at: null,
    license: "CC BY-SA",
    excerpt: "Sample result. When a project opens with unavailable layers, QGIS offers the Handle Unavailable Layers dialog, where each layer's source can be pointed at its new location…",
  },
];

export function Demo() {
  useTitle(`Try the search — ${SITE.name}`);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [message, setMessage] = useState("");
  const inflight = useRef<AbortController | null>(null);
  const live = Boolean(BRAIN_API_URL);

  async function run(query: string) {
    const text = query.trim();
    if (text.length < 3) {
      setState("error");
      setMessage("Type at least three characters.");
      return;
    }
    if (!live) {
      setHits(SAMPLE);
      setState("idle");
      return;
    }
    inflight.current?.abort();
    const ctl = new AbortController();
    inflight.current = ctl;
    setState("loading");
    setMessage("");
    try {
      const res = await fetch(`${BRAIN_API_URL}/public/search?q=${encodeURIComponent(text)}`, {
        signal: ctl.signal,
      });
      if (res.status === 429) throw new Error("Lots of searches just now. Try again in a minute.");
      if (!res.ok) throw new Error("The search service isn't available right now.");
      const data: { hits: Hit[] } = await res.json();
      setHits(data.hits);
      setState("idle");
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setState("error");
      // fetch() rejects with a browser-specific TypeError when the service is unreachable
      const msg = err instanceof TypeError ? "The search service isn't reachable right now." : (err as Error).message;
      setMessage(msg || "Something went wrong.");
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    run(q);
  }

  return (
    <section className="section page-top">
      <div className="wrap narrow">
        <p className="eyebrow">Demo</p>
        <h1>Search GIS fixes the way our support team does</h1>
        <p className="lede">
          This searches public GIS community answers we've indexed. Our support team's
          version also includes our own ticket history; this one never does.
        </p>

        <form className="search" onSubmit={onSubmit} role="search">
          <label htmlFor="q" className="sr">Describe the problem or paste the error</label>
          <input id="q" value={q} onChange={(e) => setQ(e.target.value)} maxLength={200}
            placeholder="Paste an error message or describe the problem" autoComplete="off" />
          <button className="btn btn-primary" disabled={state === "loading"}>
            {state === "loading" ? "Searching…" : "Search"}
          </button>
        </form>
        <div className="examples">
          <span className="muted">Try:</span>
          {EXAMPLES.map((ex) => (
            <button key={ex} type="button" className="chip-btn" onClick={() => { setQ(ex); run(ex); }}>
              {ex}
            </button>
          ))}
        </div>

        {!live && (
          <p className="notice">Showing a sample result. Live search appears once the site is connected to the search service.</p>
        )}
        {state === "error" && <p className="notice error" role="alert">{message}</p>}

        <div aria-live="polite">
          {hits && hits.length === 0 && <p className="muted">No matches. Try the exact error text.</p>}
          {hits && hits.length > 0 && (
            <ol className="results">
              {hits.map((h, i) => (
                <li key={(h.url ?? "") + i} className="card result">
                  <div className="result-meta">
                    <span className="src">{SOURCE_LABEL[h.source] ?? h.source}</span>
                    {h.created_at && <span>{new Date(h.created_at * 1000).getFullYear()}</span>}
                    {h.license && <span>{h.license}</span>}
                  </div>
                  <h3>
                    {h.url ? <a href={h.url} target="_blank" rel="noopener noreferrer">{h.title ?? "Untitled"}</a> : h.title}
                  </h3>
                  <p className="excerpt">{h.excerpt}</p>
                  {h.author && <p className="attr">By {h.author}. Follow the link for the full answer.</p>}
                </li>
              ))}
            </ol>
          )}
        </div>

        <div className="cta-panel small">
          <p>Want this for your team, with your own history in it?</p>
          <Link to="/contact?interest=managed-gis-support" className="btn btn-primary">Ask about support</Link>
        </div>
      </div>
    </section>
  );
}
