"""Crawl Responsa's Browse ("עיון") sources tree into JSON cache files.

Run from the repo root with Responsa running (or launchable), nothing else driving
it meanwhile:

    python scripts/build_browse_cache.py [--out DIR] [--only N ...] [--limit-depth D]

One file per top-level category (`NN.json.gz`) is written into the cache
directory as soon as that category is finished, so an interrupted run
resumes where it stopped: categories whose file exists are skipped (delete
a file to re-crawl it). `--only 3 5` restricts to those root indexes.

Node format in the files: a leaf is its text (a string); a node with
children is `[text, [child, child, ...]]`. Siblings keep their tree order
and are never merged, so duplicate sibling names survive.
"""
import argparse
import ctypes
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from responsa_api.internal import browse_gui  # noqa: E402
from responsa_api.internal.automation import ResponsaAutomation  # noqa: E402
from responsa_api.internal.browse_tree import DEFAULT_CACHE_DIR  # noqa: E402


class Crawler:
    def __init__(self, reader, log):
        self.reader = reader
        self.log = log
        self.count = 0
        self.max_depth = 0
        self.last_report = time.time()

    def crawl(self, hitem, depth=1):
        text, has_children = self.reader.read(hitem)
        self.count += 1
        self.max_depth = max(self.max_depth, depth)
        if time.time() - self.last_report > 30:
            self.log(f"    ... {self.count} nodes so far, at depth {depth}: {text[:40]!r}")
            self.last_report = time.time()
        if not has_children:
            return text
        self.reader.expand(hitem)
        kids = [self.crawl(c, depth + 1) for c in self.reader.children(hitem)]
        self.reader.collapse_reset(hitem)
        return [text, kids]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_CACHE_DIR)
    ap.add_argument("--only", type=int, nargs="*")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    def log(msg):
        print(msg, flush=True)

    # Keep the machine and its display awake for the (hours-long) run --
    # only for as long as this process lives; no system setting changes.
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)

    auto = ResponsaAutomation().start()
    _, tree_hwnd = browse_gui.open_browse_tree(auto)
    reader = browse_gui.TreeReader(tree_hwnd)
    roots = reader.roots()
    reader.set_redraw(False)
    log(f"{len(roots)} top-level categories")

    try:
        t_all = time.time()
        total = 0
        for i, root in enumerate(roots):
            path = os.path.join(args.out, f"{i:02d}.json.gz")
            if (args.only is not None and i not in args.only) or os.path.exists(path):
                continue
            name, _ = reader.read(root)
            log(f"[{i:02d}] {name}")
            t0 = time.time()
            c = Crawler(reader, log)
            node = c.crawl(root)
            tmp = path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as f:
                json.dump(node, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
            total += c.count
            log(f"[{i:02d}] done: {c.count} nodes, max depth {c.max_depth}, "
                f"{time.time() - t0:.0f}s, {os.path.getsize(path) / 1024:.0f} KiB")
    finally:
        reader.close()
    log(f"finished: {total} nodes crawled this run in {time.time() - t_all:.0f}s")


if __name__ == "__main__":
    main()
