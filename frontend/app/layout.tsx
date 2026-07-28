import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "AgFabric AI — Agricultural Intelligence Platform",
  description:
    "Unified operational intelligence for agricultural businesses: hybrid AI search, knowledge graph, risk detection, and full source traceability.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  // lang is required for screen readers to pick the right pronunciation rules.
  return (
    <html lang="en">
      <body className="min-h-dvh antialiased">
        <a href="#main" className="skip-link">
          Skip to main content
        </a>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
