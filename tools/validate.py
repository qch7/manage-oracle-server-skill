#!/usr/bin/env python3
"""Offline publication checks. Never read user configuration or credentials."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "manage-oracle-server"
REQUIRED = (
    "README.md", "LICENSE", "CONTRIBUTING.md", "SECURITY.md", ".gitignore",
    ".gitattributes", "requirements-dev.txt", ".github/workflows/validate.yml",
    "skill/manage-oracle-server/SKILL.md", "skill/manage-oracle-server/LICENSE",
    "skill/manage-oracle-server/agents/openai.yaml",
    "skill/manage-oracle-server/requirements.txt",
)
SECRET_PATTERNS = {
    "private-key body": re.compile(
        r"-----BEGIN (?:OPENSSH |RSA |EC |ENCRYPTED )?PRIVATE KEY-----\s*\n[A-Za-z0-9+/=]{16,}"
    ),
    "personal home path": re.compile(
        r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/(?:home|Users)/)(?![<$])[A-Za-z0-9_. -]+[\\/]"
    ),
    "long OCI identifier": re.compile(
        r"\bocid1\.[a-z0-9_]+\.[a-z0-9]+\.[a-z0-9-]*\.[A-Za-z0-9]{40,}\b"
    ),
    "GitHub credential": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"
    ),
}
PRIVATE_PARTS = {".ssh", ".oci", "__pycache__", "state", "local", "venv"}
PRIVATE_NAMES = {"profiles.json", "runtimes.json", "known_hosts", "id_rsa", "id_ecdsa", "id_ed25519"}
PRIVATE_SUFFIXES = (".pem", ".key", ".ppk", ".pyc", ".pyo", ".zip", ".log", ".lock", ".plan.json", ".journal.json")


def check_text(text):
    """Return pattern labels only; never echo matching secret material."""
    return [label for label, pattern in SECRET_PATTERNS.items() if pattern.search(text)]


def check_index_paths(names):
    errors = []
    for name in names:
        path = Path(name)
        if (any(part in PRIVATE_PARTS or part.startswith(".venv") for part in path.parts)
                or path.name in PRIVATE_NAMES or path.name.startswith(".env")
                or path.name.endswith(PRIVATE_SUFFIXES)):
            errors.append(f"Private/generated file in Git index: {name}")
    return errors


def check_markdown(path, text, root):
    errors = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        destination = (path.parent / unquote(parsed.path)).resolve()
        if not destination.is_relative_to(root) or not destination.exists():
            errors.append("Broken or external local Markdown reference")
    for block in re.findall(r"```json\s*\n(.*?)\n```", text, re.S):
        try:
            json.loads(block)
        except json.JSONDecodeError:
            errors.append("Invalid JSON example")
    return errors


def index_files():
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True, check=False,
    )
    if result.returncode:
        return []
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def main():
    errors = [f"Missing required file: {name}" for name in REQUIRED if not (ROOT / name).is_file()]
    tracked = index_files()
    errors.extend(check_index_paths(tracked))
    paths = {ROOT / name for name in REQUIRED if (ROOT / name).is_file()}
    paths.update(ROOT / name for name in tracked if (ROOT / name).is_file())
    for directory in (SKILL, ROOT / "tools", ROOT / "tests", ROOT / ".github"):
        paths.update(p for p in directory.rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo"))

    for path in sorted(paths):
        name = path.relative_to(ROOT).as_posix()
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            errors.append(f"{name}: publication sources must be ordinary local files")
            continue
        try:
            text = path.read_text(encoding="utf-8")
            errors.extend(f"{name}: possible {label}" for label in check_text(text))
            if path.suffix == ".py":
                ast.parse(text, filename=name, feature_version=(3, 10))
            elif path.suffix == ".json":
                json.loads(text)
            elif path.suffix in (".yml", ".yaml"):
                yaml.safe_load(text)
            elif path.suffix == ".md":
                errors.extend(f"{name}: {error}" for error in check_markdown(path, text, ROOT))
        except (OSError, UnicodeError, SyntaxError, ValueError, yaml.YAMLError):
            errors.append(f"{name}: invalid encoding, syntax, or data format")

    try:
        skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"\A---\n(.*?)\n---(?:\n|$)", skill_text, re.S)
        metadata = yaml.safe_load(match.group(1)) if match else None
        if not isinstance(metadata, dict) or metadata.get("name") != "manage-oracle-server":
            errors.append("Skill name/frontmatter is invalid")
        elif not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
            errors.append("Skill description is missing")
        elif metadata.get("license") != "MIT":
            errors.append("Skill license metadata must match LICENSE")
        ui = yaml.safe_load((SKILL / "agents/openai.yaml").read_text(encoding="utf-8"))
        interface = ui["interface"]
        if not 25 <= len(interface["short_description"]) <= 64:
            errors.append("UI short_description must contain 25-64 characters")
        if "$manage-oracle-server" not in interface["default_prompt"]:
            errors.append("UI default_prompt must name the skill")
        for field in ("icon_small", "icon_large"):
            icon = (SKILL / interface[field]).resolve()
            if not icon.is_relative_to(SKILL) or not icon.is_file():
                errors.append(f"Missing or invalid UI {field}")
        if (ROOT / "LICENSE").read_bytes() != (SKILL / "LICENSE").read_bytes():
            errors.append("Installed skill must carry the repository license notice")
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, yaml.YAMLError):
        errors.append("Could not validate package metadata/license")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Validated {len(paths)} publication files: metadata, links, JSON, Python 3.10 syntax, and common secret patterns.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
