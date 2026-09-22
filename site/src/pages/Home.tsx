import { Link, useTitle } from "../router";
import { SITE } from "../config";
import { PRODUCTS } from "../data/products";
import { ProductCard } from "../components/ProductCard";
import { HeroArt } from "../components/HeroArt";

const STACK = ["QGIS", "ArcGIS Pro", "Google Earth Pro", "Bat acoustic classifiers", "Sony AVCHD video", "GUANO metadata", "SQL Server Express", "Microsoft 365"];

export function Home() {
  useTitle(`${SITE.name} — software for ecologists`);
  return (
    <>
      <section className="hero">
        <div className="wrap hero-grid">
          <div>
            <p className="eyebrow">IT and software for ecological consultancies</p>
            <h1>Less time fixing GIS.<br />More time in the field.</h1>
            <p className="lede">
              We run IT for ecologists, so we see the same problems every survey season:
              broken project links, detector metadata nobody checked, and grid references that
              should never reach a public report. Now we're turning the fixes into tools.
            </p>
            <div className="cta-row">
              <Link to="/products" className="btn btn-primary">See the tools</Link>
              <Link to="/contact" className="btn btn-ghost">Talk to us</Link>
            </div>
          </div>
          <HeroArt />
        </div>
      </section>

      <section className="band">
        <div className="wrap">
          <p className="band-label">Built around the software you already use</p>
          <ul className="stack-list">
            {STACK.map((s) => <li key={s}>{s}</li>)}
          </ul>
        </div>
      </section>

      <section className="section">
        <div className="wrap">
          <div className="section-head">
            <h2>Tools</h2>
            <p className="muted">Each one started as a support ticket we kept seeing.</p>
          </div>
          <div className="grid-cards">
            {PRODUCTS.map((p) => <ProductCard key={p.slug} product={p} />)}
          </div>
        </div>
      </section>

      <section className="section alt">
        <div className="wrap three-col">
          <div>
            <h3>From real support work</h3>
            <p>Every product starts with a problem our clients raised more than once. We only build what saves real hours.</p>
          </div>
          <div>
            <h3>Careful with sensitive data</h3>
            <p>Protected-species locations are treated as sensitive by default. Our tools flag them; they never publish them.</p>
          </div>
          <div>
            <h3>Your data stays yours</h3>
            <p>Desktop tools run on your machines. Nothing is uploaded unless you choose a feature that says it will.</p>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="wrap cta-panel">
          <div>
            <h2>Shape what we build next</h2>
            <p>Early users get a say in the roadmap and founding-customer pricing.</p>
          </div>
          <Link to="/contact" className="btn btn-gold">Register interest</Link>
        </div>
      </section>
    </>
  );
}
