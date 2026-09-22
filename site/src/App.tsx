import { Layout } from "./components/Layout";
import { usePath } from "./router";
import { Home } from "./pages/Home";
import { Products } from "./pages/Products";
import { ProductDetail } from "./pages/ProductDetail";
import { Demo } from "./pages/Demo";
import { Contact } from "./pages/Contact";
import { Legal } from "./pages/Legal";
import { NotFound } from "./pages/NotFound";
import { PRODUCTS } from "./data/products";

export default function App() {
  const path = usePath().replace(/\/+$/, "") || "/";

  let page;
  if (path === "/") page = <Home />;
  else if (path === "/products") page = <Products />;
  else if (path.startsWith("/products/")) {
    const product = PRODUCTS.find((p) => `/products/${p.slug}` === path);
    page = product ? <ProductDetail product={product} /> : <NotFound />;
  } else if (path === "/demo") page = <Demo />;
  else if (path === "/contact") page = <Contact />;
  else if (path === "/legal") page = <Legal />;
  else page = <NotFound />;

  return <Layout path={path}>{page}</Layout>;
}
