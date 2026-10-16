import type { Metadata, Viewport } from "next";
import { WallpaperProvider } from "@/components/wallpaper";
import "./globals.css";

export const metadata: Metadata = {
  title: "Northwind Assistant",
  description: "Shop Northwind Electronics with an AI assistant that asks before it buys.",
};

export const viewport: Viewport = {
  themeColor: "#0b0d1a",
  colorScheme: "dark",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <WallpaperProvider>{children}</WallpaperProvider>
      </body>
    </html>
  );
}
