import { Cable, Gift, Package, Usb } from "lucide-react";
import { money } from "@/lib/api";

export type Product = { id: string; title: string; price: number; currency?: string; kind?: string };

function Glyph({ p }: { p: Product }) {
  const t = p.title.toLowerCase();
  const Icon = p.kind === "digital" || t.includes("gift") ? Gift : t.includes("cable") ? Cable : t.includes("hub") ? Usb : Package;
  return <Icon className="size-5" aria-hidden />;
}

// Search results, as small cards the shopper can pick from.
export function Products({ items, onPick }: { items: Product[]; onPick: (p: Product) => void }) {
  if (!items.length) return null;
  return (
    <ul className="flex flex-wrap gap-2" aria-label="Products found">
      {items.slice(0, 6).map((p) => (
        <li key={p.id}>
          <button
            type="button"
            onClick={() => onPick(p)}
            className="glass press flex w-[208px] items-center gap-3 rounded-[var(--r-card)] p-2.5 text-left"
            title={`Ask about ${p.title}`}
          >
            <span className="grid size-10 shrink-0 place-items-center rounded-[12px] bg-white/10 text-label">
              <Glyph p={p} />
            </span>
            <span className="min-w-0">
              <span className="line-clamp-2 text-[13.5px] leading-tight">{p.title}</span>
              <span className="tabular text-[13px] text-label-2">{money(p.price, p.currency)}</span>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

export function parseProducts(content: string): Product[] {
  try {
    const data = JSON.parse(content);
    return Array.isArray(data) ? data.filter((p) => p && typeof p.id === "string" && typeof p.price === "number") : [];
  } catch {
    return [];
  }
}
