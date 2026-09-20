import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  LayoutDashboard,
  ArrowLeftRight,
  BarChart3,
  Settings,
  Upload,
  RefreshCw,
  FileText,
  Search,
  LogOut,
} from "lucide-react";
import { useAuthStore } from "@/stores/auth-store";
import { api } from "@/lib/api";

interface Command {
  id: string;
  label: string;
  icon: React.ElementType;
  action: () => void;
  keywords?: string[];
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const signOut = useAuthStore((s) => s.signOut);

  const commands: Command[] = useMemo(
    () => [
      {
        id: "dashboard",
        label: "Go to Dashboard",
        icon: LayoutDashboard,
        action: () => navigate("/dashboard"),
        keywords: ["home", "overview"],
      },
      {
        id: "transactions",
        label: "Go to Transactions",
        icon: ArrowLeftRight,
        action: () => navigate("/transactions"),
        keywords: ["bank", "receipts"],
      },
      {
        id: "reports",
        label: "Go to Reports",
        icon: BarChart3,
        action: () => navigate("/reports"),
        keywords: ["ledger", "export"],
      },
      {
        id: "settings",
        label: "Go to Settings",
        icon: Settings,
        action: () => navigate("/settings"),
        keywords: ["profile", "connections"],
      },
      {
        id: "upload",
        label: "Upload Receipt",
        icon: Upload,
        action: () => navigate("/transactions?action=upload"),
        keywords: ["file", "document", "invoice"],
      },
      {
        id: "reconcile",
        label: "Match Receipts",
        icon: RefreshCw,
        action: async () => {
          await api.post("/api/actions/reconcile");
          queryClient.invalidateQueries({ queryKey: ["dashboard"] });
          queryClient.invalidateQueries({ queryKey: ["transactions"] });
        },
        keywords: ["match", "sync"],
      },
      {
        id: "export-pdf",
        label: "Download Current Report (PDF)",
        icon: FileText,
        action: () => {
          const now = new Date();
          window.open(
            `/api/reports/${now.getFullYear()}/${now.getMonth() + 1}/pdf`,
            "_blank"
          );
        },
        keywords: ["download", "pdf"],
      },
      {
        id: "signout",
        label: "Sign Out",
        icon: LogOut,
        action: () => signOut(),
        keywords: ["logout", "exit"],
      },
    ],
    [navigate, queryClient, signOut]
  );

  const filtered = useMemo(() => {
    if (!query) return commands;
    const q = query.toLowerCase();
    return commands.filter(
      (cmd) =>
        cmd.label.toLowerCase().includes(q) ||
        cmd.keywords?.some((k) => k.includes(q))
    );
  }, [commands, query]);

  useEffect(() => {
    setSelectedIndex(0);
  }, [filtered]);

  const execute = useCallback(
    (cmd: Command) => {
      setOpen(false);
      setQuery("");
      cmd.action();
    },
    []
  );

  // Keyboard shortcut: Cmd+K / Ctrl+K
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((prev) => !prev);
        setQuery("");
      }
      if (e.key === "Escape" && open) {
        setOpen(false);
        setQuery("");
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open]);

  // Arrow key navigation
  useEffect(() => {
    if (!open) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((i) => Math.min(i + 1, filtered.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (filtered[selectedIndex]) {
          execute(filtered[selectedIndex]);
        }
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, filtered, selectedIndex, execute]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[20vh]">
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/50 backdrop-blur-sm"
        onClick={() => {
          setOpen(false);
          setQuery("");
        }}
      />

      {/* Palette */}
      <div className="relative w-full max-w-lg rounded-xl border bg-background shadow-warm-md">
        {/* Search input */}
        <div className="flex items-center gap-3 border-b px-4">
          <Search className="h-4 w-4 text-primary" />
          <input
            type="text"
            placeholder="Type a command..."
            className="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
          <kbd className="rounded-md border bg-muted px-1.5 py-0.5 text-xs font-mono text-muted-foreground">
            ESC
          </kbd>
        </div>

        {/* Results */}
        <div className="max-h-64 overflow-y-auto p-2">
          {filtered.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              No commands found.
            </p>
          ) : (
            filtered.map((cmd, i) => (
              <button
                key={cmd.id}
                className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                  i === selectedIndex
                    ? "bg-primary/10 text-primary"
                    : "text-foreground hover:bg-muted/50"
                }`}
                onClick={() => execute(cmd)}
                onMouseEnter={() => setSelectedIndex(i)}
              >
                <cmd.icon className={`h-4 w-4 ${i === selectedIndex ? "text-primary" : "text-muted-foreground"}`} />
                {cmd.label}
              </button>
            ))
          )}
        </div>

        {/* Footer hint */}
        <div className="flex items-center gap-4 border-t px-4 py-2 text-xs text-muted-foreground">
          <span>
            <kbd className="rounded-md border bg-muted px-1 py-0.5 font-mono">&#8593;&#8595;</kbd> Navigate
          </span>
          <span>
            <kbd className="rounded-md border bg-muted px-1 py-0.5 font-mono">&#8629;</kbd> Select
          </span>
          <span>
            <kbd className="rounded-md border bg-muted px-1 py-0.5 font-mono">Esc</kbd> Close
          </span>
        </div>
      </div>
    </div>
  );
}
