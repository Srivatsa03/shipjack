// The assistant API (shipjack/assistant/api.py). Demo tokens stand in for sign-in.
export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8200";
export const SHOPPER_TOKEN = process.env.NEXT_PUBLIC_SHOPPER_TOKEN ?? "demo-shopper";
export const OPERATOR_TOKEN = process.env.NEXT_PUBLIC_OPERATOR_TOKEN ?? "demo-operator";

export type Address = {
  name: string;
  line_one: string;
  line_two?: string;
  city: string;
  state: string;
  postal_code: string;
  country?: string;
};

export type User = { id: string; name: string; email: string; address: Address | null; role: string };
export type Thread = { id: string; title: string; created: string; updated: string };

export type ToolCall = { name: string; args: Record<string, unknown> };
export type Message = {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  tool_calls?: ToolCall[];
  name?: string | null;
};

export type PurchaseCard = {
  id: string;
  kind: "purchase";
  actor: "shopper";
  session_id: string;
  merchant: { id?: string; name?: string; website?: string };
  items: { title: string; quantity: number; amount: number }[];
  total: number;
  currency: string;
  ship_to: Address | null;
  email: string | null;
};

export type RefundAsk = {
  id: string;
  kind: "refund";
  actor: "operator";
  order_id: string;
  amount: number;
  reason: string;
  user: string;
  order_status: string;
  paid: number;
  already_refunded: number;
};

export type Pending = PurchaseCard | RefundAsk;

export type OrderRow = {
  id: string;
  thread_id: string;
  user_id: string;
  status: string;
  data: AcpOrder;
  created: string;
  updated: string;
};

export type AcpOrder = {
  id: string;
  status: string;
  line_items?: { id: string; title: string; quantity: { ordered: number; current: number; fulfilled?: number } }[];
  fulfillments?: { id: string; type: string; status: string; carrier?: string; tracking_number?: string }[];
  totals?: { type: string; amount: number }[];
  adjustments?: { id: string; type: string; amount: number; status: string }[];
};

export type ThreadView = Thread & { messages: Message[]; pending: Pending[]; orders: OrderRow[] };

export type TraceEvent = { event: string; ts?: number; node?: string; [key: string]: unknown };
export type StoredEvent = { seq: number; run_id: string; ts: string; event: string; node: string | null; data: TraceEvent };

export type StreamEvent =
  | { type: "start"; data: { run_id: string } }
  | { type: "token"; data: { node: string; text: string } }
  | { type: "trace"; data: TraceEvent }
  | { type: "node"; data: { node: string; messages?: Message[]; checkout?: unknown; route?: string } }
  | { type: "approval"; data: Pending }
  | { type: "error"; data: { message: string } }
  | { type: "done"; data: { run_id: string } };

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail ?? body.message ?? `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export const shopper = {
  me: () => call<User>("/api/me", SHOPPER_TOKEN),
  saveAddress: (a: Address) => call<User>("/api/me/address", SHOPPER_TOKEN, { method: "PUT", body: JSON.stringify(a) }),
  threads: () => call<Thread[]>("/api/threads", SHOPPER_TOKEN),
  newThread: () => call<Thread>("/api/threads", SHOPPER_TOKEN, { method: "POST" }),
  thread: (id: string) => call<ThreadView>(`/api/threads/${id}`, SHOPPER_TOKEN),
  orders: () => call<OrderRow[]>("/api/orders", SHOPPER_TOKEN),
  send: (id: string, text: string, onEvent: (e: StreamEvent) => void, signal?: AbortSignal) =>
    stream(`/api/threads/${id}/messages`, SHOPPER_TOKEN, { text }, onEvent, signal),
  decide: (id: string, approvalId: string, approved: boolean, onEvent: (e: StreamEvent) => void) =>
    stream(`/api/threads/${id}/decision`, SHOPPER_TOKEN, { approval_id: approvalId, approved }, onEvent),
};

export type Metrics = {
  threads: number;
  orders: number;
  pending_approvals: number;
  purchases_approved: number;
  purchases_declined: number;
  refused: number;
  errors: number;
  model_calls: number;
  tokens_in: number;
  tokens_out: number;
  p50_ms: number | null;
  p95_ms: number | null;
};

export type Approval = {
  id: string;
  thread_id: string;
  kind: string;
  actor: string;
  payload: RefundAsk;
  status: string;
  decided_by: string | null;
  note: string | null;
  created: string;
  decided: string | null;
};

export type OpsThread = { id: string; title: string; updated: string; user_name: string; model_calls: number };

export const operator = {
  approvals: (status = "pending") => call<Approval[]>(`/api/ops/approvals?status=${status}`, OPERATOR_TOKEN),
  decide: (id: string, approved: boolean, note: string) =>
    call<{ id: string; status: string }>(`/api/ops/approvals/${id}`, OPERATOR_TOKEN, {
      method: "POST",
      body: JSON.stringify({ approved, note }),
    }),
  orders: () => call<OrderRow[]>("/api/ops/orders", OPERATOR_TOKEN),
  act: (orderId: string, action: "ship" | "deliver", body: Record<string, string> = {}) =>
    call<AcpOrder>(`/api/ops/orders/${orderId}/${action}`, OPERATOR_TOKEN, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  threads: () => call<OpsThread[]>("/api/ops/threads", OPERATOR_TOKEN),
  thread: (id: string) => call<ThreadView>(`/api/threads/${id}`, OPERATOR_TOKEN),
  events: (id: string) => call<StoredEvent[]>(`/api/threads/${id}/events`, OPERATOR_TOKEN),
  metrics: () => call<Metrics>("/api/ops/metrics", OPERATOR_TOKEN),
  knowledge: () => call<Knowledge>("/api/ops/knowledge", OPERATOR_TOKEN),
  search: (q: string, kind: string, mode: string) =>
    call<{ data: KbHit[]; vector: boolean }>(
      `/api/ops/knowledge/search?${new URLSearchParams({ q, ...(kind ? { kind } : {}), ...(mode ? { mode } : {}) })}`,
      OPERATOR_TOKEN,
    ),
  sampleCatalog: () => call<{ name: string; csv: string }>("/api/ops/sample-catalog", OPERATOR_TOKEN),
  importCatalog: (csv: string) =>
    call<CatalogResult>("/api/ops/catalog", OPERATOR_TOKEN, { method: "POST", body: JSON.stringify({ csv }) }),
  addDocument: (slug: string, markdown: string) =>
    call<{ slug: string; chunks: number; seconds: number }>("/api/ops/documents", OPERATOR_TOKEN, {
      method: "POST",
      body: JSON.stringify({ slug, markdown }),
    }),
};

export type Knowledge = {
  data: { kind: string; doc: string; chunks: number }[];
  vector: boolean;
  embedder: string;
  imports_allowed: boolean;
};
export type KbHit = {
  title: string;
  text: string;
  source: string;
  kind: string;
  doc: string;
  score: number;
  vector_rank: number | null;
  keyword_rank: number | null;
};
export type CatalogResult = { format: string; imported: number; indexed: number; skipped: string[]; seconds: number };

// A POST that answers with server-sent events. EventSource can't send a body or headers, so this
// reads the response stream and splits it into events itself.
async function stream(
  path: string,
  token: string,
  body: unknown,
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const detail = await res.json().catch(() => ({}));
    throw new ApiError(res.status, detail.detail ?? `Request failed (${res.status})`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let type = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7);
        else if (line.startsWith("data: ")) data.push(line.slice(6));
      }
      if (data.length) onEvent({ type, data: JSON.parse(data.join("\n")) } as StreamEvent);
    }
  }
}

export const money = (cents: number, currency = "usd") =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: currency.toUpperCase() }).format(cents / 100);
