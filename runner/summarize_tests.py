#!/usr/bin/env python3
"""Fold Maven surefire/failsafe XML reports into a short per-module test summary.

Usage: summarize_tests.py <project root> [--gate "<verdict line>"] [--command "<command used>"]

Walks the tree for target/surefire-reports/TEST-*.xml and target/failsafe-reports/TEST-*.xml,
sums tests/failures/errors/skipped per module (the directory above target/), prints a table,
totals, then the gate verdict and the command — the caller tells it both, the reports alone
do not know whether the gate was meant to be full.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

KINDS = ("surefire", "failsafe")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--gate", default="")
    ap.add_argument("--command", default="")
    a = ap.parse_args()

    per: dict[str, dict[str, list[int]]] = defaultdict(lambda: {k: [0, 0, 0, 0] for k in KINDS})
    seen = {k: 0 for k in KINDS}
    for kind in KINDS:
        for xml in a.root.rglob(f"target/{kind}-reports/TEST-*.xml"):
            try:
                suite = ET.parse(xml).getroot()
            except ET.ParseError as e:
                print(f"# unreadable report {xml}: {e}", file=sys.stderr)
                continue
            module = xml.relative_to(a.root).parts[0] if len(xml.relative_to(a.root).parts) > 3 else "."
            row = per[module][kind]
            for i, attr in enumerate(("tests", "failures", "errors", "skipped")):
                row[i] += int(suite.get(attr, "0") or 0)
            seen[kind] += 1

    print("Тесты по модулям (всего/отказов/ошибок/пропущено)")
    print(f"{'модуль':<28}{'surefire':<22}{'failsafe':<22}")
    total = {k: [0, 0, 0, 0] for k in KINDS}
    for module in sorted(per):
        cells = []
        for kind in KINDS:
            r = per[module][kind]
            for i in range(4):
                total[kind][i] += r[i]
            cells.append("/".join(map(str, r)) if sum(r) else "—")
        print(f"{module:<28}{cells[0]:<22}{cells[1]:<22}")
    print(f"{'ИТОГО':<28}{'/'.join(map(str, total['surefire'])):<22}{'/'.join(map(str, total['failsafe'])):<22}")
    for kind in KINDS:
        if seen[kind] == 0:
            print(f"{kind}: отчётов нет — эти тесты не выполнялись")
    red = sum(total[k][1] + total[k][2] for k in KINDS)
    print(f"Красных: {red}")
    if a.gate:
        print(f"Итог гейта: {a.gate}")
    if a.command:
        print(f"Команда: {a.command}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
