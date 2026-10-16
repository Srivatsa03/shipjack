import { Check, Loader, TriangleAlert, X } from "lucide-react";
import type { TraceEvent } from "@/lib/api";
import { money } from "@/lib/api";

export type Step = { key: string; label: string; detail?: string; tone: "work" | "good" | "warn" | "bad" };

const TEAM: Record<string, string> = { shop: "Shopping agent", support: "Support agent", chat: "Assistant" };

function toolLabel(names: string[]): string {
  const count = (n: string) => names.filter((x) => x === n).length;
  if (count("propose_purchase")) return "Proposing a purchase";
  if (count("request_refund")) return "Requesting a refund";
  if (count("search_products")) return "Searching the catalog";
  if (count("get_product")) return count("get_product") > 1 ? `Reading ${count("get_product")} product pages` : "Reading a product page";
  if (count("list_my_orders") || count("get_order")) return "Checking your orders";
  if (count("search_policies")) return "Reading the store policy";
  return "Writing a reply";
}

const seconds = (ms: unknown) => (typeof ms === "number" ? `${(ms / 1000).toFixed(1)} s` : undefined);

// What each traced event means to the shopper, in plain words. Unknown events are skipped.
export function stepFor(e: TraceEvent, n: number): Step | null {
  const key = `${n}-${e.event}`;
  switch (e.event) {
    case "route":
      return { key, label: TEAM[String(e.route)] ?? "Assistant", tone: "work" };
    case "model_call":
      if (e.node === "router") return null;
      return {
        key,
        label: toolLabel((e.tool_calls as string[]) ?? []),
        detail: [String(e.model ?? ""), seconds(e.latency_ms)].filter(Boolean).join(", "),
        tone: "work",
      };
    case "model_fallback":
      return { key, label: `${String(e.model)} unavailable, switching models`, tone: "warn" };
    case "model_error":
      return { key, label: "No model answered", tone: "bad" };
    case "tool_call_dropped":
      return { key, label: "Ignored a tool this agent may not use", tone: "warn" };
    case "proposal_refused":
      return { key, label: "Proposal sent back", detail: String(e.reason ?? ""), tone: "warn" };
    case "checkout_ready":
      return { key, label: "Checkout matches: item, price and address", detail: money(Number(e.total)), tone: "good" };
    case "checkout_refused":
      return { key, label: "Checkout refused", detail: ((e.problems as string[]) ?? []).join("; "), tone: "bad" };
    case "approval":
      return { key, label: e.approved ? "Approved" : "Declined", tone: e.approved ? "good" : "warn" };
    case "order_placed":
      return { key, label: "Paid", detail: String(e.order_id ?? ""), tone: "good" };
    case "overcharge":
      return { key, label: "Charged more than approved", tone: "bad" };
    case "payment_declined":
    case "payment_error":
      return { key, label: "Payment didn't go through", tone: "bad" };
    case "refund_requested":
      return { key, label: "Sent to the store for review", detail: money(Number(e.amount)), tone: "work" };
    case "refunded":
      return { key, label: "Refunded", detail: money(Number(e.amount)), tone: "good" };
    case "budget":
      return { key, label: "Step limit reached", tone: "warn" };
    default:
      return null;
  }
}

export function AgentTrail({ steps, running }: { steps: Step[]; running: boolean }) {
  if (!steps.length) return null;
  return (
    <ol aria-label="What the assistant did" className="flex flex-wrap items-center gap-1.5 text-[12.5px]">
      {steps.map((s, i) => {
        const live = running && i === steps.length - 1 && s.tone === "work";
        return (
          <li
            key={s.key}
            className="glass-clear inline-flex items-center gap-1.5 rounded-full py-1 pr-3 pl-2 text-label-2"
            title={s.detail}
          >
            <span
              aria-hidden
              className={
                "grid size-4 place-items-center rounded-full " +
                (s.tone === "good"
                  ? "bg-green/90 text-black"
                  : s.tone === "bad"
                    ? "bg-red text-white"
                    : s.tone === "warn"
                      ? "bg-orange text-black"
                      : "bg-white/15 text-label")
              }
            >
              {live ? (
                <Loader className="size-3 animate-spin" />
              ) : s.tone === "good" ? (
                <Check className="size-3" strokeWidth={3} />
              ) : s.tone === "bad" ? (
                <X className="size-3" strokeWidth={3} />
              ) : s.tone === "warn" ? (
                <TriangleAlert className="size-2.5" strokeWidth={3} />
              ) : (
                <span className="size-1.5 rounded-full bg-label-2" />
              )}
            </span>
            <span className="text-label">{s.label}</span>
            {s.detail && <span className="tabular hidden text-label-3 sm:inline">{s.detail}</span>}
          </li>
        );
      })}
    </ol>
  );
}
