import { Link, useTitle } from "../router";

export function NotFound() {
  useTitle("Page not found");
  return (
    <section className="section page-top">
      <div className="wrap narrow">
        <h1>That page isn't here</h1>
        <p className="lede">It may have moved. Try the product list instead.</p>
        <Link to="/products" className="btn btn-primary">See products</Link>
      </div>
    </section>
  );
}
