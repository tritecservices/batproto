import { Link } from "../router";
import { STATUS_LABEL, type Product } from "../data/products";
import { Icon } from "./Icon";

export function StatusBadge({ status }: { status: Product["status"] }) {
  return <span className={`badge badge-${status}`}>{STATUS_LABEL[status]}</span>;
}

export function ProductCard({ product }: { product: Product }) {
  return (
    <Link to={`/products/${product.slug}`} className="card product-card">
      <div className="card-top">
        <span className="icon-tile"><Icon name={product.icon} /></span>
        <StatusBadge status={product.status} />
      </div>
      <h3>{product.name}</h3>
      <p>{product.summary}</p>
      <span className="card-more">Read more <span aria-hidden="true">→</span></span>
    </Link>
  );
}
