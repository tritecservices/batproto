import { useTitle } from "../router";
import { SITE } from "../config";
import { PRODUCTS } from "../data/products";
import { ProductCard } from "../components/ProductCard";

export function Products() {
  useTitle(`Products — ${SITE.name}`);
  return (
    <section className="section page-top">
      <div className="wrap">
        <p className="eyebrow">Products</p>
        <h1>Tools for survey teams</h1>
        <p className="lede">
          Status badges are honest: <em>planned</em> means we're gathering requirements, and
          we'd like to hear yours.
        </p>
        <div className="grid-cards">
          {PRODUCTS.map((p) => <ProductCard key={p.slug} product={p} />)}
        </div>
      </div>
    </section>
  );
}
