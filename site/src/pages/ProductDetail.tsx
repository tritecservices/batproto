import { Link, useTitle } from "../router";
import { SITE } from "../config";
import type { Product } from "../data/products";
import { Icon } from "../components/Icon";
import { StatusBadge } from "../components/ProductCard";

export function ProductDetail({ product: p }: { product: Product }) {
  useTitle(`${p.name} — ${SITE.name}`);
  const cta = p.status === "available" ? "Get in touch" : "Register interest";
  return (
    <article className="section page-top">
      <div className="wrap narrow">
        <Link to="/products" className="back">← All products</Link>
        <div className="detail-head">
          <span className="icon-tile lg"><Icon name={p.icon} size={30} /></span>
          <div>
            <h1>{p.name}</h1>
            <StatusBadge status={p.status} />
          </div>
        </div>
        <p className="lede">{p.summary}</p>

        <div className="problem">
          <h2>The problem</h2>
          <p>{p.problem}</p>
        </div>

        <h2>What it does</h2>
        <ul className="ticks">
          {p.features.map((f) => <li key={f}>{f}</li>)}
        </ul>

        <div className="facts">
          <div>
            <h3>Works with</h3>
            <ul className="chips">{p.worksWith.map((w) => <li key={w}>{w}</li>)}</ul>
          </div>
          <div>
            <h3>How it ships</h3>
            <p>{p.delivery}</p>
          </div>
          <div>
            <h3>What it won't do</h3>
            <p>{p.notFor}</p>
          </div>
        </div>

        <div className="cta-panel small">
          <p>{p.status === "available" ? "Want to know more?" : "Want early access, or to tell us how you'd use it?"}</p>
          <Link to={`/contact?interest=${p.slug}`} className="btn btn-primary">{cta}</Link>
        </div>
      </div>
    </article>
  );
}
