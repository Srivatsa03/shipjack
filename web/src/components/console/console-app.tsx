"use client";

import {
  Activity,
  ArrowLeft,
  BookOpen,
  Bot,
  Check,
  CircleCheck,
  Cpu,
  Gauge,
  Inbox,
  MessagesSquare,
  Package,
  RotateCcw,
  ShieldCheck,
  Search,
  Truck,
  TriangleAlert,
  Upload,
  User as UserIcon,
  Webhook,
  X,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { LiquidLens } from "@/components/liquid-lens";
import {
  type Approval,
  ApiError,
  type CatalogResult,
  type KbHit,
  type Knowledge,
  type Metrics,
  money,
  type OpsThread,
  operator,
  type OrderRow,
  type StoredEvent,
  unreachable,
} from "@/lib/api";

type View = "approvals" | "orders" | "catalog" | "runs" | "metrics";
const VIEWS: { id: View; label: string; icon: typeof Inbox }[] = [
  { id: "approvals", label: "Refund requests", icon: Inbox },
  { id: "orders", label: "Orders", icon: Package },
  { id: "catalog", label: "Catalog & knowledge", icon: BookOpen },
  { id: "runs", label: "Conversations", icon: MessagesSquare },
  { id: "metrics", label: "Health", icon: Gauge },
];

export function ConsoleApp() {
  const [view, setView] = useState<View>("approvals");
  const [pending, setPending] = useState<Approval[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refreshPending = useCallback(async () => {
    try {
      setPending(await operator.approvals("pending"));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : unreachable());
    }
  }, []);

  useEffect(() => {
    const first = window.setTimeout(refreshPending, 0);
    const timer = window.setInterval(refreshPending, 5000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [refreshPending]);

  return (
    <div className="fixed inset-0 flex">
      <aside className="glass m-3 hidden w-[248px] shrink-0 flex-col rounded-[var(--r-panel)] p-2.5 md:flex" aria-label="Console">
        <div className="flex items-center gap-2.5 px-2.5 pt-2 pb-4">
          <span
            aria-hidden
            className="grid size-8 place-items-center rounded-[10px] bg-gradient-to-b from-[#3d7bff] to-[#2450d6] text-[14px] font-semibold text-white"
          >
            N
          </span>
          <div>
            <p className="text-[14.5px] leading-tight font-semibold">Northwind</p>
            <p className="text-[12px] text-label-3">Store console</p>
          </div>
        </div>
        <nav>
          <ul className="space-y-0.5">
            {VIEWS.map((v) => (
              <li key={v.id}>
                <button
                  type="button"
                  onClick={() => setView(v.id)}
                  aria-current={view === v.id}
                  className={
                    "flex w-full items-center gap-2.5 rounded-[var(--r-row)] px-3 py-2 text-left text-[14px] transition-colors " +
                    (view === v.id ? "bg-white/14 text-label" : "text-label-2 hover:bg-white/7 hover:text-label")
                  }
                >
                  <v.icon className="size-4" aria-hidden />
                  <span className="flex-1">{v.label}</span>
                  {v.id === "approvals" && pending.length > 0 && (
                    <span className="tabular min-w-5 rounded-full bg-orange px-1.5 text-center text-[12px] font-semibold text-black">
                      {pending.length}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <Link
          href="/"
          className="glass-clear press mt-auto flex items-center gap-2 rounded-full px-3.5 py-2 text-[13.5px] text-label-2"
        >
          <ArrowLeft className="size-4" aria-hidden /> Shopper app
        </Link>
      </aside>

      <main className="thin-scroll min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[1080px] px-4 pt-6 pb-16 md:px-8">
          <nav className="mb-4 flex gap-2 overflow-x-auto md:hidden" aria-label="Console sections">
            {VIEWS.map((v) => (
              <button
                key={v.id}
                type="button"
                onClick={() => setView(v.id)}
                className={"glass-clear press shrink-0 rounded-full px-3.5 py-1.5 text-[13.5px] " + (view === v.id ? "text-label" : "text-label-2")}
              >
                {v.label}
              </button>
            ))}
          </nav>
          {error && (
            <p role="alert" className="glass mb-4 rounded-[var(--r-card)] px-4 py-3 text-[14px] text-[#ffb4ae]">
              {error}
            </p>
          )}
          {view === "approvals" && <Approvals pending={pending} onChange={refreshPending} />}
          {view === "orders" && <Orders />}
          {view === "catalog" && <Catalog />}
          {view === "runs" && <Runs />}
          {view === "metrics" && <Health />}
        </div>
      </main>
      <LiquidLens />
    </div>
  );
}

function Title({ children, sub }: { children: React.ReactNode; sub: string }) {
  return (
    <header className="mb-5">
      <h1 className="text-[28px] leading-tight font-semibold tracking-tight [text-shadow:0_2px_18px_rgb(0_0_0/0.4)]">{children}</h1>
      <p className="mt-1 text-[14.5px] text-label-2">{sub}</p>
    </header>
  );
}

// ---- refund requests ----------------------------------------------------------------------------------

function Approvals({ pending, onChange }: { pending: Approval[]; onChange: () => void }) {
  const [decided, setDecided] = useState<Approval[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    operator.approvals("").then((all) => live && setDecided(all.filter((a) => a.status !== "pending").slice(0, 12)));
    return () => {
      live = false;
    };
  }, [pending.length]);

  const decide = async (a: Approval, approved: boolean) => {
    setBusy(a.id);
    try {
      await operator.decide(a.id, approved, notes[a.id] ?? "");
    } finally {
      setBusy(null);
      onChange();
    }
  };

  return (
    <section>
      <Title sub="The support agent can only ask. Every refund waits here for a person on the Northwind team.">
        Refund requests
      </Title>
      {pending.length === 0 && (
        <div className="glass flex items-center gap-3 rounded-[var(--r-card)] px-5 py-4 text-[14.5px] text-label-2">
          <CircleCheck className="size-5 text-green" aria-hidden /> Nothing waiting. New requests appear here within seconds.
        </div>
      )}
      <ul className="grid gap-3 lg:grid-cols-2">
        {pending.map((a) => {
          const p = a.payload;
          return (
            <li key={a.id} className="glass-thick rounded-[var(--r-panel)] p-4">
              <div className="flex items-baseline justify-between gap-3 px-1">
                <h2 className="text-[16px] font-semibold">Refund {money(p.amount)}</h2>
                <span className="text-[12.5px] text-label-3">{new Date(a.created).toLocaleTimeString()}</span>
              </div>
              <dl className="row mt-3 divide-y divide-white/10 px-3.5 text-[14px]">
                <Pair k="Order" v={`${p.order_id} (${p.order_status})`} />
                <Pair k="Customer" v={p.user} />
                <Pair k="Paid" v={`${money(p.paid)}${p.already_refunded ? `, ${money(p.already_refunded)} refunded already` : ""}`} />
                <Pair k="Reason" v={p.reason || "No reason given"} />
              </dl>
              <label className="mt-3 block px-1 text-[13px] text-label-2" htmlFor={`note-${a.id}`}>
                Note to the customer (optional)
              </label>
              <input
                id={`note-${a.id}`}
                value={notes[a.id] ?? ""}
                onChange={(e) => setNotes({ ...notes, [a.id]: e.target.value })}
                placeholder="e.g. Photo shows the damage"
                className="row mt-1.5 w-full px-3.5 py-2.5 text-[14px] outline-none placeholder:text-label-3"
              />
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  disabled={busy === a.id}
                  onClick={() => decide(a, true)}
                  className="glass-tint press flex flex-1 items-center justify-center gap-1.5 rounded-full py-2.5 text-[15px] font-semibold"
                >
                  <Check className="size-4" strokeWidth={2.5} aria-hidden /> Refund {money(p.amount)}
                </button>
                <button
                  type="button"
                  disabled={busy === a.id}
                  onClick={() => decide(a, false)}
                  className="glass-clear press flex items-center gap-1.5 rounded-full px-5 py-2.5 text-[15px]"
                >
                  <X className="size-4" aria-hidden /> Decline
                </button>
              </div>
            </li>
          );
        })}
      </ul>

      {decided.length > 0 && (
        <>
          <h2 className="mt-8 mb-2 text-[15px] font-semibold text-label-2">Decided</h2>
          <ul className="glass divide-y divide-white/8 rounded-[var(--r-card)] px-4">
            {decided.map((a) => (
              <li key={a.id} className="flex items-center gap-3 py-2.5 text-[14px]">
                <span className={"size-2 shrink-0 rounded-full " + (a.status === "approved" ? "bg-green" : "bg-red")} aria-hidden />
                <span className="min-w-0 flex-1 truncate">
                  {a.status === "approved" ? "Refunded" : "Declined"} {money(a.payload.amount)} on {a.payload.order_id}
                  {a.note ? <span className="text-label-3">: {a.note}</span> : null}
                </span>
                <span className="text-[12.5px] text-label-3">{a.decided ? new Date(a.decided).toLocaleString() : ""}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function Pair({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex gap-3 py-2.5">
      <dt className="w-[78px] shrink-0 text-label-2">{k}</dt>
      <dd className="min-w-0">{v}</dd>
    </div>
  );
}

// ---- orders --------------------------------------------------------------------------------------------

function Orders() {
  const [rows, setRows] = useState<OrderRow[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const load = useCallback(() => operator.orders().then(setRows), []);

  useEffect(() => {
    const first = window.setTimeout(load, 0);
    const timer = window.setInterval(load, 4000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [load]);

  const act = async (o: OrderRow, action: "ship" | "deliver") => {
    setBusy(o.id);
    try {
      await operator.act(o.id, action, action === "ship" ? { carrier: "USPS", tracking_number: trackingNumber() } : {});
      setFlash(action === "ship" ? `Shipped ${o.id}. The customer's chat updates from the store's webhook.` : `Delivered ${o.id}.`);
    } catch (e) {
      setFlash(e instanceof ApiError ? e.message : "That didn't work.");
    } finally {
      setBusy(null);
      load();
    }
  };

  return (
    <section>
      <Title sub="Orders placed through the assistant. Status comes from the store's signed webhooks.">Orders</Title>
      {flash && <p className="mb-3 text-[14px] text-label-2">{flash}</p>}
      <div className="glass-thick overflow-x-auto rounded-[var(--r-panel)] p-2">
        <table className="w-full min-w-[640px] text-left text-[14px]">
          <thead className="text-[12.5px] text-label-3">
            <tr>
              <th className="px-3 py-2 font-medium">Item</th>
              <th className="px-3 py-2 font-medium">Order</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 text-right font-medium">Total</th>
              <th className="px-3 py-2 text-right font-medium">Refunded</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-white/8">
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-label-3">
                  No orders yet. Buy something in the shopper app.
                </td>
              </tr>
            )}
            {rows.map((o) => {
              const totals = Object.fromEntries((o.data.totals ?? []).map((t) => [t.type, t.amount]));
              return (
                <tr key={o.id}>
                  <td className="max-w-[240px] truncate px-3 py-2.5">{o.data.line_items?.[0]?.title ?? "Order"}</td>
                  <td className="tabular px-3 py-2.5 text-label-2">{o.id}</td>
                  <td className="px-3 py-2.5">
                    <StatusPill status={o.status} />
                  </td>
                  <td className="tabular px-3 py-2.5 text-right">{totals.total !== undefined ? money(totals.total) : ""}</td>
                  <td className="tabular px-3 py-2.5 text-right text-label-2">
                    {totals.amount_refunded ? money(totals.amount_refunded) : ""}
                  </td>
                  <td className="px-3 py-1.5 text-right">
                    {o.status === "created" && (
                      <button
                        type="button"
                        disabled={busy === o.id}
                        onClick={() => act(o, "ship")}
                        className="glass-clear press inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-[13.5px]"
                      >
                        <Truck className="size-4" aria-hidden /> Ship
                      </button>
                    )}
                    {o.status === "shipped" && (
                      <button
                        type="button"
                        disabled={busy === o.id}
                        onClick={() => act(o, "deliver")}
                        className="glass-clear press inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-[13.5px]"
                      >
                        <CircleCheck className="size-4" aria-hidden /> Mark delivered
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// A demo tracking number: the store has no carrier, so the operator's "Ship" makes one up.
function trackingNumber(): string {
  return `9400${String(Date.now()).slice(-14)}`;
}

function StatusPill({ status }: { status: string }) {
  const tone =
    status === "completed"
      ? "bg-green/18 text-green"
      : status === "shipped"
        ? "bg-teal/18 text-teal"
        : status === "canceled"
          ? "bg-red/18 text-red"
          : "bg-orange/18 text-orange";
  const label = { created: "Placed", shipped: "Shipped", completed: "Delivered", canceled: "Canceled" }[status] ?? status;
  return <span className={"rounded-full px-2.5 py-0.5 text-[12.5px] font-medium " + tone}>{label}</span>;
}

// ---- catalog and knowledge: onboarding a merchant -----------------------------------------------------------

function Catalog() {
  const [kb, setKb] = useState<Knowledge | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [slug, setSlug] = useState("");
  const [page, setPage] = useState("");
  const load = useCallback(() => operator.knowledge().then(setKb), []);

  useEffect(() => {
    const first = window.setTimeout(load, 0);
    return () => window.clearTimeout(first);
  }, [load]);

  const importCsv = async (csv: string, name: string) => {
    setBusy(true);
    setResult(null);
    try {
      const r: CatalogResult = await operator.importCatalog(csv);
      setResult(
        `Imported ${r.imported} products from ${name} (${r.format === "shopify" ? "a Shopify export" : "a simple CSV"}) ` +
          `and indexed them for search in ${r.seconds} s.` +
          (r.skipped.length ? ` Skipped ${r.skipped.length}: ${r.skipped.slice(0, 4).join("; ")}.` : ""),
      );
      load();
    } catch (e) {
      setResult(e instanceof ApiError ? e.message : "The import failed.");
    } finally {
      setBusy(false);
    }
  };

  const addPage = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await operator.addDocument(slug.trim(), page);
      setResult(`Added "${r.slug}" as ${r.chunks} searchable section${r.chunks === 1 ? "" : "s"} in ${r.seconds} s.`);
      setSlug("");
      setPage("");
      load();
    } catch (e) {
      setResult(e instanceof ApiError ? e.message : "That page couldn't be added.");
    } finally {
      setBusy(false);
    }
  };

  const products = kb?.data.filter((d) => d.kind === "product").length ?? 0;
  const pages = kb?.data.filter((d) => d.kind === "policy") ?? [];
  const locked = kb ? !kb.imports_allowed : false;

  return (
    <section>
      <Title sub="What the agents know about Northwind: its products and its policies. An import goes straight into the store's search.">
        Catalog & knowledge
      </Title>
      {result && (
        <p role="status" className="glass mb-4 rounded-[var(--r-card)] px-4 py-3 text-[14px]">
          {result}
        </p>
      )}
      <div className="grid gap-3 lg:grid-cols-2">
        <div className="glass-thick rounded-[var(--r-panel)] p-4">
          <h2 className="px-1 text-[16px] font-semibold">Import a catalog</h2>
          <p className="mt-1 px-1 text-[13.5px] text-label-2">
            A Shopify product export (Products, then Export) or a CSV with id, title and price. It replaces the
            catalog; orders keep what they were sold as.
          </p>
          {locked ? (
            <p className="row mt-3 px-3.5 py-3 text-[13.5px] text-label-2">
              Imports are off in the public demo, so every visitor sees the same store. Run shipjack locally to try them.
            </p>
          ) : (
            <div className="mt-3 flex flex-wrap gap-2">
              <label className="glass-clear press inline-flex cursor-pointer items-center gap-1.5 rounded-full px-4 py-2 text-[14px]">
                <Upload className="size-4" aria-hidden /> Choose a CSV file
                <input
                  type="file"
                  accept=".csv,text/csv"
                  className="sr-only"
                  disabled={busy}
                  onChange={async (e) => {
                    const file = e.target.files?.[0];
                    if (file) await importCsv(await file.text(), file.name);
                    e.target.value = "";
                  }}
                />
              </label>
              <button
                type="button"
                disabled={busy}
                onClick={async () => {
                  const sample = await operator.sampleCatalog();
                  await importCsv(sample.csv, sample.name);
                }}
                className="glass-tint press rounded-full px-4 py-2 text-[14px] font-semibold"
              >
                Import the sample Shopify export
              </button>
            </div>
          )}
          <dl className="row mt-4 divide-y divide-white/10 px-3.5 text-[14px]">
            <Pair k="Products" v={kb ? `${products} indexed` : "Loading"} />
            <Pair k="Search" v={kb ? (kb.vector ? `by meaning (${kb.embedder.split(":").pop()})` : "keywords only (no Postgres)") : ""} />
          </dl>
        </div>

        <div className="glass-thick rounded-[var(--r-panel)] p-4">
          <h2 className="px-1 text-[16px] font-semibold">Policy pages</h2>
          <ul className="row mt-3 divide-y divide-white/10 px-3.5 text-[14px]">
            {pages.length === 0 && <li className="py-2.5 text-label-3">No pages yet.</li>}
            {pages.map((d) => (
              <li key={d.doc} className="flex justify-between py-2.5">
                <span>{d.doc}</span>
                <span className="text-label-3">
                  {d.chunks} section{d.chunks === 1 ? "" : "s"}
                </span>
              </li>
            ))}
          </ul>
          {!locked && (
            <form
              className="mt-3 space-y-2"
              onSubmit={(e) => {
                e.preventDefault();
                addPage();
              }}
            >
              <input
                value={slug}
                onChange={(e) => setSlug(e.target.value)}
                placeholder="Page name, e.g. student-discount"
                className="row w-full px-3.5 py-2.5 text-[14px] outline-none placeholder:text-label-3"
                aria-label="Page name"
              />
              <textarea
                value={page}
                onChange={(e) => setPage(e.target.value)}
                rows={4}
                placeholder={"## Student discount\nStudents get 10% off with a .edu email."}
                className="row w-full resize-y px-3.5 py-2.5 text-[14px] outline-none placeholder:text-label-3"
                aria-label="Page text (Markdown, one ## heading per topic)"
              />
              <button
                type="submit"
                disabled={busy || !slug.trim() || !page.trim()}
                className="glass-clear press rounded-full px-4 py-2 text-[14px]"
              >
                Add page
              </button>
            </form>
          )}
        </div>
      </div>
      <SearchTester />
    </section>
  );
}

function SearchTester() {
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const [mode, setMode] = useState("vector");
  const [hits, setHits] = useState<KbHit[] | null>(null);
  const run = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (q.trim()) setHits((await operator.search(q.trim(), kind, mode)).data);
  };
  const choice = (value: string, current: string, set: (v: string) => void, label: string) => (
    <button
      key={label}
      type="button"
      onClick={() => set(value)}
      aria-pressed={current === value}
      className={"rounded-full px-3 py-1 text-[13px] " + (current === value ? "bg-white/18 text-label" : "text-label-2 hover:text-label")}
    >
      {label}
    </button>
  );
  return (
    <div className="glass-thick mt-3 rounded-[var(--r-panel)] p-4">
      <h2 className="px-1 text-[16px] font-semibold">Try a search</h2>
      <p className="mt-1 px-1 text-[13.5px] text-label-2">What the support and shopping agents get back for a question.</p>
      <form onSubmit={run} className="mt-3 flex flex-wrap items-center gap-2">
        <div className="row flex min-w-[240px] flex-1 items-center gap-2 px-3.5">
          <Search className="size-4 text-label-3" aria-hidden />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="e.g. the parcel left, can it go to my office instead"
            className="w-full bg-transparent py-2.5 text-[14px] outline-none placeholder:text-label-3"
            aria-label="Search question"
          />
        </div>
        <div className="row flex gap-1 p-1" role="group" aria-label="What to search">
          {choice("", kind, setKind, "All")}
          {choice("product", kind, setKind, "Products")}
          {choice("policy", kind, setKind, "Policies")}
        </div>
        <div className="row flex gap-1 p-1" role="group" aria-label="How to search">
          {choice("vector", mode, setMode, "Meaning")}
          {choice("hybrid", mode, setMode, "Hybrid")}
          {choice("keyword", mode, setMode, "Keywords")}
        </div>
        <button type="submit" className="glass-tint press rounded-full px-4 py-2 text-[14px] font-semibold">
          Search
        </button>
      </form>
      {hits && (
        <ol className="mt-3 space-y-2">
          {hits.length === 0 && <li className="px-1 text-[14px] text-label-3">Nothing matched.</li>}
          {hits.map((h, i) => (
            <li key={h.source + i} className="row px-3.5 py-2.5">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-[14px] font-medium">
                  {i + 1}. {h.title}
                </span>
                <span className="tabular text-[12px] text-label-3">
                  {h.vector_rank ? `meaning #${h.vector_rank}` : ""}
                  {h.vector_rank && h.keyword_rank ? ", " : ""}
                  {h.keyword_rank ? `keywords #${h.keyword_rank}` : ""}
                </span>
              </div>
              <p className="mt-0.5 line-clamp-2 text-[13px] text-label-2">{h.text}</p>
              <p className="mt-0.5 text-[12px] text-label-3">{h.source}</p>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ---- conversations: replay what the agents did -----------------------------------------------------------

function Runs() {
  const [threads, setThreads] = useState<OpsThread[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [events, setEvents] = useState<StoredEvent[]>([]);

  useEffect(() => {
    operator.threads().then((t) => {
      setThreads(t);
      if (t[0]) setSelected((s) => s ?? t[0].id);
    });
  }, []);

  useEffect(() => {
    if (!selected) return;
    let live = true;
    operator.events(selected).then((e) => live && setEvents(e));
    return () => {
      live = false;
    };
  }, [selected]);

  return (
    <section>
      <Title sub="Every step the agents took: which model answered, the tools they called, the checks, the approvals and the webhooks.">
        Conversations
      </Title>
      <div className="grid gap-3 lg:grid-cols-[300px_1fr]">
        <ul className="glass thin-scroll max-h-[70vh] overflow-y-auto rounded-[var(--r-panel)] p-2">
          {threads.map((t) => (
            <li key={t.id}>
              <button
                type="button"
                onClick={() => setSelected(t.id)}
                aria-current={selected === t.id}
                className={
                  "block w-full rounded-[var(--r-row)] px-3 py-2 text-left transition-colors " +
                  (selected === t.id ? "bg-white/14" : "hover:bg-white/7")
                }
              >
                <span className="block truncate text-[14px]">{t.title}</span>
                <span className="text-[12px] text-label-3">
                  {t.user_name}, {t.model_calls} model calls
                </span>
              </button>
            </li>
          ))}
        </ul>
        <div className="glass-thick thin-scroll max-h-[70vh] overflow-y-auto rounded-[var(--r-panel)] p-4">
          {events.length === 0 ? (
            <p className="text-[14px] text-label-3">Pick a conversation to replay it.</p>
          ) : (
            <Timeline events={events} />
          )}
        </div>
      </div>
    </section>
  );
}

function describe(e: StoredEvent): { icon: typeof Bot; tone: string; title: string; detail?: string } | null {
  const d = e.data as Record<string, unknown>;
  switch (e.event) {
    case "route":
      return { icon: Bot, tone: "text-indigo", title: `Routed to ${String(d.route)}` };
    case "model_call":
      return {
        icon: Cpu,
        tone: "text-label-2",
        title: `${String(e.node)} called ${String(d.model || "a model")}`,
        detail: [
          `${d.tokens_in ?? 0} in / ${d.tokens_out ?? 0} out tokens`,
          typeof d.latency_ms === "number" ? `${(d.latency_ms / 1000).toFixed(1)} s` : null,
          (d.tool_calls as string[] | undefined)?.length ? `tools: ${(d.tool_calls as string[]).join(", ")}` : null,
        ]
          .filter(Boolean)
          .join("  |  "),
      };
    case "model_fallback":
      return { icon: TriangleAlert, tone: "text-orange", title: `${String(d.model)} failed, fell back`, detail: String(d.error ?? "") };
    case "model_error":
      return { icon: TriangleAlert, tone: "text-red", title: "Every model failed", detail: String(d.error ?? "") };
    case "checkout_ready":
      return { icon: ShieldCheck, tone: "text-green", title: `Checkout verified at ${money(Number(d.total))}`, detail: "cart, price, address and merchant match" };
    case "checkout_refused":
    case "proposal_refused":
      return { icon: ShieldCheck, tone: "text-red", title: "Refused before payment", detail: String(d.reason ?? (d.problems as string[] | undefined)?.join("; ") ?? "") };
    case "approval_requested":
      return { icon: UserIcon, tone: "text-orange", title: `Waiting for the ${String(d.actor)}`, detail: String(d.kind) };
    case "approval":
      return { icon: UserIcon, tone: d.approved ? "text-green" : "text-red", title: `${String(d.kind)} ${d.approved ? "approved" : "declined"}` };
    case "order_placed":
      return { icon: Package, tone: "text-green", title: `Order ${String(d.order_id)} paid`, detail: money(Number(d.amount)) };
    case "refund_requested":
      return { icon: RotateCcw, tone: "text-orange", title: `Refund of ${money(Number(d.amount))} requested`, detail: String(d.reason ?? "") };
    case "refunded":
      return { icon: RotateCcw, tone: "text-green", title: `Refunded ${money(Number(d.amount))}` };
    case "order_update":
      return { icon: Webhook, tone: "text-teal", title: `Webhook: ${String(d.order_id)} is ${String(d.status)}` };
    case "run_error":
      return { icon: TriangleAlert, tone: "text-red", title: "The run failed", detail: String(d.error ?? "") };
    case "budget":
      return { icon: Activity, tone: "text-orange", title: "Step limit reached" };
    default:
      return null;
  }
}

function Timeline({ events }: { events: StoredEvent[] }) {
  const rows = events.map((e) => ({ e, d: describe(e) })).filter((r) => r.d);
  return (
    <ol className="relative space-y-1">
      {rows.map(({ e, d }) => {
        const Icon = d!.icon;
        return (
          <li key={e.seq} className="flex gap-3 rounded-[var(--r-row)] px-2 py-1.5 hover:bg-white/5">
            <span className={"mt-0.5 grid size-7 shrink-0 place-items-center rounded-full bg-white/8 " + d!.tone}>
              <Icon className="size-[15px]" aria-hidden />
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[14px]">{d!.title}</p>
              {d!.detail && <p className="truncate text-[12.5px] text-label-3">{d!.detail}</p>}
            </div>
            <time className="tabular shrink-0 pt-0.5 text-[12px] text-label-3">{new Date(e.ts).toLocaleTimeString()}</time>
          </li>
        );
      })}
    </ol>
  );
}

// ---- health ----------------------------------------------------------------------------------------------

function Health() {
  const [m, setM] = useState<Metrics | null>(null);
  useEffect(() => {
    const load = () => operator.metrics().then(setM);
    const first = window.setTimeout(load, 0);
    const timer = window.setInterval(load, 5000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, []);
  if (!m) return <Title sub="Loading">Health</Title>;
  const sec = (ms: number | null) => (ms == null ? "n/a" : `${(ms / 1000).toFixed(1)} s`);
  const tiles: [string, string, string][] = [
    ["Model calls", String(m.model_calls ?? 0), `${Math.round((m.tokens_in ?? 0) / 1000)}k tokens in, ${Math.round((m.tokens_out ?? 0) / 1000)}k out`],
    ["Model latency", `${sec(m.p50_ms)} median`, `${sec(m.p95_ms)} at the 95th percentile`],
    ["Orders", String(m.orders), `${m.purchases_approved} approved, ${m.purchases_declined} declined by shoppers`],
    ["Stopped before paying", String(m.refused), "checkouts that didn't match the proposal"],
    ["Waiting on a person", String(m.pending_approvals), "approvals not decided yet"],
    ["Errors", String(m.errors), "model, payment and refund failures"],
  ];
  return (
    <section>
      <Title sub="How the assistant is doing, from its own event log.">Health</Title>
      <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {tiles.map(([label, value, note]) => (
          <li key={label} className="glass rounded-[var(--r-card)] p-4">
            <p className="text-[13.5px] text-label-2">{label}</p>
            <p className="tabular mt-1 text-[26px] font-semibold tracking-tight">{value}</p>
            <p className="mt-0.5 text-[12.5px] text-label-3">{note}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
