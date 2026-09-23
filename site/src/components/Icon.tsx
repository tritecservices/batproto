import type { Product } from "../data/products";

// Simple line icons drawn for this site. stroke="currentColor" so they theme.
const PATHS: Record<Product["icon"], string> = {
  path: "M4 18c3 0 3-6 6-6s3 6 6 6 3-12 4-12M4 6h4M16 18h4",
  wave: "M3 12h2l2-6 3 12 3-9 2 5 2-2h4",
  shield: "M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6l7-3zM9 12l2 2 4-4",
  moon: "M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5zM16 4v3M14.5 5.5h3",
  chat: "M4 5h16v10H9l-5 4V5zM8 9h8M8 12h5",
};

export function Icon({ name, size = 24 }: { name: Product["icon"]; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  );
}
