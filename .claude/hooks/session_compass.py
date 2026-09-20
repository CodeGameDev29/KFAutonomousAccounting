#!/usr/bin/env python3
"""SessionStart hook - a short, live orientation.

CLAUDE.md carries the static guidance; this adds only what changes between sessions: git state,
whether the local stack is listening, and what the backend's own /health says about the database,
storage and the LLM endpoint. Everything it touches is on loopback - it makes no request to any
other machine. Fail-soft everywhere; always exits 0.
"""
import json
import os
import subprocess
import sys
import urllib.request

VITE_PORT = "5174"  # pinned in web/vite.config.ts


def sh(args, timeout=6):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def listening_ports():
    """Ports holding a TCP listener, from whichever tool this platform has."""
    ports = set()
    for ln in sh(["netstat", "-an"]).splitlines():
        parts = ln.split()
        if len(parts) >= 4 and "LISTEN" in ln.upper():
            for field in parts[1:5]:
                sep = ":" if ":" in field else "."
                tail = field.rsplit(sep, 1)[-1]
                if tail.isdigit():
                    ports.add(tail)
                    break
    return ports


def api_port(root):
    """PORT is the only line read from .env; nothing else in that file is looked at."""
    try:
        with open(os.path.join(root, ".env"), encoding="utf-8", errors="ignore") as fh:
            for ln in fh:
                if ln.strip().startswith("PORT="):
                    return ln.split("=", 1)[1].strip() or "8080"
    except Exception:
        pass
    return "8080"


def local_health(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%s/health" % port, timeout=4) as resp:
            return json.loads(resp.read(16384).decode("utf-8", "replace"))
    except Exception:
        return None


def summarize(body):
    """One line out of /health: overall status, then the extraction block compacted."""
    bits = ["status=%s" % body.get("status", "?")]
    block = body.get("extraction")
    if isinstance(block, dict):
        for key, val in block.items():
            if isinstance(val, dict):
                val = "reachable" if val.get("reachable") else "unreachable"
            elif isinstance(val, list):
                val = ">".join(str(v) for v in val) or "-"
            bits.append("%s=%s" % (key, val))
    return " ".join(bits)


def main():
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    out = ["[compass] Autonomous Accounting - self-hosted; correctness over speed, synthetic "
           "data only."]
    out.append("  git: %s @ %s" % (
        sh(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"]).strip() or "?",
        sh(["git", "-C", root, "log", "-1", "--format=%h %s"]).strip() or "?"))

    dirty = [e[3:] for e in sh(["git", "-C", root, "status", "--porcelain"]).splitlines()
             if len(e) > 3]
    if dirty:
        out.append("  dirty tree (%d): %s%s" % (len(dirty), ", ".join(dirty[:5]),
                                                " ..." if len(dirty) > 5 else ""))

    missing = [name for name, rel in ((".env", ".env"), (".venv", ".venv"),
                                      ("web/node_modules", "web/node_modules"))
               if not os.path.exists(os.path.join(root, rel))]
    if missing:
        out.append("  not set up yet: %s (README.md > Quickstart)" % ", ".join(missing))

    ports, port = listening_ports(), api_port(root)
    out.append("  listening: API :%s %s | vite :%s %s" % (
        port, "UP" if port in ports else "down", VITE_PORT,
        "up" if VITE_PORT in ports else "off"))
    if port in ports:
        body = local_health(port)
        out.append("  /health: " + (summarize(body) if isinstance(body, dict)
                                    else "no answer - read the uvicorn output"))

    out.append("  gates: ./scripts/gates.sh (ruff, pytest, web typecheck, web build) - there is "
               "no CI")
    print("\n".join(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
