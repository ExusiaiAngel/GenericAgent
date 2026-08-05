"""Approval-gated, transport-neutral Markdown Skill lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time

from session_store import ConversationIdentity


_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
_SECRET_RE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{16,}|"
    r"\b(?:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*\S{8,})",
    re.IGNORECASE,
)


class SkillManager:
    def __init__(self, proposal_root, install_root, *, ttl_seconds=86400):
        self.proposal_root = Path(proposal_root).resolve()
        self.install_root = Path(install_root).resolve()
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.proposal_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.install_root.mkdir(parents=True, exist_ok=True, mode=0o750)

    @staticmethod
    def _validate_slug(slug):
        slug = str(slug or "").strip().lower()
        if not _SLUG_RE.fullmatch(slug):
            raise ValueError("skill name must be a safe lowercase slug")
        return slug

    @staticmethod
    def _validate_content(content):
        content = str(content or "").strip()
        if not content.startswith("#"):
            raise ValueError("Skill content must be Markdown beginning with a heading")
        if len(content.encode("utf-8")) > 16384:
            raise ValueError("Skill exceeds 16384-byte budget")
        if _SECRET_RE.search(content):
            raise ValueError("secret-like content detected in Skill")
        return content + "\n"

    def _proposal_dir(self, proposal_id):
        if not re.fullmatch(r"[a-f0-9]{24}", str(proposal_id or "")):
            raise ValueError("invalid proposal id")
        path = (self.proposal_root / proposal_id).resolve()
        if path.parent != self.proposal_root:
            raise ValueError("proposal path escapes root")
        return path

    def propose(self, slug, content, reason, identity: ConversationIdentity):
        slug = self._validate_slug(slug)
        content = self._validate_content(content)
        proposal_id = secrets.token_hex(12)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        path = self._proposal_dir(proposal_id)
        path.mkdir(mode=0o750)
        meta = {
            "id": proposal_id, "slug": slug, "reason": str(reason or "")[:1000],
            "sha256": digest, "status": "pending", "created_at": time.time(),
            "expires_at": time.time() + self.ttl_seconds,
            "identity_key": identity.key, "actor": identity.actor,
        }
        self._atomic_text(path / "SKILL.md", content, 0o640)
        self._atomic_text(path / "proposal.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n", 0o640)
        print(f"[SKILL-AUDIT] action=propose id={proposal_id} slug={slug}")
        return dict(meta)

    @staticmethod
    def _atomic_text(path: Path, content: str, mode: int):
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(content); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temp_name, mode)
            os.replace(temp_name, path)
            temp_name = None
        finally:
            if temp_name:
                try: os.unlink(temp_name)
                except FileNotFoundError: pass

    def get(self, proposal_id):
        path = self._proposal_dir(proposal_id)
        meta_path = path / "proposal.json"
        skill_path = path / "SKILL.md"
        if not meta_path.is_file() or meta_path.is_symlink() or not skill_path.is_file() or skill_path.is_symlink():
            raise FileNotFoundError("proposal not found")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        content = skill_path.read_text(encoding="utf-8")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != meta.get("sha256"):
            raise ValueError("proposal hash mismatch")
        return meta, content

    def list_pending(self):
        results = []
        for path in sorted(self.proposal_root.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                meta, _ = self.get(path.name)
                if meta.get("status") == "pending":
                    results.append(meta)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return results

    def approve(self, proposal_id, identity: ConversationIdentity):
        meta, content = self.get(proposal_id)
        if meta.get("status") != "pending":
            raise ValueError("proposal is not pending")
        if time.time() > float(meta.get("expires_at", 0)):
            raise ValueError("proposal expired")
        if meta.get("identity_key") != identity.key or not identity.actor or meta.get("actor") != identity.actor:
            raise PermissionError("approval identity does not match proposal owner")
        slug = self._validate_slug(meta.get("slug"))
        content = self._validate_content(content)
        target = (self.install_root / slug).resolve()
        if target.parent != self.install_root or target.exists():
            raise ValueError("active Skill already exists or path is invalid")
        staged = Path(tempfile.mkdtemp(prefix=f".{slug}.", dir=self.install_root))
        try:
            os.chmod(staged, 0o750)
            self._atomic_text(staged / "SKILL.md", content, 0o640)
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != meta["sha256"]:
                raise ValueError("candidate changed during approval")
            os.replace(staged, target)
            staged = None
        finally:
            if staged and staged.exists():
                shutil.rmtree(staged)
        meta["status"] = "approved"; meta["approved_at"] = time.time()
        self._atomic_text(self._proposal_dir(proposal_id) / "proposal.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n", 0o640)
        print(f"[SKILL-AUDIT] action=approve id={proposal_id} slug={slug}")
        return {"id": proposal_id, "slug": slug, "sha256": meta["sha256"], "status": "approved"}

    def reject(self, proposal_id, identity: ConversationIdentity):
        meta, _ = self.get(proposal_id)
        if meta.get("status") != "pending":
            raise ValueError("proposal is not pending")
        if meta.get("identity_key") != identity.key or not identity.actor or meta.get("actor") != identity.actor:
            raise PermissionError("rejection identity does not match proposal owner")
        meta["status"] = "rejected"; meta["rejected_at"] = time.time()
        self._atomic_text(self._proposal_dir(proposal_id) / "proposal.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n", 0o640)
        print(f"[SKILL-AUDIT] action=reject id={proposal_id} slug={meta.get('slug','')}")
        return {"id": proposal_id, "status": "rejected"}


def render_skill_catalog(install_root) -> str:
    root = Path(install_root)
    if not root.is_dir():
        return ""
    lines = []
    for path in sorted(root.iterdir()):
        skill = path / "SKILL.md"
        if not path.is_dir() or path.is_symlink() or not skill.is_file() or skill.is_symlink():
            continue
        first = next((line.lstrip("# ").strip() for line in skill.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("#")), path.name)
        lines.append(f"- {path.name}: {first[:120]}")
    return "\n".join(lines)
