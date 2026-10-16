import {
  BatteryCharging,
  Cable,
  Car,
  Gift,
  HardDrive,
  Headphones,
  Keyboard,
  Laptop,
  Monitor,
  Mouse,
  Package,
  RectangleHorizontal,
  ShieldPlus,
  SprayCan,
  Usb,
  Webcam,
} from "lucide-react";
import { money } from "@/lib/api";

export type Product = { id: string; title: string; price: number; currency?: string; kind?: string };

// What a product is, from its title: the first matching word picks the icon.
const ICONS: [RegExp, typeof Package][] = [
  [/gift card/i, Gift],
  [/warranty|care:/i, ShieldPlus],
  [/dock/i, Monitor],
  [/hub|adapter|reader/i, Usb],
  [/cable|hdmi/i, Cable],
  [/charger|power bank/i, BatteryCharging],
  [/ssd|drive/i, HardDrive],
  [/mouse/i, Mouse],
  [/keyboard|wrist rest/i, Keyboard],
  [/earbuds|headphone/i, Headphones],
  [/webcam|camera/i, Webcam],
  [/stand|arm/i, Laptop],
  [/car mount/i, Car],
  [/cleaning/i, SprayCan],
  [/mat/i, RectangleHorizontal],
];

function Glyph({ p }: { p: Product }) {
  const Icon = ICONS.find(([re]) => re.test(p.title))?.[1] ?? (p.kind === "digital" ? Gift : Package);
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

// After the agent answers, show the products it talked about (search returns more than it recommends).
export function mentioned(items: Product[], reply: string | undefined): Product[] {
  if (!reply) return items;
  // models write "10‑in‑1" with non-breaking hyphens and narrow spaces: compare letters and digits only
  const plain = (t: string) => ` ${t.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()} `;
  const text = plain(reply);
  const named = items.filter((p) => text.includes(plain(p.title.replace(/^northwind /i, ""))));
  return named.length ? named : items.slice(0, 3);
}
