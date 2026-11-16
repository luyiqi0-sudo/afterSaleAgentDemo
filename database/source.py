"""Verify a deployed SQL seed against the selected dataset's frozen manifest."""
import hashlib
import json
from pathlib import Path


class DatabaseSourceMismatch(ValueError):
    pass


def verify_database_source(dataset_root: Path, database_path: Path,
                           dictionary_path: Path | None) -> Path:
    """Allow relocation while requiring the same schema, dictionary and seed bytes."""
    try:
        manifest = json.loads((Path(dataset_root) / "manifest.json").read_text(encoding="utf-8"))
        database = Path(database_path)
        directory = database.parent
        dictionary = Path(dictionary_path) if dictionary_path is not None else directory / "dictionary.json"
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError
        sources = {
            "database/ecommerce.db": database,
            "database/dictionary.json": dictionary,
            "database/schema.sql": directory / "schema.sql",
        }
        for name, path in sources.items():
            if (not path.is_file() or path.is_symlink()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]):
                raise ValueError
        # SQLite can read a WAL even through a read-only connection. Only the
        # manifest-bound database file is part of this immutable seed delivery.
        if any(Path(str(database) + suffix).exists() or Path(str(database) + suffix).is_symlink()
               for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError
    except (OSError, KeyError, AttributeError, TypeError, ValueError):
        raise DatabaseSourceMismatch("DATABASE_SNAPSHOT_MISMATCH") from None
    return dictionary
