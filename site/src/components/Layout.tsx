import { useEffect, useState, type ReactNode } from "react";
import { Link } from "../router";
import { SITE } from "../config";

const NAV = [
  { to: "/products", label: "Products" },
  { to: "/demo", label: "Try the search" },
  { to: "/contact", label: "Contact" },
];

export function Layout({ path, children }: { path: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  useEffect(() => setOpen(false), [path]);

  return (
    <>
      <a className="skip" href="#main">Skip to content</a>
      <header className="site-header">
        <div className="wrap header-row">
          <Link to="/" className="brand" aria-label={`${SITE.name} home`}>
            <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true">
              <rect width="32" height="32" rx="7" fill="var(--forest)" />
              <path d="M5 22c4-1 6-5 11-5s7 4 11 5" fill="none" stroke="var(--gold)" strokeWidth="2.4" strokeLinecap="round" />
              <path d="M9 13c2-3 5-5 7-5s5 2 7 5" fill="none" stroke="var(--sage)" strokeWidth="2" strokeLinecap="round" />
            </svg>
            <span>{SITE.name}</span>
          </Link>
          <button className="menu-btn" aria-expanded={open} aria-controls="nav"
            onClick={() => setOpen((o) => !o)}>
            <span className="sr">Menu</span>
            <svg width="22" height="22" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              {open ? <path d="M6 6l12 12M18 6L6 18" /> : <path d="M4 7h16M4 12h16M4 17h16" />}
            </svg>
          </button>
          <nav id="nav" className={open ? "nav open" : "nav"} aria-label="Main">
            {NAV.map((n) => (
              <Link key={n.to} to={n.to}
                aria-current={path === n.to || path.startsWith(n.to + "/") ? "page" : undefined}>
                {n.label}
              </Link>
            ))}
          </nav>
        </div>
      </header>

      <main id="main">{children}</main>

      <footer className="site-footer">
        <div className="wrap footer-grid">
          <div>
            <strong>{SITE.name}</strong>
            <p className="muted">{SITE.tagline}</p>
          </div>
          <div className="footer-links">
            <Link to="/products">Products</Link>
            <Link to="/contact">Contact</Link>
            <Link to="/legal">Privacy &amp; legal</Link>
          </div>
          <p className="fineprint">
            © {new Date().getFullYear()} {SITE.company}. QGIS, ArcGIS, Google Earth and other
            product names are trademarks of their respective owners and are used only to
            describe compatibility. {SITE.name} is not affiliated with or endorsed by them.
          </p>
        </div>
      </footer>
    </>
  );
}
