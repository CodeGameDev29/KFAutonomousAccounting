/**
 * Shared resizable-column hook and row-flash helper used by both the
 * authenticated AccountDetail page (/reports/:year/:month) and the public
 * SharedReport page (/shared/:token).
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface ResizableColumnDef {
  key: string;
  label: string;
  initial: number;
  min: number;
  align?: "left" | "right" | "center";
}

export function useResizableColumns(columns: ResizableColumnDef[]) {
  const [widths, setWidths] = useState<Record<string, number>>(() =>
    Object.fromEntries(columns.map((c) => [c.key, c.initial])),
  );

  const dragging = useRef<{ key: string; startX: number; startWidth: number } | null>(null);

  const beginResize = useCallback(
    (key: string, e: React.MouseEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      dragging.current = {
        key,
        startX: e.clientX,
        startWidth: widths[key],
      };
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [widths],
  );

  useEffect(() => {
    function onMove(e: MouseEvent) {
      const drag = dragging.current;
      if (!drag) return;
      const col = columns.find((c) => c.key === drag.key);
      const min = col?.min ?? 60;
      const next = Math.max(min, drag.startWidth + (e.clientX - drag.startX));
      setWidths((prev) => (prev[drag.key] === next ? prev : { ...prev, [drag.key]: next }));
    }
    function onUp() {
      if (!dragging.current) return;
      dragging.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [columns]);

  return { widths, beginResize };
}

/** Highlight a target row by DOM id — scrolls into view and adds a flash class. */
export function flashRow(id: string): void {
  const row = document.getElementById(id);
  if (!row) return;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.remove("shared-highlight-row");
  // force reflow so re-adding the class restarts the animation
  void (row as HTMLElement).offsetWidth;
  row.classList.add("shared-highlight-row");
  window.setTimeout(() => row.classList.remove("shared-highlight-row"), 2000);
}

/** Same as flashRow but polls for up to 800 ms — used for cross-tab jumps. */
export function flashRowWhenMounted(id: string): void {
  const start = Date.now();
  function tick() {
    if (document.getElementById(id)) {
      flashRow(id);
      return;
    }
    if (Date.now() - start < 800) {
      window.requestAnimationFrame(tick);
    }
  }
  tick();
}

/** Inline CSS for the shared-highlight-row flash animation. Inject once per page. */
export const HIGHLIGHT_ROW_CSS = `
  @keyframes shared-highlight-flash {
    0%   { background-color: #fff3b0; box-shadow: inset 4px 0 0 #f0c419; }
    70%  { background-color: #fff3b0; box-shadow: inset 4px 0 0 #f0c419; }
    100% { background-color: transparent; box-shadow: inset 4px 0 0 transparent; }
  }
  tr.shared-highlight-row {
    animation: shared-highlight-flash 2s ease-out;
  }
`;
