"use client";

import { ArrowUp, Clock, LayoutGrid, PanelLeft, Plus, Truck, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { LiquidLens } from "@/components/liquid-lens";
import { Markdown } from "@/components/markdown";
import { useWallpaper, WALLPAPERS } from "@/components/wallpaper";
import {
  API,
  ApiError,
  forgetSession,
  money,
  type Message,
  type OrderRow,
  type PurchaseCard,
  type RefundAsk,
  session,
  shopper,
  type StreamEvent,
  type Thread,
  type ThreadView,
  type User,
  unreachable,
} from "@/lib/api";
import { AgentTrail, type Step, stepFor } from "./agent-trail";
import { ApprovalSheet } from "./approval-sheet";
import { mentioned, parseProducts, type Product, Products } from "./products";

const SUGGESTIONS = [
  "Find me a USB-C hub with HDMI under $40",
  "Buy me a $25 Northwind gift card",
  "Where's my order?",
  "What's your return policy?",
];

type Live = { text: string; steps: Step[]; products: Product[] };

export function ShopperApp() {
  const [me, setMe] = useState<User | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [view, setView] = useState<ThreadView | null>(null);
  const [live, setLive] = useState<Live | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [orders, setOrders] = useState<OrderRow[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [demo, setDemo] = useState(false);
  const [waking, setWaking] = useState(false);
  const [draft, setDraft] = useState("");
  const [sidebar, setSidebar] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const pick = (p: Product) => {
    setDraft(`I'd like the ${p.title}.`);
    composer.current?.focus();
  };
  const counter = useRef(0);

  const refreshLists = useCallback(async () => {
    const [t, o] = await Promise.all([shopper.threads(), shopper.orders()]);
    setThreads(t);
    setOrders(o);
  }, []);

  useEffect(() => {
    const slow = window.setTimeout(() => setWaking(true), 2500); // free hosting sleeps when idle
    (async () => {
      try {
        setDemo((await session()).demo);
        setMe(await shopper.me());
        await refreshLists();
        const health = await fetch(`${API}/api/health`).then((r) => r.json());
        setModels(health.models ?? []);
      } catch (err) {
        setError(
          err instanceof ApiError ? err.message : unreachable(),
        );
      } finally {
        window.clearTimeout(slow);
        setWaking(false);
      }
    })();
    return () => window.clearTimeout(slow);
  }, [refreshLists]);

  const remember = (id: string | null) => {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("t", id);
    else url.searchParams.delete("t");
    window.history.replaceState(null, "", url);
  };

  const open = useCallback(async (id: string) => {
    setCurrent(id);
    setLive(null);
    setError(null);
    setSidebar(false);
    remember(id);
    try {
      setView(await shopper.thread(id));
    } catch {
      setCurrent(null);
      remember(null);
    }
  }, []);

  // a link to a conversation (?t=...) reopens it, and so does a reload
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("t");
    if (id) void Promise.resolve().then(() => open(id));
  }, [open]);

  // keep order statuses and a pending refund fresh (the store's webhooks change them)
  const waitingOnStore = view?.pending.some((p) => p.kind === "refund") ?? false;
  useEffect(() => {
    const every = waitingOnStore ? 3000 : 8000;
    const timer = window.setInterval(async () => {
      if (document.hidden || running) return;
      setOrders(await shopper.orders().catch(() => orders));
      if (current && waitingOnStore) setView(await shopper.thread(current));
    }, every);
    return () => window.clearInterval(timer);
  }, [current, waitingOnStore, running, orders]);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [view?.messages.length, live?.text, live?.steps.length, live?.products.length]);

  const onEvent = useCallback((e: StreamEvent) => {
    if (e.type === "start") setLive((l) => l ?? { text: "", steps: [], products: [] });
    else if (e.type === "token") setLive((l) => (l ? { ...l, text: l.text + e.data.text } : l));
    else if (e.type === "trace") {
      const step = stepFor(e.data, counter.current++);
      if (step) setLive((l) => (l ? { ...l, steps: [...l.steps, step], text: "" } : l));
    } else if (e.type === "node" && e.data.node === "shop_tools") {
      const found = (e.data.messages ?? []).filter((m) => m.name === "search_products").flatMap((m) => parseProducts(m.content));
      if (found.length) setLive((l) => (l ? { ...l, products: found } : l));
    } else if (e.type === "error") setError(e.data.message);
  }, []);

  const run = useCallback(
    async (go: (threadId: string) => Promise<void>, threadId: string) => {
      setRunning(true);
      setError(null);
      setLive({ text: "", steps: [], products: [] });
      try {
        await go(threadId);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "The connection to the assistant dropped. Try again.");
      } finally {
        setRunning(false);
        setView(await shopper.thread(threadId));
        setLive((l) => (l ? { ...l, text: "", products: [] } : l));
        refreshLists();
      }
    },
    [refreshLists],
  );

  const send = async (text: string) => {
    const words = text.trim();
    if (!words || running) return;
    setDraft("");
    let id = current;
    if (!id) {
      id = (await shopper.newThread()).id;
      setCurrent(id);
      remember(id);
    }
    const shown: Message = { id: `local-${Date.now()}`, role: "user", content: words };
    setView((v) => (v && v.id === id ? { ...v, messages: [...v.messages, shown] } : { ...(v ?? {}), id: id!, title: words, created: "", updated: "", messages: [shown], pending: [], orders: [] }));
    await run((t) => shopper.send(t, words, onEvent), id);
  };

  const decide = async (card: PurchaseCard, approved: boolean) => {
    if (!current) return;
    setView((v) => (v ? { ...v, pending: v.pending.filter((p) => p.id !== card.id) } : v));
    await run((t) => shopper.decide(t, card.id, approved, onEvent), current);
  };

  const newChat = () => {
    remember(null);
    setCurrent(null);
    setView(null);
    setLive(null);
    setError(null);
    setSidebar(false);
  };

  const purchase = view?.pending.find((p): p is PurchaseCard => p.kind === "purchase");
  const refund = view?.pending.find((p): p is RefundAsk => p.kind === "refund");
  const messages = view?.messages ?? [];
  const lastUser = messages.map((m) => m.role).lastIndexOf("user");
  // search results: the products the turn's answer talks about, each shown once per turn
  const productsAt = new Map<number, Product[]>();
  let seen = new Set<string>();
  messages.forEach((m, i) => {
    if (m.role === "user") seen = new Set();
    if (m.role !== "tool" || m.name !== "search_products") return;
    const fresh = mentioned(parseProducts(m.content), finalReply(messages, i)).filter((p) => !seen.has(p.id));
    fresh.forEach((p) => seen.add(p.id));
    if (fresh.length) productsAt.set(i, fresh);
  });
  // each order's card sits under the latest assistant message that mentions it, once
  const cardAt = new Map<number, OrderRow[]>();
  for (const o of view?.orders ?? []) {
    const at = messages.findLastIndex((m) => m.role === "assistant" && m.content.includes(o.id));
    if (at >= 0) cardAt.set(at, [...(cardAt.get(at) ?? []), o]);
  }

  return (
    <div className="fixed inset-0 flex">
      {/* ---- sidebar ---- */}
      <aside
        className={
          "glass z-40 m-3 w-[288px] shrink-0 flex-col rounded-[var(--r-panel)] p-2.5 md:flex " +
          (sidebar ? "fixed inset-y-0 left-0 flex" : "hidden")
        }
        aria-label="Conversations and orders"
      >
        <div className="flex items-center gap-2 p-1.5">
          <button
            type="button"
            onClick={newChat}
            className="glass-clear press flex flex-1 items-center gap-2 rounded-full px-3.5 py-2 text-[14px] font-medium"
          >
            <Plus className="size-4" aria-hidden /> New chat
          </button>
          <button
            type="button"
            onClick={() => setSidebar(false)}
            className="glass-clear press grid size-9 place-items-center rounded-full md:hidden"
            aria-label="Close sidebar"
          >
            <X className="size-4" />
          </button>
        </div>

        <nav className="thin-scroll mt-2 min-h-0 flex-1 overflow-y-auto px-1">
          {threads.length === 0 && <p className="px-3 py-2 text-[13px] text-label-3">Your chats will appear here.</p>}
          <ul className="space-y-0.5">
            {threads.map((t) => (
              <li key={t.id}>
                <button
                  type="button"
                  onClick={() => open(t.id)}
                  aria-current={t.id === current}
                  className={
                    "block w-full truncate rounded-[var(--r-row)] px-3 py-2 text-left text-[14px] transition-colors " +
                    (t.id === current ? "bg-white/14 text-label" : "text-label-2 hover:bg-white/7 hover:text-label")
                  }
                >
                  {t.title}
                </button>
              </li>
            ))}
          </ul>

          {orders.length > 0 && (
            <>
              <h3 className="mt-5 mb-1 px-3 text-[13px] font-semibold text-label-2">Orders</h3>
              <ul className="space-y-0.5">
                {orders.slice(0, 8).map((o) => (
                  <li key={o.id}>
                    <button
                      type="button"
                      onClick={() => open(o.thread_id)}
                      className="flex w-full items-center gap-2.5 rounded-[var(--r-row)] px-3 py-2 text-left hover:bg-white/7"
                    >
                      <StatusDot status={o.status} />
                      <span className="min-w-0 flex-1 truncate text-[13.5px]">{o.data.line_items?.[0]?.title ?? o.id}</span>
                      <span className="text-[12px] text-label-3 capitalize">{o.status}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </nav>

        <footer className="mt-2 border-t border-sep p-2">
          {me && (
            <div className="mb-2.5 px-1.5">
              <p className="text-[14px] font-medium">{me.name}</p>
              <p className="truncate text-[12.5px] text-label-3">
                {me.address ? `Ships to ${me.address.line_one}, ${me.address.city}` : "No delivery address yet"}
              </p>
            </div>
          )}
          <WallpaperPicker />
          {demo && (
            <button
              type="button"
              onClick={() => {
                forgetSession();
                window.history.replaceState(null, "", "/");
                window.location.reload(); // a clean start: new sandbox, no state carried over
              }}
              className="mt-2 px-1.5 text-[12.5px] text-label-3 underline-offset-2 hover:text-label hover:underline"
            >
              Start over with a new sandbox
            </button>
          )}
        </footer>
      </aside>

      {/* ---- conversation ---- */}
      <main className="relative min-w-0 flex-1">
        <div className="pointer-events-none absolute inset-x-0 top-3 z-30 flex items-center justify-between gap-2 px-3 md:justify-center">
          <button
            type="button"
            onClick={() => setSidebar(true)}
            className="glass-clear press pointer-events-auto grid size-10 place-items-center rounded-full md:hidden"
            aria-label="Open sidebar"
          >
            <PanelLeft className="size-[18px]" />
          </button>
          <div data-lens className="glass pointer-events-auto flex items-center gap-2.5 rounded-full py-1.5 pr-4 pl-1.5">
            <span
              aria-hidden
              className="grid size-7 place-items-center rounded-full bg-gradient-to-b from-[#3d7bff] to-[#2450d6] text-[13px] font-semibold text-white"
            >
              N
            </span>
            <span className="text-[14.5px] font-semibold">Northwind Assistant</span>
            {models[0] && <span className="hidden text-[12.5px] text-label-3 sm:inline">{models[0]}</span>}
          </div>
          <Link
            href="/console"
            className="glass-clear press pointer-events-auto flex items-center gap-1.5 rounded-full px-3.5 py-2 text-[13.5px] md:absolute md:right-4"
          >
            <LayoutGrid className="size-4" aria-hidden /> <span className="hidden sm:inline">Store console</span>
          </Link>
        </div>

        <div ref={scroller} className="thin-scroll h-full overflow-y-auto">
          <div className="mx-auto flex min-h-full max-w-[760px] flex-col gap-3 px-4 pt-24 pb-48">
            {waking && (
              <p role="status" className="glass self-center rounded-full px-4 py-2 text-[13.5px] text-label-2">
                Waking the demo server. Free hosting sleeps when idle, so this can take up to a minute.
              </p>
            )}
            {!view && !running && <Welcome name={me?.name} demo={demo} onPick={send} />}

            {messages.map((m, i) => (
              <MessageItem key={m.id + i} m={m} products={productsAt.get(i) ?? []} orders={cardAt.get(i) ?? []} onPick={pick}>
                {i === lastUser && live && live.steps.length > 0 && (
                  <div className="pt-1">
                    <AgentTrail steps={live.steps} running={running} />
                  </div>
                )}
              </MessageItem>
            ))}

            {running && live && (
              <>
                {live.products.length > 0 && <Products items={live.products} onPick={pick} />}
                {live.text && (
                  <div className="glass max-w-[88%] self-start rounded-[22px] rounded-bl-[8px] px-4 py-2.5">
                    <span className="caret">
                      <Markdown text={live.text} />
                    </span>
                  </div>
                )}
              </>
            )}

            {refund && (
              <div className="glass flex items-center gap-3 self-start rounded-[var(--r-card)] px-4 py-3 text-[14px]">
                <Clock className="size-5 shrink-0 text-orange" aria-hidden />
                <span>
                  Waiting for Northwind to review your refund of <strong>{money(refund.amount)}</strong> on {refund.order_id}.
                </span>
              </div>
            )}

            {error && (
              <p role="alert" className="glass self-start rounded-[var(--r-card)] px-4 py-3 text-[14px] text-[#ffb4ae]">
                {error}
              </p>
            )}
          </div>
        </div>

        <div aria-hidden className="scroll-edge top z-10" />
        <div aria-hidden className="scroll-edge bottom z-10" />
        {purchase && <div aria-hidden className="dim-in absolute inset-0 z-20 bg-[rgb(3_4_12/0.5)]" />}
        <div className="pointer-events-none absolute inset-x-0 bottom-4 z-30 flex flex-col items-center gap-3 px-3">
          {purchase && (
            <div className="pointer-events-auto flex w-full justify-center">
              <ApprovalSheet card={purchase} busy={running} onDecide={(ok) => decide(purchase, ok)} />
            </div>
          )}
          {!purchase && (
            <form
              className="pointer-events-auto w-full max-w-[760px]"
              onSubmit={(e) => {
                e.preventDefault();
                send(draft);
              }}
            >
              <div data-lens className="glass flex items-end gap-2 rounded-[28px] py-2 pr-2 pl-5">
                <label htmlFor="composer" className="sr-only">
                  Message the assistant
                </label>
                <textarea
                  ref={composer}
                  id="composer"
                  rows={1}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      send(draft);
                    }
                  }}
                  placeholder={running ? "Working on it…" : "Ask for a product, an order or a refund"}
                  className="max-h-40 min-h-[40px] flex-1 resize-none bg-transparent py-2 text-[15.5px] outline-none placeholder:text-label-3 field-sizing-content"
                />
                <button
                  type="submit"
                  disabled={running || !draft.trim()}
                  className="glass-tint press grid size-10 shrink-0 place-items-center rounded-full"
                  aria-label="Send"
                >
                  <ArrowUp className="size-5" strokeWidth={2.5} />
                </button>
              </div>
            </form>
          )}
        </div>
      </main>
      {/* after the page, so the lens only touches elements React has already hydrated */}
      <LiquidLens />
    </div>
  );
}

// The answer that closed the turn a message belongs to: the last assistant text before the next user message.
function finalReply(messages: Message[], from: number): string | undefined {
  let reply: string | undefined;
  for (const n of messages.slice(from + 1)) {
    if (n.role === "user") break;
    if (n.role === "assistant" && n.content.trim()) reply = n.content;
  }
  return reply;
}

function Welcome({ name, demo, onPick }: { name?: string; demo: boolean; onPick: (t: string) => void }) {
  return (
    <section className="flex flex-1 flex-col items-center justify-center pb-10 text-center">
      <h1 className="text-[34px] leading-tight font-semibold tracking-tight [text-shadow:0_2px_24px_rgb(0_0_0/0.45)] sm:text-[44px]">
        {name && !demo ? `Hi ${name.split(" ")[0]}. What do you need?` : "What do you need?"}
      </h1>
      <p className="mt-3 max-w-[460px] text-[16px] text-label-2 [text-shadow:0_1px_12px_rgb(0_0_0/0.5)]">
        I can find products at Northwind and buy them for you. You approve every payment, and it ships only to your saved
        address.
      </p>
      {demo && (
        <p className="mt-2 max-w-[460px] text-[13.5px] text-label-3 [text-shadow:0_1px_12px_rgb(0_0_0/0.5)]">
          This is a public demo: a made-up store, test payments, and a private sandbox for this browser. The store console
          shows only your sandbox.
        </p>
      )}
      <ul className="mt-8 flex max-w-[620px] flex-wrap justify-center gap-2">
        {SUGGESTIONS.map((s) => (
          <li key={s}>
            <button type="button" onClick={() => onPick(s)} className="glass-clear press rounded-full px-4 py-2 text-[14px]">
              {s}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function MessageItem({
  m,
  products,
  orders,
  onPick,
  children,
}: {
  m: Message;
  products: Product[];
  orders: OrderRow[];
  onPick: (p: Product) => void;
  children?: React.ReactNode;
}) {
  if (m.role === "user") {
    return (
      <div className="flex flex-col items-end gap-2">
        <div className="glass-tint max-w-[80%] rounded-[22px] rounded-br-[8px] px-4 py-2.5 whitespace-pre-wrap">{m.content}</div>
        {children && <div className="self-start">{children}</div>}
      </div>
    );
  }
  if (m.role === "tool") return products.length ? <Products items={products} onPick={onPick} /> : null;
  if (!m.content.trim()) return null;
  const wide = /^\s*\|.*\|/m.test(m.content); // a table needs the room
  return (
    <div className="flex flex-col items-start gap-2">
      <div className={"glass rounded-[22px] rounded-bl-[8px] px-4 py-2.5 " + (wide ? "w-full" : "max-w-[88%]")}>
        <Markdown text={m.content} />
      </div>
      {orders.map((o) => (
        <OrderCard key={o.id} order={o} />
      ))}
    </div>
  );
}

const STAGES = ["created", "shipped", "completed"] as const;

function OrderCard({ order }: { order: OrderRow }) {
  const status = order.status;
  const at = status === "canceled" ? -1 : Math.max(0, STAGES.indexOf(status as (typeof STAGES)[number]));
  const shipment = order.data.fulfillments?.find((f) => f.type === "shipping");
  const totals = Object.fromEntries((order.data.totals ?? []).map((t) => [t.type, t.amount]));
  return (
    <div className="glass w-full max-w-[420px] rounded-[var(--r-card)] p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-[14px] font-medium">
          <Truck className="size-4 text-label-2" aria-hidden /> {order.data.line_items?.[0]?.title ?? order.id}
        </span>
        <span className="tabular text-[14px]">{money(totals.total ?? 0)}</span>
      </div>
      <ol className="mt-3 grid grid-cols-3 gap-1.5" aria-label="Order progress">
        {["Placed", "Shipped", "Delivered"].map((label, i) => (
          <li key={label} className="flex flex-col gap-1.5">
            <span className={"h-1.5 rounded-full " + (at >= i ? "bg-green" : "bg-white/15")} />
            <span className={"text-[12px] " + (at >= i ? "text-label" : "text-label-3")}>{label}</span>
          </li>
        ))}
      </ol>
      <p className="mt-2 text-[12.5px] text-label-3">
        {status === "canceled"
          ? "Canceled and refunded."
          : shipment?.tracking_number
            ? `${shipment.carrier ?? "Carrier"} tracking ${shipment.tracking_number}`
            : "Northwind will add tracking when it ships."}
        {totals.amount_refunded ? ` Refunded ${money(totals.amount_refunded)}.` : ""}
      </p>
    </div>
  );
}

function StatusDot({ status }: { status: string }) {
  const color = status === "completed" ? "bg-green" : status === "shipped" ? "bg-teal" : status === "canceled" ? "bg-red" : "bg-orange";
  return <span aria-hidden className={"size-2 shrink-0 rounded-full " + color} />;
}

function WallpaperPicker() {
  const { kind, setKind } = useWallpaper();
  return (
    <fieldset className="flex items-center gap-2 px-1.5">
      <legend className="sr-only">Wallpaper</legend>
      <span className="mr-auto text-[12.5px] text-label-3">Wallpaper</span>
      {WALLPAPERS.map((w) => (
        <button
          key={w.kind}
          type="button"
          onClick={() => setKind(w.kind)}
          aria-pressed={kind === w.kind}
          title={w.name}
          className={
            "size-6 rounded-full border transition-transform hover:scale-110 " +
            (kind === w.kind ? "border-white ring-2 ring-white/40" : "border-white/30") +
            (w.kind === "tahoe"
              ? " bg-gradient-to-b from-[#1f1c5c] via-[#c26a86] to-[#2a2154]"
              : w.kind === "milkyway"
                ? " bg-[url(/wallpaper/milkyway-wide.webp)] bg-cover bg-center"
                : " bg-gradient-to-br from-[#3a3f52] to-[#0e1016]")
          }
        >
          <span className="sr-only">{w.name}</span>
        </button>
      ))}
    </fieldset>
  );
}
