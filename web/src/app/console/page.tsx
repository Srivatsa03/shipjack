import type { Metadata } from "next";
import { ConsoleApp } from "@/components/console/console-app";

export const metadata: Metadata = { title: "Northwind store console" };

export default function ConsolePage() {
  return <ConsoleApp />;
}
