"""Replay real repositories' history through Covenant.

For each repo: clone recent history, compile a standard instruction at an older commit, then
judge each of the following real commits with the CI check (`run_ci`, tests not run). Real,
reviewed commits by maintainers are mostly fine, so what Covenant flags is a list of candidates
to review: real rule breaks (an API removal) or false alarms to fix.

Nothing from the repos is executed: checkers read source with parsers and git.

    python -m covenant.bench.replay bench/replay/repos.txt --work DIR --out results.jsonl
    python -m covenant.bench.replay --report results.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

INSTRUCTION = (
    "Don't change public APIs, don't commit secrets or API keys, don't delete or skip tests, "
    "don't add new dependencies, no leftover debug prints, don't silence type errors, "
    "don't commit files over 5 MB, and run tests before committing."
)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()[:300]}")
    return result.stdout


def clone(url: str, dest: Path, depth: int) -> None:
    if (dest / ".git").exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-c", "core.longpaths=true", "clone", "-q", "--depth", str(depth), "--single-branch",
                    "--no-tags", url, str(dest)], check=True, capture_output=True, text=True)
    _git(dest, "config", "core.longpaths", "true")
    _git(dest, "config", "advice.detachedHead", "false")


def _short(value, limit: int = 300):
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + "..."


def replay_repo(url: str, work: Path, commits: int = 30, depth: int = 150, instruction: str = INSTRUCTION) -> dict:
    from ..ci import run_ci
    from ..compiler.extract import HeuristicExtractor
    from ..compiler.pipeline import compile_instruction

    name = url.rstrip("/").removesuffix(".git").split("github.com/")[-1]
    repo = work / name.replace("/", "__")
    out: dict = {"repo": name, "url": url, "commits": [], "error": None}
    started = time.time()
    try:
        clone(url, repo, depth)
        head = (_git(repo, "rev-parse", "--verify", "-q", "origin/HEAD", check=False).strip()
                or _git(repo, "rev-parse", "HEAD").strip())
        _git(repo, "checkout", "-q", "--force", "--detach", head)
        shas = _git(repo, "rev-list", "--first-parent", "--no-merges", f"--max-count={commits}", head).split()
        shas = [s for s in shas if _git(repo, "rev-parse", "-q", "--verify", f"{s}^", check=False).strip()]
        if not shas:
            raise RuntimeError("no commits with a parent in the clone")
        base0 = _git(repo, "rev-parse", f"{shas[-1]}^").strip()

        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        if "covenant.yaml" not in exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else True:
            with exclude.open("a", encoding="utf-8") as f:
                f.write("\ncovenant.yaml\n")

        _git(repo, "checkout", "-q", "--force", "--detach", base0)
        t0 = time.time()
        result = compile_instruction(instruction, repo, HeuristicExtractor())
        out["compile_seconds"] = round(time.time() - t0, 2)
        cov_path = repo / "covenant.yaml"
        result.covenant.dump(cov_path)
        facts = result.facts
        out["facts"] = {"python_packages": [p.name for p in facts.python_packages], "ts_entries": facts.ts_entries[:10],
                        "source_language": facts.source_language, "test_commands": facts.test_commands[:5],
                        "data_paths": facts.data_paths[:10], "migration_dirs": facts.migration_dirs}
        out["clauses"] = [{"id": c.id, "text": c.source_span, "checker": c.checker, "status": c.status.value,
                           "scope": c.scope[:10], "params": _short(c.params, 400), "assumptions": c.assumptions}
                          for c in result.covenant.clauses]
        out["questions_defaulted"] = [q.text if hasattr(q, "text") else str(q) for q in result.defaulted]
        out["compile_issues"] = [str(i) for i in result.issues]

        for sha in reversed(shas):
            entry = {"sha": sha[:12]}
            try:
                _git(repo, "checkout", "-q", "--force", "--detach", sha)
                entry["subject"] = _git(repo, "log", "-1", "--format=%s", sha).strip()[:120]
                stat = _git(repo, "diff", "--shortstat", f"{sha}^", sha).strip()
                entry["stat"] = stat
                t0 = time.time()
                ci = run_ci(repo, cov_path, base=f"{sha}^", run_test_commands=False)
                entry["seconds"] = round(time.time() - t0, 2)
                entry["reports"] = [{"clause": r.violation, "checker": r.checker, "evidence": _short(r.evidence, 600)}
                                    for r in ci.reports]
            except Exception as exc:  # one bad commit shouldn't lose the repo
                entry["error"] = f"{type(exc).__name__}: {exc}"[:500]
                entry["trace"] = traceback.format_exc()[-1500:]
            out["commits"].append(entry)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:800]
        out["trace"] = traceback.format_exc()[-2000:]
    out["seconds"] = round(time.time() - started, 1)
    return out


def report(path: Path) -> str:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines = []
    commits = [c for r in rows for c in r["commits"]]
    judged = [c for c in commits if "error" not in c]
    flagged = [c for c in judged if c.get("reports")]
    lines.append(f"repos: {len(rows)}  (failed: {sum(1 for r in rows if r['error'])})")
    lines.append(f"commits judged: {len(judged)}  errors: {len(commits) - len(judged)}  flagged: {len(flagged)}")
    secs = sorted(c["seconds"] for c in judged if "seconds" in c)
    if secs:
        lines.append(f"seconds per commit: median {secs[len(secs) // 2]}  p95 {secs[int(len(secs) * .95)]}")
    by_checker = Counter(rep["checker"] for c in judged for rep in c.get("reports", []))
    lines.append("reports by checker: " + ", ".join(f"{k} {v}" for k, v in by_checker.most_common()))
    status = Counter((cl["checker"] or "none", cl["status"]) for r in rows for cl in r.get("clauses", []))
    lines.append("clauses (checker, status): " + ", ".join(f"{k[0]}/{k[1]} {v}" for k, v in sorted(status.items())))
    per_repo = defaultdict(int)
    for r in rows:
        per_repo[r["repo"]] = sum(1 for c in r["commits"] if c.get("reports"))
    lines.append("flagged commits per repo: " + ", ".join(f"{k} {v}" for k, v in sorted(per_repo.items(), key=lambda kv: -kv[1]) if v))
    for r in rows:
        if r["error"]:
            lines.append(f"FAILED {r['repo']}: {r['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("repos", nargs="?", help="file with one GitHub URL per line")
    p.add_argument("--work", default="replay-work", help="where clones go")
    p.add_argument("--out", default="replay-results.jsonl")
    p.add_argument("--commits", type=int, default=30)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--only", help="comma-separated repo names (owner/name) to run")
    p.add_argument("--report", help="summarize a results file and exit")
    args = p.parse_args(argv)
    if args.report:
        print(report(Path(args.report)))
        return 0
    urls = [u.strip() for u in Path(args.repos).read_text(encoding="utf-8").splitlines()
            if u.strip() and not u.startswith("#")]
    if args.only:
        wanted = set(args.only.split(","))
        urls = [u for u in urls if any(u.rstrip("/").endswith(w) for w in wanted)]
    work, out = Path(args.work).resolve(), Path(args.out)
    done = 0
    with ProcessPoolExecutor(max_workers=args.jobs) as pool, out.open("a", encoding="utf-8") as f:
        futures = {pool.submit(replay_repo, u, work, args.commits): u for u in urls}
        for fut in as_completed(futures):
            row = fut.result()
            f.write(json.dumps(row) + "\n")
            f.flush()
            done += 1
            flagged = sum(1 for c in row["commits"] if c.get("reports"))
            print(f"[{done}/{len(urls)}] {row['repo']}: {len(row['commits'])} commits, {flagged} flagged,"
                  f" {row['seconds']}s{'  ERROR ' + row['error'] if row['error'] else ''}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
