from __future__ import annotations

import json

from hightempbot.cli.cleanup_manifest import evaluate_manifest


def test_cleanup_manifest_refuses_paths_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({
            "root": str(root),
            "items": [{
                "path": "../outside.txt",
                "action": "delete",
                "proof": {"unused": True},
            }],
        }),
        encoding="utf-8",
    )

    result = evaluate_manifest(manifest, apply=True)

    assert result["results"][0]["status"] == "refused_outside_root"
    assert outside.exists()
