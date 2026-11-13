#!/usr/bin/env python3
"""Check a source-only publication tree locally; private vocabulary never enters the report.

This is a content gate, not publication approval. Secret scanning and human review
are separate required checks. No network access, remote writes or telemetry.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

BLOCKED_SUFFIXES = {".docx", ".doc", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".eml", ".har", ".zip", ".gz", ".sqlite3", ".db", ".log", ".pem", ".key", ".p12", ".tfstate"}
BLOCKED_DIRS = {".git", ".aws", ".codex", ".agents", ".venv", "node_modules", "test-results", "playwright-report", ".scannerwork", "__pycache__"}


def normalized(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def blocked_path(path):
    parts = path.parts
    name = path.name.casefold()
    return (any(p in BLOCKED_DIRS for p in parts)
            or name == "agents.md" or (name.startswith(".env") and name != ".env.example")
            or path.suffix.casefold() in BLOCKED_SUFFIXES
            or str(path).startswith(("local/private/", "local/review/", "e2e/report/"))
            or (str(path).startswith("local/fixtures/") and str(path) != "local/fixtures/README.md"))


def has_private_locator(line):
    for parent, user in re.findall(r"/(Users|home)/([\w.-]+)/", line):
        # Documented service-container paths contain no workstation identity.
        if parent == "home" and user in {"node", "StepFunctionsLocal"}:
            continue
        return True
    if re.search(r"[A-Z]:\\Users\\[^\\]+\\", line):
        return True
    accounts = re.findall(r"arn:(?:aws|aws-us-gov|aws-cn):[^\s:]*:[^\s:]*:(\d{12}):", line)
    # The all-zero account is the existing local emulator identity, not a cloud account.
    return any(account != "000000000000" for account in accounts)


def audit_tree(root, private_terms=()):
    """Hash every regular text file; flag private terms without echoing their values."""
    root = Path(root).resolve()
    terms = [normalized(term) for term in private_terms if isinstance(term, str) and term.strip()]
    manifest, findings = [], []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        private_path = False
        for index, term in enumerate(terms):
            if re.search(r"(?<!\w)" + re.escape(term.replace("_", " ")) + r"(?!\w)", normalized(str(relative)).replace("_", " ")):
                findings.append({"path_sha256": hashlib.sha256(str(relative).encode()).hexdigest(),
                                 "kind": "private_term_in_path", "term_id": index})
                private_path = True
        if private_path:
            continue
        if path.is_symlink():
            findings.append({"path": str(relative), "kind": "symlink"})
            continue
        if not path.is_file():
            continue
        if blocked_path(relative):
            findings.append({"path": str(relative), "kind": "excluded_artifact"})
            continue
        data = path.read_bytes()
        try:
            content = data.decode("utf-8")
            if "\x00" in content:
                raise UnicodeError("NUL in text")
        except UnicodeError:
            findings.append({"path": str(relative), "kind": "non_text_artifact"})
            continue
        manifest.append({"path": str(relative), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                         "executable": bool(path.stat().st_mode & 0o111)})
        for number, line in enumerate(content.splitlines(), 1):
            value = normalized(line)
            for index, term in enumerate(terms):
                if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", value):
                    findings.append({"path": str(relative), "line": number, "kind": "private_term", "term_id": index})
            # Personal machine paths and cloud account identities require explicit removal/review.
            if has_private_locator(line):
                findings.append({"path": str(relative), "line": number, "kind": "private_locator"})
    return {"format": 1, "files": manifest, "findings": findings, "private_term_count": len(terms)}


def verify_manifest(root, document):
    if document.get("format") != 1 or document.get("findings"):
        return False
    current = audit_tree(root)
    return not current["findings"] and current["files"] == document["files"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tree", type=Path)
    parser.add_argument("--private-terms", type=Path, help="Private JSON array, stored outside the publication tree")
    parser.add_argument("--manifest", type=Path, required=True, help="Write outside the publication tree")
    parser.add_argument("--verify", action="store_true", help="Verify an existing manifest; do not regenerate it")
    args = parser.parse_args(argv)
    if args.manifest.resolve().is_relative_to(args.tree.resolve()):
        parser.error("Keep audit metadata outside the publication tree.")
    if args.private_terms and args.private_terms.resolve().is_relative_to(args.tree.resolve()):
        parser.error("Keep private vocabulary outside the publication tree.")
    if args.verify:
        ok = verify_manifest(args.tree, json.loads(args.manifest.read_text()))
        print("Publication snapshot unchanged." if ok else "Publication snapshot changed or contains excluded artifacts.")
        return 0 if ok else 1
    terms = json.loads(args.private_terms.read_text()) if args.private_terms else []
    if not isinstance(terms, list) or any(not isinstance(t, str) for t in terms):
        parser.error("Private vocabulary must be a JSON array of strings.")
    result = audit_tree(args.tree, terms)
    args.manifest.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Reviewed {len(result['files'])} source files against {len(terms)} private terms; {len(result['findings'])} findings.")
    print("Manual content/history review and local secret scanning are still required. Nothing has been published.")
    return 1 if result["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
