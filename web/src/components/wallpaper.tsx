"use client";

import { createContext, useContext, useMemo, useSyncExternalStore } from "react";

export type WallpaperKind = "tahoe" | "milkyway" | "graphite";
export const WALLPAPERS: { kind: WallpaperKind; name: string }[] = [
  { kind: "tahoe", name: "Dunes" },
  { kind: "milkyway", name: "Milky Way" },
  { kind: "graphite", name: "Graphite" },
];

// Dunes at dusk, drawn here so no image is needed: a deep sky into a warm horizon, a low sun,
// and five ridgelines, each lit along its crest. Static on purpose.
function dunes(): string {
  const ridges = [
    { y: 610, a: 70, f: 0.0042, p: 0.6, top: "#5a3d8f", bottom: "#2a2154", lit: "#f0a0b8" },
    { y: 680, a: 60, f: 0.0051, p: 2.1, top: "#43307a", bottom: "#1d1a44", lit: "#e58aa8" },
    { y: 760, a: 75, f: 0.0036, p: 4.0, top: "#2f2a6c", bottom: "#141638", lit: "#b98be0" },
    { y: 850, a: 55, f: 0.0047, p: 1.3, top: "#1f2358", bottom: "#0c0f2a", lit: "#8f8ff0" },
    { y: 940, a: 45, f: 0.0058, p: 5.2, top: "#151a44", bottom: "#070a1d", lit: "#6f86f2" },
  ];
  const path = (r: (typeof ridges)[number]) => {
    let d = `M0 1000 L0 ${r.y}`;
    for (let x = 0; x <= 1600; x += 40) {
      const y = r.y - r.a * Math.sin(x * r.f + r.p) - r.a * 0.35 * Math.sin(x * r.f * 2.7 + r.p * 1.9);
      d += ` L${x} ${y.toFixed(1)}`;
    }
    return `${d} L1600 1000 Z`;
  };
  const defs = ridges
    .map(
      (r, i) =>
        `<linearGradient id="d${i}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${r.top}"/><stop offset="1" stop-color="${r.bottom}"/></linearGradient>`,
    )
    .join("");
  const shapes = ridges
    .map(
      (r, i) =>
        `<path d="${path(r)}" fill="url(#d${i})"/><path d="${path(r)}" fill="none" stroke="${r.lit}" stroke-opacity="0.35" stroke-width="1.5"/>`,
    )
    .join("");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 1000" preserveAspectRatio="xMidYMid slice"><defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#070a24"/><stop offset="0.38" stop-color="#1f1c5c"/><stop offset="0.6" stop-color="#7a3f86"/><stop offset="0.72" stop-color="#e2867e"/><stop offset="0.8" stop-color="#f7b98c"/></linearGradient><radialGradient id="sun" cx="0.68" cy="0.6" r="0.35"><stop offset="0" stop-color="#ffd9a8" stop-opacity="0.95"/><stop offset="0.25" stop-color="#ff9e7a" stop-opacity="0.45"/><stop offset="1" stop-color="#ff9e7a" stop-opacity="0"/></radialGradient>${defs}</defs><rect width="1600" height="1000" fill="url(#sky)"/><rect width="1600" height="1000" fill="url(#sun)"/>${shapes}</svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

type Ctx = { kind: WallpaperKind; setKind: (k: WallpaperKind) => void };
const WallpaperContext = createContext<Ctx>({ kind: "tahoe", setKind: () => {} });
export const useWallpaper = () => useContext(WallpaperContext);

// The choice lives in localStorage (a per-browser convenience); reading it through
// useSyncExternalStore keeps the server render and the first client render the same.
const KEY = "shipjack.wallpaper";
const CHANGED = "shipjack-wallpaper";

function readKind(): WallpaperKind {
  try {
    const saved = localStorage.getItem(KEY);
    return WALLPAPERS.some((w) => w.kind === saved) ? (saved as WallpaperKind) : "tahoe";
  } catch {
    return "tahoe"; // private mode or blocked storage: the default
  }
}

function subscribe(onChange: () => void) {
  window.addEventListener("storage", onChange);
  window.addEventListener(CHANGED, onChange);
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(CHANGED, onChange);
  };
}

export function WallpaperProvider({ children }: { children: React.ReactNode }) {
  const kind = useSyncExternalStore(subscribe, readKind, () => "tahoe" as WallpaperKind);
  const tahoe = useMemo(() => dunes(), []);
  const setKind = (k: WallpaperKind) => {
    try {
      localStorage.setItem(KEY, k);
    } catch {
      // the choice just won't be remembered
    }
    window.dispatchEvent(new Event(CHANGED));
  };

  return (
    <WallpaperContext.Provider value={{ kind, setKind }}>
      <div aria-hidden className="wallpaper" data-kind={kind} style={{ ["--tahoe" as string]: tahoe }} />
      {children}
    </WallpaperContext.Provider>
  );
}
