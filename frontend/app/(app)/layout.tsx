import type { ReactNode } from "react";

import { Shell } from "@/components/shell";

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <>
      {/* Fixed colour-mesh wallpaper behind every page. Decorative, so hidden
          from assistive tech and non-interactive. */}
      <div aria-hidden="true" className="app-canvas" />
      <Shell>{children}</Shell>
    </>
  );
}
