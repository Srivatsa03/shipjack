# shipjack web

The web app for the shipjack shopping assistant, in Apple's Liquid Glass style (Next.js 16, React 19,
Tailwind 4).

- `/` is the shopper's chat: the agents' steps stream in live, search results become product cards,
  and every purchase stops at an approval sheet that shows the item, the total and where it ships.
- `/console` is the store's console: refund requests that wait for a person, orders with ship and
  deliver actions, a replay of every conversation's agent steps, and health numbers.

It talks to the assistant API (`shipjack-assistant`, default `http://127.0.0.1:8200`).

```bash
npm install
npm run dev          # http://localhost:3000
```

Settings (all optional): `NEXT_PUBLIC_API_URL`, and the demo sign-in tokens
`NEXT_PUBLIC_SHOPPER_TOKEN` (default `demo-shopper`) and `NEXT_PUBLIC_OPERATOR_TOKEN`
(default `demo-operator`). The tokens are a stand-in for real sign-in.

## The material

`src/app/globals.css` defines the glass (`.glass`, `.glass-clear`, `.glass-tint`, `.glass-thick`).
Glass is only for what floats above the content (sidebar, toolbar, composer, sheets, controls),
never glass on glass. `src/components/liquid-lens.tsx` bends the backdrop at the rim of small glass
controls on Chromium; other browsers get frosted glass. Motion is one orchestrated moment (the
approval sheet rising), and reduced motion and reduced transparency are respected.

Wallpapers: Dunes (drawn in code), the Milky Way (photo by Benjamin Voros on Unsplash) and Graphite.
