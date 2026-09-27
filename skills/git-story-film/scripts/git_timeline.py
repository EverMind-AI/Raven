#!/usr/bin/env python3
"""Extract the facts a git-story film is built from, as one JSON file.

Usage: python3 git_timeline.py <repo> [--out timeline.json] [--branch HEAD]

Collects: first/last day, daily commit counts (the beads), running totals,
the busiest days, tags, feat/perf commits with PR numbers (candidate beats),
reverts (candidate "falls"), and a de-duplicated author count. Everything is
read-only; nothing in the repo is changed.
"""

import argparse
import collections
import json
import re
import shutil
import subprocess
import sys


def git(repo, *args):
    executable = shutil.which("git")
    if executable is None:
        raise FileNotFoundError("git executable not found on PATH")
    return subprocess.run([executable, "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--branch", default="HEAD")
    ap.add_argument("--out", default="timeline.json")
    a = ap.parse_args()

    log = git(a.repo, "log", a.branch, "--date=short", "--format=%h%x1f%ad%x1f%an%x1f%s")
    rows = [line.split("\x1f") for line in log.strip().split("\n") if line]
    if not rows:
        sys.exit("no commits found")
    rows.reverse()

    daily = collections.Counter(r[1] for r in rows)
    days, total = [], 0
    for d in sorted(daily):
        total += daily[d]
        days.append({"date": d, "commits": daily[d], "total": total})

    pr = re.compile(r"\(#(\d+)\)")
    beats = []
    for h, d, _, s in rows:
        m = re.match(r"(feat|perf)(\([^)]*\))?!?:", s)
        if m:
            p = pr.search(s)
            beats.append(
                {
                    "date": d,
                    "hash": h,
                    "type": m.group(1),
                    "scope": (m.group(2) or "").strip("()"),
                    "subject": s,
                    "pr": int(p.group(1)) if p else None,
                }
            )
    reverts = [{"date": d, "hash": h, "subject": s} for h, d, _, s in rows if s.lower().startswith("revert")]

    # Bots are dropped; the same person often appears under several names, so this is an upper bound.
    authors = sorted({r[2] for r in rows if not re.search(r"bot\b|\[bot\]", r[2], re.I)})
    try:
        tags = [
            t
            for t in git(a.repo, "tag", "--sort=creatordate", "--format=%(refname:short)\x1f%(creatordate:short)")
            .strip()
            .split("\n")
            if t
        ]
        tags = [dict(zip(("tag", "date"), t.split("\x1f"))) for t in tags]
    except subprocess.CalledProcessError:
        tags = []

    out = {
        "repo": a.repo,
        "first_day": days[0]["date"],
        "last_day": days[-1]["date"],
        "total_commits": total,
        "active_days": len(days),
        "busiest_days": sorted(days, key=lambda d: -d["commits"])[:5],
        "author_names_upper_bound": len(authors),
        "days": days,
        "tags": tags,
        "first_commit": {"hash": rows[0][0], "date": rows[0][1], "subject": rows[0][3]},
        "feature_commits": beats,
        "reverts": reverts,
    }
    with open(a.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(
        f"{a.out}: {total} commits, {len(days)} active days, {out['first_day']} -> {out['last_day']}, "
        f"{len(beats)} feat/perf commits, {len(reverts)} reverts, busiest {out['busiest_days'][0]['date']} ({out['busiest_days'][0]['commits']})"
    )


if __name__ == "__main__":
    main()
