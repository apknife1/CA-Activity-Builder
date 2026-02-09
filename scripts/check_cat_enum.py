from __future__ import annotations

import ast
from pathlib import Path


def _load_cat_members(inst_path: Path) -> set[str]:
    src = inst_path.read_text(encoding="utf-8")
    mod = ast.parse(src)
    for node in mod.body:
        if isinstance(node, ast.ClassDef) and node.name == "Cat":
            members: set[str] = set()
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                    if isinstance(target, ast.Name):
                        members.add(target.id)
            return members
    return set()


def _find_cat_usage(root: Path) -> dict[str, list[tuple[Path, int]]]:
    used: dict[str, list[tuple[Path, int]]] = {}
    for path in root.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        mod = ast.parse(src)
        for node in ast.walk(mod):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "Cat":
                    used.setdefault(node.attr, []).append((path, node.lineno))
    return used


def main() -> int:
    root = Path("src/ca_bldr")
    inst_path = root / "instrumentation.py"

    if not inst_path.exists():
        print("ERROR: instrumentation.py not found at src/ca_bldr/instrumentation.py")
        return 2

    cat_members = _load_cat_members(inst_path)
    used = _find_cat_usage(root)
    missing = sorted([name for name in used if name not in cat_members])

    if not missing:
        print("OK: All Cat.* references are present in the Cat enum.")
        return 0

    print("ERROR: Missing Cat enum entries:")
    for name in missing:
        locs = used.get(name, [])
        for path, line in locs:
            print(f"  {name}: {path}:{line}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
