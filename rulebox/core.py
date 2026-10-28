"""Local persistence and a conservative Codex instruction-file adapter."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import os
import re
import sqlite3
import tempfile
import threading

START = "<!-- rulebox:start -->"
END = "<!-- rulebox:end -->"
MAX_FILE_BYTES = 256 * 1024


def text(value, name, limit=4000):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > limit or "\x00" in value:
        raise ValueError(f"{name} is too long or contains a null character")
    if START in value or END in value:
        raise ValueError("Rulebox markers cannot appear in a rule")
    return value


def draft_rule(value):
    """Formatting only: never invent conditions or claim model inference."""
    source = text(value, "Instruction")
    normalized = re.sub(r"[ \t]+", " ", source)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return {"source": source, "instruction": normalized,
            "title": normalized.splitlines()[0][:72],
            "note": "Local formatting only. Review wording and conditions before saving."}


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL,
                instruction TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL)""")
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def all(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM rules ORDER BY id")]

    def save(self, data, rule_id=None):
        values = [text(data.get("title"), "Title", 120),
                  text(data.get("source"), "Original instruction"),
                  text(data.get("instruction"), "Rule")]
        if type(data.get("enabled", True)) is not bool:
            raise ValueError("Enabled must be true or false")
        values += [int(data.get("enabled", True)), datetime.now(timezone.utc).isoformat()]
        with closing(self.connect()) as db, db:
            if rule_id is None:
                cursor = db.execute("INSERT INTO rules (title,source,instruction,enabled,updated_at) VALUES (?,?,?,?,?)", values)
                return cursor.lastrowid
            cursor = db.execute("UPDATE rules SET title=?,source=?,instruction=?,enabled=?,updated_at=? WHERE id=?", values + [rule_id])
            if cursor.rowcount != 1:
                raise ValueError("Rule not found")
            return rule_id

    def delete(self, rule_id):
        with closing(self.connect()) as db, db:
            if db.execute("DELETE FROM rules WHERE id=?", [rule_id]).rowcount != 1:
                raise ValueError("Rule not found")


class CodexAdapter:
    """Only touches the managed block of one explicitly selected project."""
    def __init__(self, project):
        self.project = Path(project).expanduser().resolve(strict=True)
        if not self.project.is_dir():
            raise ValueError("Project must be an existing directory")
        self.path = self.project / "AGENTS.md"
        self.lock = threading.Lock()

    def read(self):
        if self.path.is_symlink():
            raise ValueError("Refusing a symlinked AGENTS.md")
        if (self.project / "AGENTS.override.md").exists():
            raise ValueError("AGENTS.override.md takes precedence here. Resolve it before applying Rulebox rules.")
        if not self.path.exists():
            return b""
        if self.path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("AGENTS.md exceeds the 256 KiB edit limit")
        return self.path.read_bytes()

    def preview(self, rules):
        old = self.read()
        content = old.decode("utf-8")
        active = [r for r in rules if r["enabled"]]
        block = ""
        if active:
            lines = [START, "## Rulebox instructions", "", "User-reviewed project preferences. Apply within higher-priority instructions.", ""]
            for rule in active:
                # Indent continuation lines so rules remain distinct list entries.
                lines.append("- " + rule["instruction"].replace("\n", "\n  "))
            lines += [END]
            block = "\n".join(lines)
        if START in content or END in content:
            if content.count(START) != 1 or content.count(END) != 1:
                raise ValueError("Malformed or duplicate Rulebox markers; repair the file manually")
            a, b = content.index(START), content.index(END) + len(END)
            if a >= b - len(END):
                raise ValueError("Rulebox markers are out of order")
            result = content[:a] + block + content[b:]
        elif block:
            result = content + ("\n\n" if content and not content.endswith("\n\n") else "") + block + "\n"
        else:
            result = content
        if len(result.encode()) > MAX_FILE_BYTES:
            raise ValueError("Result exceeds the instruction-file edit limit")
        after = result.encode()
        # Bind confirmation to both the existing file and the exact rendered rules.
        revision = hashlib.sha256(old + b"\x00" + after).hexdigest()
        return {"path": str(self.path), "before": content, "after": result,
                "revision": revision, "active_count": len(active),
                "changed": old != after,
                "warning": "Start a new Codex session in this project after applying. Nested overrides and instruction-size limits can affect discovery."}

    def apply(self, rules, revision):
        with self.lock:
            proposal = self.preview(rules)
            if not isinstance(revision, str) or revision != proposal["revision"]:
                raise ValueError("Rules or AGENTS.md changed. Preview again before applying.")
            if not proposal["changed"]:
                return {"path": str(self.path), "backup": None, "changed": False}
            old = self.read()
            backup = None
            if self.path.exists():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = self.project / f".rulebox-backup-{stamp}.md"
                with backup.open("xb") as out:
                    os.chmod(backup, 0o600)
                    out.write(old)
            fd, temp = tempfile.mkstemp(prefix=".rulebox-", dir=self.project)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(proposal["after"].encode())
                    out.flush()
                    os.fsync(out.fileno())
                # Catch ordinary editor changes during backup/write preparation.
                if self.read() != old:
                    raise ValueError("AGENTS.md changed during apply; preview again")
                os.replace(temp, self.path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            return {"path": str(self.path), "backup": str(backup) if backup else None,
                    "changed": True}
