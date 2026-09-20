#!/usr/bin/env python3
"""
Pipeline script for address-manual-test-bug-report skill.

All business logic for the multi-phase bug fixing pipeline. The orchestrator
calls these subcommands and spawns the subagents between them.

Subcommands used by SKILL.md orchestration:
  setup <bug_report_file>                              Parse bugs, create run dir
  generate-prompt <research|planning|fixing> <id> <dir> Emit this phase's agent brief
  validate <run_dir> <research|planning|fixing>         Check phase outputs exist
  compile-kb <run_dir>                                  Aggregate research → knowledge_base.md
  analyze-overlaps <run_dir>                            Plan overlap → execution groups
  compile-report <run_dir>                              Aggregate fixes → final_report.md

Optional/utility (not called by SKILL.md — for manual debugging only):
  update-progress <run_dir> <bug_id> <phase> <status>   Update progress.json per-bug status
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).parent
PROMPTS_DIR = SKILL_DIR / "prompts"


# ═══════════════════════════════════════════════════════════
# SETUP
# ═══════════════════════════════════════════════════════════

def cmd_setup(args):
    """Parse bug report, create run directory, output JSON."""
    if not args:
        print(json.dumps({"error": "Usage: pipeline.py setup <bug_report_file>"}))
        sys.exit(1)

    bug_file = args[0]
    if not os.path.exists(bug_file):
        print(json.dumps({"error": f"File not found: {bug_file}"}))
        sys.exit(1)

    with open(bug_file, "r", encoding="utf-8") as f:
        content = f.read().strip()

    if not content:
        print(json.dumps({"error": "Bug report file is empty"}))
        sys.exit(1)

    bugs = _parse_bugs(content)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = str(SKILL_DIR / "runs" / timestamp)

    for subdir in ["research", "plans", "fixes"]:
        os.makedirs(os.path.join(run_dir, subdir), exist_ok=True)

    output = {
        "source_file": os.path.abspath(bug_file),
        "run_dir": run_dir,
        "timestamp": timestamp,
        "bug_count": len(bugs),
        "bugs": bugs,
    }

    with open(os.path.join(run_dir, "bugs.json"), "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    progress = {
        "phase": "initialized",
        "total_bugs": len(bugs),
        "research_complete": 0,
        "planning_complete": 0,
        "fixing_complete": 0,
        "bugs": {
            str(b["id"]): {
                "title": b["title"],
                "research": "pending",
                "planning": "pending",
                "fixing": "pending",
            }
            for b in bugs
        },
    }

    with open(os.path.join(run_dir, "progress.json"), "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)

    print(json.dumps(output, indent=2))


def _parse_bugs(content):
    """Parse bug report text into structured bugs list.

    Handles: numbered lists (1. / 1)), bullet lists (- / *),
    markdown headers, and fallback to paragraph splitting.
    """
    # Pattern 1: Numbered list — "1." or "1)" at start of line
    numbered = re.split(r"\n(?=\d+[\.\)]\s)", "\n" + content)
    numbered = [s.strip() for s in numbered if s.strip()]
    if len(numbered) > 1:
        bugs = []
        for item in numbered:
            m = re.match(r"(\d+)[\.\)]\s*(.*)", item, re.DOTALL)
            if m:
                text = m.group(2).strip()
                lines = text.split("\n", 1)
                bugs.append({
                    "id": int(m.group(1)),
                    "title": lines[0].strip().rstrip(".")[:150],
                    "description": text,
                })
        if bugs:
            return bugs

    # Pattern 2: Bullet list — "- " or "* " at start of line
    bulleted = re.split(r"\n(?=[-*]\s)", "\n" + content)
    bulleted = [s.strip() for s in bulleted if s.strip()]
    if len(bulleted) > 1:
        bugs = []
        for i, item in enumerate(bulleted):
            text = re.sub(r"^[-*]\s*", "", item).strip()
            if text:
                lines = text.split("\n", 1)
                bugs.append({
                    "id": i + 1,
                    "title": lines[0].strip().rstrip(".")[:150],
                    "description": text,
                })
        if bugs:
            return bugs

    # Pattern 3: Markdown headers — "## Bug 1:" or "### Issue:"
    headers = re.split(r"\n(?=#{1,4}\s)", "\n" + content)
    headers = [s.strip() for s in headers if s.strip()]
    if len(headers) > 1:
        bugs = []
        for i, section in enumerate(headers):
            lines = section.split("\n", 1)
            title = re.sub(r"^#{1,4}\s*", "", lines[0]).strip().rstrip(".")
            desc = lines[1].strip() if len(lines) > 1 else title
            bugs.append({"id": i + 1, "title": title[:150], "description": desc})
        if bugs:
            return bugs

    # Fallback: paragraphs separated by blank lines
    paragraphs = re.split(r"\n\s*\n", content)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    if len(paragraphs) > 1:
        bugs = []
        for i, para in enumerate(paragraphs):
            lines = para.split("\n", 1)
            title = re.sub(r"^[#\-*\d.)]+\s*", "", lines[0]).strip()
            bugs.append({"id": i + 1, "title": title[:150], "description": para})
        return bugs

    # Last resort: entire file is one bug
    return [{"id": 1, "title": content.split("\n", 1)[0][:150], "description": content}]


# ═══════════════════════════════════════════════════════════
# GENERATE PROMPT
# ═══════════════════════════════════════════════════════════

def cmd_generate_prompt(args):
    """Read template, substitute placeholders, print ready-to-use prompt."""
    if len(args) < 3:
        print(json.dumps({"error": "Usage: pipeline.py generate-prompt <research|planning|fixing> <bug_id> <run_dir>"}))
        sys.exit(1)

    agent_type, bug_id, run_dir = args[0], args[1], args[2]

    # Load bug data
    with open(os.path.join(run_dir, "bugs.json"), "r", encoding="utf-8") as f:
        data = json.load(f)

    bug = next((b for b in data["bugs"] if str(b["id"]) == str(bug_id)), None)
    if not bug:
        print(json.dumps({"error": f"Bug {bug_id} not found in bugs.json"}))
        sys.exit(1)

    # Load template
    template_file = PROMPTS_DIR / f"{agent_type}_agent.md"
    if not template_file.exists():
        print(json.dumps({"error": f"Template not found: {template_file}"}))
        sys.exit(1)

    template = template_file.read_text(encoding="utf-8")

    # Build substitution map
    subs = {
        "{BUG_ID}": str(bug["id"]),
        "{BUG_TITLE}": bug["title"],
        "{BUG_DESCRIPTION}": bug["description"],
        "{RUN_DIR}": run_dir,
    }

    if agent_type == "research":
        subs["{OUTPUT_PATH}"] = os.path.join(run_dir, "research", f"bug_{bug_id}_research.md")
    elif agent_type == "planning":
        subs["{OUTPUT_PATH}"] = os.path.join(run_dir, "plans", f"bug_{bug_id}_plan.md")
        subs["{KNOWLEDGE_BASE_PATH}"] = os.path.join(run_dir, "knowledge_base.md")
    elif agent_type == "fixing":
        subs["{OUTPUT_PATH}"] = os.path.join(run_dir, "fixes", f"bug_{bug_id}_fix_report.md")
        subs["{PLAN_PATH}"] = os.path.join(run_dir, "plans", f"bug_{bug_id}_plan.md")

    prompt = template
    for key, value in subs.items():
        prompt = prompt.replace(key, value)

    # Output the fully substituted prompt (not JSON — raw text for direct use)
    print(prompt)


# ═══════════════════════════════════════════════════════════
# VALIDATE PHASE
# ═══════════════════════════════════════════════════════════

def cmd_validate(args):
    """Check all expected output files exist for a given phase."""
    if len(args) < 2:
        print(json.dumps({"error": "Usage: pipeline.py validate <run_dir> <research|planning|fixing>"}))
        sys.exit(1)

    run_dir, phase = args[0], args[1]

    with open(os.path.join(run_dir, "bugs.json"), "r", encoding="utf-8") as f:
        data = json.load(f)

    dir_map = {"research": "research", "planning": "plans", "fixing": "fixes"}
    suffix_map = {"research": "research", "planning": "plan", "fixing": "fix_report"}

    subdir = dir_map.get(phase)
    suffix = suffix_map.get(phase)

    if not subdir or not suffix:
        print(json.dumps({"error": f"Unknown phase: {phase}. Use research|planning|fixing"}))
        sys.exit(1)

    results = {
        "phase": phase,
        "total": data["bug_count"],
        "complete": 0,
        "missing": [],
        "present": [],
    }

    for bug in data["bugs"]:
        filename = f"bug_{bug['id']}_{suffix}.md"
        filepath = os.path.join(run_dir, subdir, filename)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 100:
            results["complete"] += 1
            results["present"].append({
                "bug_id": bug["id"],
                "title": bug["title"],
                "file": filepath,
                "size_bytes": os.path.getsize(filepath),
            })
        else:
            results["missing"].append({
                "bug_id": bug["id"],
                "title": bug["title"],
                "expected": filepath,
                "exists": os.path.exists(filepath),
                "size": os.path.getsize(filepath) if os.path.exists(filepath) else 0,
            })

    results["all_complete"] = results["complete"] == results["total"]
    print(json.dumps(results, indent=2))


# ═══════════════════════════════════════════════════════════
# COMPILE KNOWLEDGE BASE
# ═══════════════════════════════════════════════════════════

def cmd_compile_kb(args):
    """Read all research reports, extract cross-references, write knowledge_base.md."""
    if not args:
        print(json.dumps({"error": "Usage: pipeline.py compile-kb <run_dir>"}))
        sys.exit(1)

    run_dir = args[0]

    with open(os.path.join(run_dir, "bugs.json"), "r", encoding="utf-8") as f:
        data = json.load(f)

    research_dir = os.path.join(run_dir, "research")

    # Collect research reports and extract file references for cross-referencing
    reports = []
    file_to_bugs = {}  # file path → list of bug IDs that reference it

    FILE_EXT_PATTERN = r"(?:py|ts|tsx|js|jsx|sql|yaml|yml|json|md|html|css|env)"

    for bug in data["bugs"]:
        report_path = os.path.join(research_dir, f"bug_{bug['id']}_research.md")
        if not os.path.exists(report_path):
            continue

        content = open(report_path, "r", encoding="utf-8").read()
        reports.append({"bug": bug, "content": content})

        # Extract file path references (backtick-wrapped or bare)
        refs_backtick = re.findall(rf"`([\w/.\-]+\.(?:{FILE_EXT_PATTERN}))`", content)
        refs_bare = re.findall(rf"(?:^|\s)([\w/.\-]+\.(?:{FILE_EXT_PATTERN}))", content, re.MULTILINE)
        all_refs = set(refs_backtick) | set(refs_bare)

        for ref in all_refs:
            file_to_bugs.setdefault(ref, set()).add(bug["id"])

    shared_files = {f: sorted(bids) for f, bids in file_to_bugs.items() if len(bids) > 1}

    # Build knowledge base document
    lines = [
        "# Knowledge Base — Bug Fix Research Synthesis",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Total bugs researched: {len(reports)}/{data['bug_count']}",
        "",
    ]

    # Cross-reference: files touched by multiple bugs
    if shared_files:
        lines.extend([
            "## Cross-Reference: Shared Files",
            "",
            "These files are relevant to multiple bugs. Fixes must be coordinated.",
            "",
            "| File | Bugs |",
            "|------|------|",
        ])
        for fpath in sorted(shared_files):
            bids = shared_files[fpath]
            lines.append(f"| `{fpath}` | {', '.join(f'#{b}' for b in bids)} |")
        lines.append("")

        # Dependency pairs
        lines.extend([
            "## Dependency Ordering",
            "",
            "Bug pairs sharing files (sequential fixing recommended):",
            "",
        ])
        seen = set()
        for fpath, bids in shared_files.items():
            for i, a in enumerate(bids):
                for b in bids[i + 1:]:
                    pair = (min(a, b), max(a, b))
                    if pair not in seen:
                        seen.add(pair)
                        lines.append(f"- Bug #{pair[0]} <-> Bug #{pair[1]} (share `{fpath}`)")
        lines.append("")

    # All referenced files index
    lines.extend([
        "## All Referenced Files",
        "",
        "| File | Referenced By |",
        "|------|---------------|",
    ])
    for fpath in sorted(file_to_bugs):
        bids = sorted(file_to_bugs[fpath])
        lines.append(f"| `{fpath}` | {', '.join(f'#{b}' for b in bids)} |")
    lines.append("")

    # Per-bug research sections
    for report in reports:
        bug = report["bug"]
        lines.extend([
            "---",
            "",
            f"## Bug #{bug['id']}: {bug['title']}",
            "",
            report["content"],
            "",
        ])

    kb_path = os.path.join(run_dir, "knowledge_base.md")
    with open(kb_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(json.dumps({
        "status": "complete",
        "path": kb_path,
        "bugs_included": len(reports),
        "total_files_referenced": len(file_to_bugs),
        "shared_files": len(shared_files),
    }, indent=2))


# ═══════════════════════════════════════════════════════════
# ANALYZE OVERLAPS
# ═══════════════════════════════════════════════════════════

def cmd_analyze_overlaps(args):
    """Read plan files, extract files-modified, compute execution groups."""
    if not args:
        print(json.dumps({"error": "Usage: pipeline.py analyze-overlaps <run_dir>"}))
        sys.exit(1)

    run_dir = args[0]

    with open(os.path.join(run_dir, "bugs.json"), "r", encoding="utf-8") as f:
        data = json.load(f)

    plans_dir = os.path.join(run_dir, "plans")
    FILE_EXT_PATTERN = r"(?:py|ts|tsx|js|jsx|sql|yaml|yml|json|html|css)"

    # Extract files-modified per bug
    bug_files = {}
    for bug in data["bugs"]:
        plan_path = os.path.join(plans_dir, f"bug_{bug['id']}_plan.md")
        if not os.path.exists(plan_path):
            bug_files[bug["id"]] = set()
            continue

        content = open(plan_path, "r", encoding="utf-8").read()
        files = set()

        # Method 1: "Files Modified" section
        in_section = False
        for line in content.split("\n"):
            if re.match(r"^#{1,3}\s*Files Modified", line, re.IGNORECASE):
                in_section = True
                continue
            if in_section:
                if re.match(r"^#{1,3}\s", line):
                    break
                refs = re.findall(rf"`([^`]+\.(?:{FILE_EXT_PATTERN}))`", line)
                files.update(refs)
                refs2 = re.findall(rf"(?:^|\s)([\w/.\-]+\.(?:{FILE_EXT_PATTERN}))", line)
                files.update(refs2)

        # Method 2: "**File**:" lines in implementation steps
        file_refs = re.findall(r"\*\*File\*\*:\s*`?([^\s`]+)`?", content)
        files.update(file_refs)

        bug_files[bug["id"]] = files

    # Find pairwise overlaps
    bug_ids = [bug["id"] for bug in data["bugs"]]
    overlaps = {}
    for i, a in enumerate(bug_ids):
        for b in bug_ids[i + 1:]:
            shared = bug_files.get(a, set()) & bug_files.get(b, set())
            if shared:
                overlaps[f"{a}-{b}"] = sorted(shared)

    # Connected components via BFS for execution grouping
    adjacency = {bid: set() for bid in bug_ids}
    for key in overlaps:
        a, b = map(int, key.split("-"))
        adjacency[a].add(b)
        adjacency[b].add(a)

    visited = set()
    groups = []
    for bid in bug_ids:
        if bid in visited:
            continue
        component = []
        queue = [bid]
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            component.append(node)
            queue.extend(sorted(adjacency[node] - visited))
        groups.append(sorted(component))

    # Build execution order
    execution_order = []
    parallel_singles = [g[0] for g in groups if len(g) == 1]
    sequential_groups = [g for g in groups if len(g) > 1]

    if not overlaps:
        # No overlaps at all — everything parallel
        execution_order = [{"mode": "parallel", "bug_ids": bug_ids}]
    else:
        if parallel_singles:
            execution_order.append({"mode": "parallel", "bug_ids": parallel_singles})
        for group in sequential_groups:
            execution_order.append({"mode": "sequential", "bug_ids": group})

    result = {
        "total_bugs": len(bug_ids),
        "has_overlaps": bool(overlaps),
        "overlaps": overlaps,
        "execution_order": execution_order,
    }

    print(json.dumps(result, indent=2))


# ═══════════════════════════════════════════════════════════
# COMPILE FINAL REPORT
# ═══════════════════════════════════════════════════════════

def cmd_compile_report(args):
    """Read all fix reports, extract stats, assemble final_report.md."""
    if not args:
        print(json.dumps({"error": "Usage: pipeline.py compile-report <run_dir>"}))
        sys.exit(1)

    run_dir = args[0]

    with open(os.path.join(run_dir, "bugs.json"), "r", encoding="utf-8") as f:
        data = json.load(f)

    fixes_dir = os.path.join(run_dir, "fixes")

    fixed = partial = failed = no_report = 0
    all_files_modified = set()
    bug_sections = []
    qa_rows = []

    for bug in data["bugs"]:
        report_path = os.path.join(fixes_dir, f"bug_{bug['id']}_fix_report.md")

        if not os.path.exists(report_path):
            no_report += 1
            bug_sections.append(
                f"### Bug #{bug['id']}: {bug['title']}\n\n"
                f"**Status**: NO REPORT FOUND\n\n"
                f"The fixing agent did not produce a report for this bug.\n"
            )
            qa_rows.append(
                f"| #{bug['id']} | {bug['title'][:50]} | - | - | - | NO REPORT |"
            )
            continue

        content = open(report_path, "r", encoding="utf-8").read()

        # Extract status — handles multiple formats:
        #   "## Status\n\n**FIXED** -- ..."
        #   "## Status: FIXED"
        #   "## Status: ALREADY FIXED (by ...)"
        #   "**Status**: FIXED"
        status_raw = None
        for pattern in (
            r"##\s*Status\s*\n+\s*\*{0,2}([A-Za-z][A-Za-z ]*?)\*{0,2}\s*(?:--|\n|$)",
            r"##\s*Status:\s*\*{0,2}([^\n*]+?)\*{0,2}\s*(?:--|\n|$)",
            r"^\*\*Status\*\*:\s*([^\n]+)",
        ):
            m = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
            if m:
                status_raw = m.group(1).strip().upper()
                break

        if status_raw and "FIXED" in status_raw:
            status = "FIXED"
        elif status_raw and "PARTIAL" in status_raw:
            status = "PARTIAL"
        elif status_raw and ("FAIL" in status_raw or "UNRESOLVED" in status_raw):
            status = "FAILED"
        else:
            status = "UNKNOWN"

        if status == "FIXED":
            fixed += 1
        elif status == "PARTIAL":
            partial += 1
        else:
            failed += 1

        # Extract file paths from change sections
        file_refs = re.findall(
            r"(?:###\s*Change\s*\d+:\s*\[?|`)"
            r"([\w/.\-]+\.(?:py|ts|tsx|js|jsx|sql|yaml|yml|json|html|css))",
            content,
        )
        all_files_modified.update(file_refs)

        # Extract test results
        test_results = re.findall(r"\*\*Result\*\*:\s*(PASS|FAIL)", content, re.IGNORECASE)
        t_pass = sum(1 for t in test_results if t.upper() == "PASS")
        t_fail = sum(1 for t in test_results if t.upper() == "FAIL")
        t_total = len(test_results)

        files_str = ", ".join(f"`{f}`" for f in file_refs) if file_refs else "See report"
        tests_str = f"{t_pass}/{t_total} passed"
        if t_fail:
            tests_str += f", {t_fail} failed"

        bug_sections.append(
            f"### Bug #{bug['id']}: {bug['title']}\n\n"
            f"**Status**: {status}\n\n"
            f"**Tests**: {tests_str}\n\n"
            f"**Files Modified**: {files_str}\n\n"
            f"**Full Report**: `{report_path}`\n"
        )

        qa_rows.append(
            f"| #{bug['id']} | {bug['title'][:50]} | {t_total} | {t_pass} | {t_fail} | {status} |"
        )

    total = data["bug_count"]
    pct = (100 * fixed // total) if total else 0

    report = (
        f"# Final Bug Fix Report\n\n"
        f"## Executive Summary\n\n"
        f"- **Total bugs**: {total}\n"
        f"- **Fixed**: {fixed} | **Partial**: {partial} | **Failed**: {failed} | **No report**: {no_report}\n"
        f"- **Success rate**: {fixed}/{total} ({pct}%)\n"
        f"- **Total files modified**: {len(all_files_modified)}\n\n"
        f"## QA Summary\n\n"
        f"| Bug | Title | Tests Run | Passed | Failed | Status |\n"
        f"|-----|-------|-----------|--------|--------|--------|\n"
        + "\n".join(qa_rows)
        + "\n\n"
        "## Per-Bug Details\n\n"
        + "\n".join(bug_sections)
        + "\n"
        "## Files Modified (All Bugs)\n\n"
        + (
            "\n".join(f"- `{f}`" for f in sorted(all_files_modified))
            if all_files_modified
            else "- See individual fix reports"
        )
        + "\n\n"
        f"## Artifacts\n\n"
        f"- Bug report: `{data['source_file']}`\n"
        f"- Parsed bugs: `{os.path.join(run_dir, 'bugs.json')}`\n"
        f"- Knowledge base: `{os.path.join(run_dir, 'knowledge_base.md')}`\n"
        f"- Research reports: `{os.path.join(run_dir, 'research/')}`\n"
        f"- Fix plans: `{os.path.join(run_dir, 'plans/')}`\n"
        f"- Fix reports: `{os.path.join(run_dir, 'fixes/')}`\n"
    )

    report_path = os.path.join(run_dir, "final_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(json.dumps({
        "status": "complete",
        "path": report_path,
        "fixed": fixed,
        "partial": partial,
        "failed": failed,
        "no_report": no_report,
        "total": total,
        "success_rate_pct": pct,
    }, indent=2))


# ═══════════════════════════════════════════════════════════
# UPDATE PROGRESS
# ═══════════════════════════════════════════════════════════

def cmd_update_progress(args):
    """Update progress.json for a specific bug and phase."""
    if len(args) < 4:
        print(json.dumps({"error": "Usage: pipeline.py update-progress <run_dir> <bug_id> <phase> <status>"}))
        sys.exit(1)

    run_dir, bug_id, phase, status = args[0], args[1], args[2], args[3]
    progress_path = os.path.join(run_dir, "progress.json")

    with open(progress_path, "r", encoding="utf-8") as f:
        progress = json.load(f)

    if bug_id in progress["bugs"]:
        progress["bugs"][bug_id][phase] = status

    # Recount completions
    for p in ["research", "planning", "fixing"]:
        progress[f"{p}_complete"] = sum(
            1 for b in progress["bugs"].values() if b.get(p) == "complete"
        )

    progress["phase"] = phase

    with open(progress_path, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)

    print(json.dumps(progress, indent=2))


# ═══════════════════════════════════════════════════════════
# MAIN DISPATCH
# ═══════════════════════════════════════════════════════════

COMMANDS = {
    "setup": cmd_setup,
    "generate-prompt": cmd_generate_prompt,
    "validate": cmd_validate,
    "compile-kb": cmd_compile_kb,
    "analyze-overlaps": cmd_analyze_overlaps,
    "compile-report": cmd_compile_report,
    "update-progress": cmd_update_progress,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        cmds = " | ".join(COMMANDS.keys())
        print(json.dumps({"error": f"Usage: pipeline.py <{cmds}> [args...]"}))
        sys.exit(1)

    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
