"use client";

import { CreditCard, Mail, MapPin, ShieldCheck } from "lucide-react";
import { useEffect, useRef } from "react";
import type { PurchaseCard } from "@/lib/api";
import { money } from "@/lib/api";

// The purchase approval, shaped like an Apple Pay sheet: who you pay, what you get, where it goes,
// what it costs. The assistant can't pay without it, and the address on it comes from the
// shopper's profile, never from the agent that read the product pages.
export function ApprovalSheet({
  card,
  busy,
  onDecide,
}: {
  card: PurchaseCard;
  busy: boolean;
  onDecide: (approved: boolean) => void;
}) {
  const sheet = useRef<HTMLDivElement>(null);
  useEffect(() => sheet.current?.focus(), [card.id]);
  const merchant = card.merchant?.name ?? "Northwind Electronics";
  const where = card.ship_to;

  return (
    <div
      ref={sheet}
      tabIndex={-1}
      role="dialog"
      aria-modal="false"
      aria-labelledby="sheet-title"
      className="glass-thick sheet-rise w-full max-w-[460px] rounded-[var(--r-sheet)] p-[14px] focus:outline-none"
    >
      <header className="flex items-center gap-3 px-2 pt-1 pb-3">
        <span
          aria-hidden
          className="grid size-10 place-items-center rounded-[12px] bg-gradient-to-b from-[#3d7bff] to-[#2450d6] text-[17px] font-semibold text-white shadow-[inset_0_1px_0_rgb(255_255_255/0.35)]"
        >
          N
        </span>
        <div className="min-w-0 flex-1">
          <h2 id="sheet-title" className="truncate text-[17px] font-semibold">
            {merchant}
          </h2>
          <p className="text-[13px] text-label-2">The assistant wants to buy this for you</p>
        </div>
      </header>

      <ul className="row mb-2 divide-y divide-white/10 px-4">
        {card.items.map((it, i) => (
          <li key={i} className="flex items-baseline justify-between gap-4 py-3">
            <span className="min-w-0">
              <span className="block truncate">{it.title}</span>
              <span className="text-[13px] text-label-2">Quantity {it.quantity}</span>
            </span>
            <span className="tabular shrink-0">{money(it.amount, card.currency)}</span>
          </li>
        ))}
      </ul>

      <dl className="row mb-2 divide-y divide-white/10 px-4 text-[14px]">
        {where ? (
          <div className="flex gap-3 py-3">
            <dt className="flex w-[84px] shrink-0 items-start gap-2 text-label-2">
              <MapPin className="mt-0.5 size-4" aria-hidden /> Ship to
            </dt>
            <dd className="min-w-0">
              <span className="block">{where.name}</span>
              <span className="block text-label-2">
                {where.line_one}
                {where.line_two ? `, ${where.line_two}` : ""}
              </span>
              <span className="block text-label-2">
                {where.city}, {where.state} {where.postal_code}
              </span>
            </dd>
          </div>
        ) : (
          <div className="flex gap-3 py-3">
            <dt className="flex w-[84px] shrink-0 items-start gap-2 text-label-2">
              <Mail className="mt-0.5 size-4" aria-hidden /> Send to
            </dt>
            <dd className="min-w-0 truncate">{card.email}</dd>
          </div>
        )}
        <div className="flex gap-3 py-3">
          <dt className="flex w-[84px] shrink-0 items-start gap-2 text-label-2">
            <CreditCard className="mt-0.5 size-4" aria-hidden /> Pay with
          </dt>
          <dd>Visa ending 4242 (test card)</dd>
        </div>
      </dl>

      <div className="flex items-baseline justify-between px-3 pt-3 pb-4">
        <span className="text-[15px] text-label-2">Total</span>
        <span className="tabular text-[28px] font-semibold tracking-tight">{money(card.total, card.currency)}</span>
      </div>

      <button
        type="button"
        disabled={busy}
        onClick={() => onDecide(true)}
        className="glass-tint press block w-full rounded-full py-3.5 text-[17px] font-semibold"
      >
        Pay {money(card.total, card.currency)}
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => onDecide(false)}
        className="press mt-1 block w-full rounded-full py-3 text-[15px] text-blue"
      >
        Decline
      </button>

      <p className="flex items-start gap-2 px-3 pt-2 pb-1 text-[12.5px] leading-snug text-label-3">
        <ShieldCheck className="mt-px size-4 shrink-0 text-green" aria-hidden />
        Nothing is charged until you pay here. The address comes from your profile; the assistant can&apos;t change
        it.
      </p>
    </div>
  );
}
