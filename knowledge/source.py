"""Bind a deployed policy directory to the selected dataset's reviewed file hashes."""
import hashlib
import json
from pathlib import Path, PurePosixPath


class KnowledgeSourceMismatch(ValueError):
    pass


def verify_knowledge_source(dataset_root: Path, knowledge_path: Path) -> None:
    """Allow a relocated corpus only when every policy matches the dataset manifest."""
    try:
        manifest = json.loads((Path(dataset_root) / "manifest.json").read_text(encoding="utf-8"))
        expected = {
            name.removeprefix("knowledge/"): digest
            for name, digest in manifest["files"].items() if name.startswith("knowledge/")
        }
        if (not expected or len(expected) != manifest["counts"]["documents"]
                or any(PurePosixPath(name).name != name or not name.endswith(".md") for name in expected)):
            raise ValueError
        source = Path(knowledge_path)
        if not source.is_dir() or source.is_symlink():
            raise ValueError
        actual = {path.name: path for path in source.glob("*.md")}
        if actual.keys() != expected.keys():
            raise ValueError
        for name, path in actual.items():
            if (not path.is_file() or path.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != expected[name]):
                raise ValueError
    except (OSError, KeyError, AttributeError, TypeError, ValueError):
        raise KnowledgeSourceMismatch("KNOWLEDGE_SNAPSHOT_MISMATCH") from None
