"""Dry-run or apply a manifest of proven redundant files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROTECTED_PARTS = {".env", "data", "logs"}
PROTECTED_SUFFIXES = {".db", ".db-wal", ".db-shm", ".key", ".pem", ".log"}


def _is_protected(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    if parts & PROTECTED_PARTS:
        return True
    lower_name = path.name.lower()
    return any(lower_name.endswith(suffix) for suffix in PROTECTED_SUFFIXES)


def evaluate_manifest(manifest_path: str | Path, *, apply: bool = False) -> dict:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    root = Path(manifest.get("root", ".")).resolve()
    results = []
    for item in manifest.get("items", []):
        raw = str(item.get("path") or "")
        action = str(item.get("action") or "delete")
        proof = item.get("proof") or {}
        target = (root / raw).resolve()
        protected = _is_protected(target)
        exists = target.exists()
        unused = bool(proof.get("unused"))
        status = "missing"
        try:
            target.relative_to(root)
            inside_root = True
        except ValueError:
            inside_root = False
        if exists:
            if not inside_root:
                status = "refused_outside_root"
            elif protected:
                status = "refused_protected"
            elif action != "delete":
                status = "refused_unknown_action"
            elif not unused:
                status = "refused_unproven"
            elif apply:
                target.unlink()
                status = "deleted"
            else:
                status = "would_delete"
        results.append({
            "path": raw,
            "resolved": str(target),
            "exists": exists,
            "protected": protected,
            "unused": unused,
            "action": action,
            "status": status,
        })
    return {
        "manifest": str(manifest_path),
        "apply": apply,
        "results": results,
        "ok": all(r["status"] in {"missing", "would_delete", "deleted"} for r in results),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = evaluate_manifest(args.manifest, apply=args.apply)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Cleanup manifest: {'OK' if result['ok'] else 'REFUSED'}")
        for row in result["results"]:
            print(f"- {row['status']}: {row['path']}")
    from hightempbot.cli._exitcodes import OK, NOGO

    return OK if result["ok"] else NOGO


if __name__ == "__main__":
    raise SystemExit(main())
