"""Exact, approval-gated filesystem changes for remote chat transports."""

from __future__ import annotations

import argparse
from array import array
import ast
from bisect import bisect_left
from contextlib import contextmanager
import difflib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time


DEFAULT_CHANGE_STATE_ROOT = "/var/lib/genericagent/change_approval"
_STATE_LOCKS = {}
_STATE_LOCKS_GUARD = threading.Lock()
_NO_JSON_DEFAULT = object()


def get_change_state_root():
    """Return the server-private approval state root used by every entrypoint."""
    configured = os.environ.get("GENERICAGENT_CHANGE_STATE_ROOT", "").strip()
    return Path(configured or DEFAULT_CHANGE_STATE_ROOT).expanduser().resolve()


class ChangeApprovalManager:
    MAX_FILE_BYTES = 1024 * 1024
    PROPOSAL_TTL_SECONDS = 900

    def __init__(self, state_root, change_roots, *, clock=time.time):
        self.state_root = Path(state_root).resolve()
        self.change_roots = tuple(Path(root).resolve() for root in change_roots)
        self.clock = clock
        self.auth_root = self.state_root / "auth"
        self.proposal_root = self.state_root / "proposals"
        self.backup_root = self.state_root / "backups"
        for path in (self.state_root, self.auth_root, self.proposal_root, self.backup_root):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._chmod(path, 0o700)
        self.binding_path = self.auth_root / "binding.json"
        self.approvers_path = self.auth_root / "approvers.json"
        self.lock_path = self.state_root / "change_approval.lock"
        self.integrity_key_path = self.state_root / "integrity.key"

    @contextmanager
    def _exclusive_lock(self):
        lock_key = str(self.lock_path)
        with _STATE_LOCKS_GUARD:
            process_lock = _STATE_LOCKS.setdefault(lock_key, threading.RLock())
        with process_lock:
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            locked = False
            try:
                self._chmod(self.lock_path, 0o600)
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
                yield
            finally:
                if locked:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _chmod(path, mode):
        os.chmod(path, mode)
        if os.name != "nt":
            metadata = os.stat(path, follow_symlinks=False)
            actual_mode = stat.S_IMODE(metadata.st_mode)
            expected_mode = int(mode) & 0o7777
            if actual_mode != expected_mode:
                raise PermissionError(
                    f"private path mode verification failed for {path}: "
                    f"expected {expected_mode:o}, got {actual_mode:o}"
                )
            get_effective_uid = getattr(os, "geteuid", None)
            if get_effective_uid is not None and metadata.st_uid != get_effective_uid():
                raise PermissionError(
                    f"private path owner verification failed for {path}"
                )

    @staticmethod
    def _atomic_json(path: Path, value: dict, mode=0o600):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temp_name = handle.name
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            ChangeApprovalManager._chmod(temp_name, mode)
            os.replace(temp_name, path)
            temp_name = None
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes, mode: int):
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb", dir=path.parent, prefix=f".{path.name}.",
                suffix=".tmp", delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            ChangeApprovalManager._chmod(temp_name, mode)
            os.replace(temp_name, path)
            temp_name = None
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _read_json(path: Path, default=_NO_JSON_DEFAULT):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if default is _NO_JSON_DEFAULT:
                raise
            return default

    @staticmethod
    def _canonical_record(record):
        unsigned = {
            key: value for key, value in record.items()
            if key != "integrity_hmac"
        }
        return json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")

    def _integrity_key_locked(self, *, create=False):
        if self.integrity_key_path.exists():
            if self.integrity_key_path.is_symlink() or not self.integrity_key_path.is_file():
                raise ValueError("proposal integrity key is not a trusted regular file")
            key = self.integrity_key_path.read_bytes()
            if len(key) != 32:
                raise ValueError("proposal integrity key is invalid")
            self._chmod(self.integrity_key_path, 0o600)
            return key
        if not create:
            raise ValueError("proposal integrity key is unavailable")
        key = secrets.token_bytes(32)
        self._atomic_bytes(self.integrity_key_path, key, 0o600)
        return key

    def _write_proposal_locked(self, path, record):
        record.pop("integrity_hmac", None)
        record["integrity_hmac"] = hmac.new(
            self._integrity_key_locked(create=True),
            self._canonical_record(record),
            hashlib.sha256,
        ).hexdigest()
        self._atomic_json(path, record)

    def _verify_proposal_locked(self, record):
        signature = str(record.get("integrity_hmac", ""))
        expected = hmac.new(
            self._integrity_key_locked(create=False),
            self._canonical_record(record),
            hashlib.sha256,
        ).hexdigest()
        if not signature or not secrets.compare_digest(signature, expected):
            raise ValueError("source change proposal integrity verification failed")

    @staticmethod
    def _identity_record(identity):
        return {
            "platform": str(identity.platform),
            "account": str(identity.account),
            "conversation": str(identity.conversation),
            "actor": str(identity.actor),
        }

    @classmethod
    def _identity_matches(cls, record, identity):
        return record == cls._identity_record(identity)

    def issue_binding_code(self, ttl_seconds=600):
        with self._exclusive_lock():
            return self._issue_binding_code_locked(ttl_seconds)

    def _issue_binding_code_locked(self, ttl_seconds):
        ttl_seconds = max(60, min(int(ttl_seconds), 3600))
        raw = secrets.token_hex(4).upper()
        code = f"{raw[:4]}-{raw[4:]}"
        self._atomic_json(self.binding_path, {
            "sha256": hashlib.sha256(code.encode("ascii")).hexdigest(),
            "created_at": self.clock(),
            "expires_at": self.clock() + ttl_seconds,
            "used": False,
        })
        return code

    def bind(self, code, identity, *, is_private):
        identity_values = (
            getattr(identity, "account", ""),
            getattr(identity, "conversation", ""),
            getattr(identity, "actor", ""),
        )
        if (
            not is_private
            or str(getattr(identity, "platform", "")).strip().lower() != "qq"
            or not all(str(value).strip() for value in identity_values)
        ):
            raise PermissionError(
                "source approver binding requires a complete private QQ identity"
            )
        with self._exclusive_lock():
            return self._bind_locked(code, identity)

    def _bind_locked(self, code, identity):
        pending = self._read_json(self.binding_path, {})
        digest = hashlib.sha256(str(code or "").strip().upper().encode("ascii", errors="ignore")).hexdigest()
        record = self._identity_record(identity)
        if (
            not pending
            or not secrets.compare_digest(digest, str(pending.get("sha256", "")))
        ):
            raise PermissionError("binding code is invalid, expired, or already used")
        if pending.get("used"):
            if pending.get("used_by") != record:
                raise PermissionError("binding code is invalid, expired, or already used")
        else:
            if self.clock() > float(pending.get("expires_at", 0)):
                raise PermissionError("binding code is invalid, expired, or already used")
            pending.update({
                "used": True,
                "used_at": self.clock(),
                "used_by": record,
            })
            self._atomic_json(self.binding_path, pending)
        registry = self._read_json(self.approvers_path, {"approvers": []})
        if record not in registry["approvers"]:
            registry["approvers"].append(record)
        self._atomic_json(self.approvers_path, registry)
        return {"status": "bound", "actor": hashlib.sha256(record["actor"].encode()).hexdigest()[:10]}

    def is_approver(self, identity):
        registry = self._read_json(self.approvers_path, {"approvers": []})
        return any(self._identity_matches(record, identity) for record in registry.get("approvers", []))

    def _resolve_target(self, path):
        raw = Path(path).expanduser()
        if raw.is_symlink():
            raise ValueError("symbolic-link targets are not allowed")
        target = raw.resolve()
        if not target.is_file() or target.is_symlink():
            raise ValueError("change target must be a regular non-symlink file")
        if not any(target == root or root in target.parents for root in self.change_roots):
            raise PermissionError("change target is outside configured roots")
        if target.stat().st_size > self.MAX_FILE_BYTES:
            raise ValueError("change target exceeds the 1 MiB approval limit")
        return target

    @staticmethod
    def _risk_for(target):
        parts = {part.lower() for part in target.parts}
        name = target.name.lower()
        if (
            ".git" in parts
            or ".ssh" in parts
            or parts.intersection({"venv", ".venv"})
            or name in {
                "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "id_xmss",
                "authorized_keys",
            }
            or name.endswith((".key", ".pem", ".p12", ".pfx"))
        ):
            return "emergency"
        if (
            name.startswith(".env")
            or name in {"mykey.py", "mykey.json"}
            or "systemd" in parts
        ):
            return "high"
        return "normal"

    def propose_patch(self, identity, path, old_content, new_content, reason=""):
        with self._exclusive_lock():
            return self._propose_patch_locked(
                identity, path, old_content, new_content, reason
            )

    def _propose_patch_locked(self, identity, path, old_content, new_content, reason):
        if not getattr(identity, "actor", ""):
            raise PermissionError("a source proposal requires an actor identity")
        reason = "" if reason is None else str(reason)
        if not reason.strip():
            raise ValueError("reason must not be empty")
        if len(reason) > 1000:
            raise ValueError("reason must not exceed 1000 characters")
        target = self._resolve_target(path)
        try:
            current_bytes = target.read_bytes()
            current = current_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("binary or non-UTF-8 targets are not supported") from error
        old_content = str(old_content or "")
        new_content = str(new_content or "")
        if not old_content:
            raise ValueError("old_content must not be empty")
        count = current.count(old_content)
        if count != 1:
            raise ValueError(f"old_content must match exactly once; found {count}")
        updated = current.replace(old_content, new_content, 1)
        proposal_id = "SC-" + secrets.token_hex(4).upper()
        proposal_dir = self.proposal_root / proposal_id
        proposal_dir.mkdir(mode=0o700)
        self._chmod(proposal_dir, 0o700)
        created_at = self.clock()
        record = {
            "id": proposal_id,
            "status": "pending",
            "path": str(target),
            "reason": reason,
            "risk": self._risk_for(target),
            "owner": self._identity_record(identity),
            "created_at": created_at,
            "expires_at": created_at + self.PROPOSAL_TTL_SECONDS,
            "before_sha256": hashlib.sha256(current_bytes).hexdigest(),
            "after_sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
            "old_content": old_content,
            "new_content": new_content,
        }
        if record["risk"] == "emergency":
            record["challenge"] = f"{secrets.randbelow(1000000):06d}"
        self._write_proposal_locked(proposal_dir / "proposal.json", record)
        result = {key: record[key] for key in (
            "id", "status", "path", "reason", "risk", "created_at",
            "expires_at", "before_sha256", "after_sha256",
        )}
        result["reason"] = self._redact_visible_text(record["reason"])
        return result

    def _proposal_record(self, proposal_id):
        proposal_id = str(proposal_id or "").strip().upper()
        if not proposal_id.startswith("SC-") or len(proposal_id) != 11:
            raise ValueError("invalid source change proposal id")
        path = (self.proposal_root / proposal_id / "proposal.json").resolve()
        if path.parent.parent != self.proposal_root or not path.is_file() or path.is_symlink():
            raise FileNotFoundError("source change proposal not found")
        record = self._read_json(path)
        self._verify_proposal_locked(record)
        if record.get("id") != proposal_id:
            raise ValueError("source change proposal identity mismatch")
        if record.get("status") == "applying":
            record = self._recover_applying_locked(path, record)
        elif record.get("status") == "rolling_back":
            record = self._recover_rolling_back_locked(path, record)
        return path, record

    def _trusted_backup_bytes_locked(self, record):
        expected = self.backup_root / str(record.get("id", "")) / "original"
        backup_path = Path(str(record.get("backup_path", "")))
        try:
            same_path = os.path.normcase(str(backup_path.absolute())) == os.path.normcase(
                str(expected.absolute())
            )
        except OSError:
            same_path = False
        if (
            not same_path
            or backup_path.is_symlink()
            or backup_path.parent.is_symlink()
            or not backup_path.is_file()
        ):
            raise ValueError("trusted source change backup is unavailable")
        backup_bytes = backup_path.read_bytes()
        actual_digest = hashlib.sha256(backup_bytes).hexdigest()
        expected_digest = str(record.get("backup_sha256", ""))
        if not expected_digest or not secrets.compare_digest(
            actual_digest, expected_digest
        ):
            raise ValueError("trusted source change backup hash mismatch")
        return backup_bytes

    def _recover_applying_locked(self, proposal_path, record):
        target = self._resolve_target(record.get("path", ""))
        target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        before_hash = str(record.get("before_sha256", ""))
        after_hash = str(record.get("after_sha256", ""))
        if secrets.compare_digest(target_hash, after_hash):
            self._trusted_backup_bytes_locked(record)
            record.update({
                "status": "applied",
                "applied_at": self.clock(),
                "recovered_at": self.clock(),
            })
            self._write_proposal_locked(proposal_path, record)
            return record
        if secrets.compare_digest(target_hash, before_hash):
            for field in (
                "approved_by", "backup_path", "backup_sha256",
                "apply_started_at", "applied_at",
            ):
                record.pop(field, None)
            record.update({
                "status": "pending",
                "recovered_at": self.clock(),
            })
            self._write_proposal_locked(proposal_path, record)
            return record
        raise ValueError(
            "source change applying state requires manual recovery; "
            "target matches neither before nor after hash"
        )

    def _recover_rolling_back_locked(self, proposal_path, record):
        target = self._resolve_target(record.get("path", ""))
        target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        before_hash = str(record.get("before_sha256", ""))
        after_hash = str(record.get("after_sha256", ""))
        if secrets.compare_digest(target_hash, before_hash):
            backup_bytes = self._trusted_backup_bytes_locked(record)
            if not secrets.compare_digest(
                hashlib.sha256(backup_bytes).hexdigest(), before_hash
            ):
                raise ValueError("trusted source change backup does not match before hash")
            record.update({
                "status": "rolled_back",
                "rolled_back_at": self.clock(),
                "recovered_at": self.clock(),
            })
            self._write_proposal_locked(proposal_path, record)
            return record
        if secrets.compare_digest(target_hash, after_hash):
            record.pop("rollback_started_at", None)
            record.pop("rolled_back_at", None)
            record.update({
                "status": "applied",
                "recovered_at": self.clock(),
            })
            self._write_proposal_locked(proposal_path, record)
            return record
        raise ValueError(
            "source change rolling-back state requires manual recovery; "
            "target matches neither before nor after hash"
        )

    @staticmethod
    def _key_components(key):
        normalized = str(key or "")
        normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
        normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", normalized)
        return [
            component.lower()
            for component in re.split(r"[^A-Za-z0-9]+", normalized)
            if component
        ]

    @classmethod
    def _is_sensitive_key(cls, key):
        components = cls._key_components(key)
        sensitive_components = {
            "token", "password", "passwd", "pwd", "secret",
            "credential", "credentials",
        }
        sensitive_pairs = {
            ("api", "key"), ("access", "key"), ("private", "key"),
            ("secret", "key"), ("client", "secret"), ("auth", "token"),
        }
        if any(component in sensitive_components for component in components):
            return True
        return any(
            tuple(components[index:index + 2]) in sensitive_pairs
            for index in range(max(0, len(components) - 1))
        )

    @staticmethod
    def _redacted_value(value, label="redacted"):
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]
        return f"<{label} sha256={digest}>"

    @staticmethod
    def _redacted_expression(value):
        data = str(value).encode("utf-8")
        return (
            f"<redacted expression length={len(data)} "
            f"sha256={hashlib.sha256(data).hexdigest()}>"
        )

    @classmethod
    def _mask_credential_values(cls, line):
        """Idempotent final pass for unmistakable credential value formats."""
        credential_value = re.compile(
            r"(?<![A-Za-z0-9])(?:"
            r"sk-[A-Za-z0-9_-]{16,}|"
            r"ghp_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,}|"
            r"xox[A-Za-z]-[A-Za-z0-9-]{10,}|"
            r"AKIA[0-9A-Z]{16}|"
            r"AIza[0-9A-Za-z_-]{20,}"
            r")(?![A-Za-z0-9])"
        )
        return credential_value.sub(
            lambda match: cls._redacted_value(
                match.group(0), label="redacted credential",
            ),
            str(line),
        )

    @classmethod
    def _mask_line(cls, line, *, mask_all_assignments=False):
        if mask_all_assignments:
            match = re.match(r"^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=).*$", line)
            if match:
                digest = hashlib.sha256(line.encode("utf-8")).hexdigest()[:8]
                return cls._mask_credential_values(
                    f"{match.group(1)}<redacted sha256={digest}>"
                )
        if "<redacted expression length=" in line:
            return cls._mask_credential_values(line)

        def mask_quoted_mapping(match):
            if not cls._is_sensitive_key(match.group("key")):
                return match.group(0)
            return (
                f"{match.group('key_quote')}{match.group('key')}"
                f"{match.group('key_quote')}{match.group('sep')}"
                f"{match.group('value_quote')}"
                f"{cls._redacted_value(match.group('value'))}"
                f"{match.group('value_quote')}"
            )

        quoted_mapping = re.compile(
            r'''(?P<key_quote>["'])(?P<key>(?:\\.|[^\\])*?)(?P=key_quote)'''
            r'''(?P<sep>\s*:\s*)(?P<value_quote>["'])'''
            r'''(?P<value>(?:\\.|[^\\])*?)(?P=value_quote)'''
        )
        line = quoted_mapping.sub(mask_quoted_mapping, line)

        def mask_subscript_assignment(match):
            if not cls._is_sensitive_key(match.group("key")):
                return match.group(0)
            return (
                f"{match.group('target')}{match.group('open')}"
                f"{match.group('key_quote')}{match.group('key')}"
                f"{match.group('key_quote')}{match.group('close')}"
                f"{cls._redacted_expression(match.group('value'))}"
            )

        subscript_assignment = re.compile(
            r"(?P<target>[A-Za-z_][A-Za-z0-9_.]*)"
            r"(?P<open>\s*\[\s*)"
            r'''(?P<key_quote>["'])(?P<key>(?:\\.|[^\\])*?)(?P=key_quote)'''
            r"(?P<close>\s*\]\s*=(?!=)\s*)"
            r"(?P<value>.*)$"
        )
        line = subscript_assignment.sub(mask_subscript_assignment, line)

        value_pattern = r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+'''

        def mask_annotated_assignment(match):
            if not cls._is_sensitive_key(match.group("key")):
                return match.group(0)
            return (
                f"{match.group('key')}{match.group('annotation')}"
                f"{match.group('assign')}"
                f"{cls._redacted_expression(match.group('value'))}"
            )

        annotated_assignment = re.compile(
            r"(?<![A-Za-z0-9_])"
            r"(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)"
            r"(?P<annotation>\s*:\s*[^=]+?)"
            r"(?P<assign>\s*=(?!=)\s*)"
            r"(?P<value>.*)$"
        )
        line = annotated_assignment.sub(mask_annotated_assignment, line)

        def mask_direct_assignment(match):
            if not cls._is_sensitive_key(match.group("key")):
                return match.group(0)
            return (
                f"{match.group('key')}{match.group('assign')}"
                f"{cls._redacted_expression(match.group('value'))}"
            )

        direct_assignment = re.compile(
            r"(?<![A-Za-z0-9_])"
            r"(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)"
            r"(?P<assign>\s*=(?!=)\s*)"
            r"(?P<value>.*)$"
        )
        line = direct_assignment.sub(mask_direct_assignment, line)

        assignment = re.compile(
            r"(?<![A-Za-z0-9_])"
            r"(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)"
            r"(?P<sep>\s*:\s*(?![^,;]*=))"
            rf"(?P<value>{value_pattern})"
        )

        def mask_assignment(match):
            if not cls._is_sensitive_key(match.group("key")):
                return match.group(0)
            return (
                f"{match.group('key')}{match.group('sep')}"
                f"{cls._redacted_value(match.group('value'))}"
            )

        line = assignment.sub(mask_assignment, line)

        return cls._mask_credential_values(line)

    @classmethod
    def _redact_visible_text(cls, value):
        text = str(value)
        private_key_block = re.compile(
            r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )*PRIVATE KEY)-----"
            r".*?-----END (?P=label)-----",
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = private_key_block.sub(
            lambda match: cls._private_content_summary(match.group(0)),
            text,
        )
        return "\n".join(cls._mask_line(line) for line in text.splitlines())

    @classmethod
    def _redact_text(cls, value):
        return cls._redact_visible_text(value)

    @staticmethod
    def _private_content_summary(content):
        data = str(content).encode("utf-8")
        return f"<redacted length={len(data)} sha256={hashlib.sha256(data).hexdigest()}>"

    @staticmethod
    def _contains_private_key_block(content):
        return bool(re.search(
            r"-----BEGIN [^\r\n-]*PRIVATE KEY-----",
            str(content),
            flags=re.IGNORECASE,
        ))

    @classmethod
    def _python_target_is_sensitive(cls, target):
        if isinstance(target, ast.Name):
            return cls._is_sensitive_key(target.id)
        if isinstance(target, ast.Attribute):
            return cls._is_sensitive_key(target.attr)
        if isinstance(target, ast.Subscript):
            key = target.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return cls._is_sensitive_key(key.value)
            return False
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(cls._python_target_is_sensitive(item) for item in target.elts)
        if isinstance(target, ast.Starred):
            return cls._python_target_is_sensitive(target.value)
        return False

    @staticmethod
    def _python_line_byte_boundaries(lines):
        boundaries_by_line = []
        for line in lines:
            if line.isascii():
                boundaries_by_line.append(array("I", range(len(line) + 1)))
                continue
            boundaries = array("I", [0])
            byte_offset = 0
            for character in line:
                byte_offset += len(character.encode("utf-8"))
                boundaries.append(byte_offset)
            boundaries_by_line.append(boundaries)
        return boundaries_by_line

    @staticmethod
    def _python_source_offset(
        line_offsets, boundaries_by_line, lineno, byte_col,
    ):
        boundaries = boundaries_by_line[lineno - 1]
        char_col = bisect_left(boundaries, byte_col)
        if (
            byte_col < 0
            or char_col >= len(boundaries)
            or boundaries[char_col] != byte_col
        ):
            raise ValueError("AST column is not a UTF-8 character boundary")
        return line_offsets[lineno - 1] + char_col

    @classmethod
    def _redact_python_content(cls, content):
        text = str(content)
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, TypeError):
            identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", text)
            if any(cls._is_sensitive_key(identifier) for identifier in identifiers):
                return cls._private_content_summary(text)
            return cls._redact_visible_text(text)

        lines = text.splitlines(keepends=True)
        if not lines:
            return text
        line_offsets = []
        offset = 0
        for line in lines:
            line_offsets.append(offset)
            offset += len(line)
        try:
            boundaries_by_line = cls._python_line_byte_boundaries(lines)
        except UnicodeEncodeError:
            return cls._private_content_summary(text)

        spans = []
        for node in ast.walk(tree):
            targets = []
            value = None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                targets, value = [node.target], node.value
            if value is not None and any(
                cls._python_target_is_sensitive(target) for target in targets
            ):
                try:
                    node_start = cls._python_source_offset(
                        line_offsets, boundaries_by_line,
                        node.lineno, node.col_offset,
                    )
                    value_start = cls._python_source_offset(
                        line_offsets, boundaries_by_line,
                        value.lineno, value.col_offset,
                    )
                    node_end = cls._python_source_offset(
                        line_offsets, boundaries_by_line,
                        node.end_lineno, node.end_col_offset,
                    )
                except (AttributeError, IndexError, ValueError):
                    return cls._private_content_summary(text)
                prefix = text[node_start:value_start]
                assignment_index = prefix.rfind("=")
                if assignment_index < 0:
                    return cls._private_content_summary(text)
                rhs_start = node_start + assignment_index + 1
                while rhs_start < node_end and text[rhs_start] in " \t":
                    rhs_start += 1
                if rhs_start >= node_end:
                    return cls._private_content_summary(text)
                spans.append((rhs_start, node_end))

            expression_values = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                arguments = node.args
                positional = tuple(arguments.posonlyargs) + tuple(arguments.args)
                defaults = tuple(arguments.defaults)
                if (
                    len(defaults) > len(positional)
                    or len(arguments.kwonlyargs) != len(arguments.kw_defaults)
                ):
                    return cls._private_content_summary(text)
                positional_with_defaults = (
                    positional[-len(defaults):] if defaults else ()
                )
                expression_values.extend(
                    default
                    for argument, default in zip(positional_with_defaults, defaults)
                    if cls._is_sensitive_key(argument.arg)
                )
                expression_values.extend(
                    default
                    for argument, default in zip(
                        arguments.kwonlyargs, arguments.kw_defaults,
                    )
                    if default is not None and cls._is_sensitive_key(argument.arg)
                )
            elif isinstance(node, ast.Dict):
                expression_values.extend(
                    item_value
                    for key, item_value in zip(node.keys, node.values)
                    if (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and cls._is_sensitive_key(key.value)
                    )
                )
            elif isinstance(node, ast.Call):
                expression_values.extend(
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg and cls._is_sensitive_key(keyword.arg)
                )
            for expression_value in expression_values:
                try:
                    expression_start = cls._python_source_offset(
                        line_offsets, boundaries_by_line,
                        expression_value.lineno, expression_value.col_offset,
                    )
                    expression_end = cls._python_source_offset(
                        line_offsets, boundaries_by_line,
                        expression_value.end_lineno, expression_value.end_col_offset,
                    )
                except (AttributeError, IndexError, ValueError):
                    return cls._private_content_summary(text)
                if expression_start >= expression_end:
                    return cls._private_content_summary(text)
                spans.append((expression_start, expression_end))

        # Keep only outermost spans so nested sensitive assignments cannot
        # invalidate offsets or expose fragments of an enclosing RHS.
        selected = []
        for start, end in sorted(spans, key=lambda item: (item[0], -item[1])):
            if selected and start >= selected[-1][0] and end <= selected[-1][1]:
                continue
            if selected and start < selected[-1][1]:
                selected[-1] = (selected[-1][0], max(selected[-1][1], end))
            else:
                selected.append((start, end))
        redacted_parts = []
        cursor = 0
        for start, end in selected:
            redacted_parts.append(text[cursor:start])
            redacted_parts.append(cls._redacted_expression(text[start:end]))
            cursor = end
        redacted_parts.append(text[cursor:])
        text = "".join(redacted_parts)
        return cls._redact_visible_text(text)

    @staticmethod
    def _line_ending_signature(content):
        text = str(content)
        endings = tuple(
            {"\r\n": "CRLF", "\r": "CR", "\n": "LF"}[ending]
            for ending in re.findall(r"\r\n|\r|\n", text)
        )
        if text and not text.endswith(("\r\n", "\r", "\n")):
            endings += ("NO_NEWLINE",)
        return endings

    @classmethod
    def _format_line_endings(cls, content):
        endings = cls._line_ending_signature(content)
        if not endings:
            return "EMPTY"
        runs = []
        start = 1
        marker = endings[0]
        for index, current in enumerate(endings[1:], start=2):
            if current == marker:
                continue
            location = f"line {start}" if start == index - 1 else f"lines {start}-{index - 1}"
            runs.append(f"{marker} ({location})")
            start = index
            marker = current
        end = len(endings)
        location = f"line {start}" if start == end else f"lines {start}-{end}"
        runs.append(f"{marker} ({location})")
        return ", ".join(runs)

    @classmethod
    def _line_ending_diff(cls, old_content, new_content):
        if cls._line_ending_signature(old_content) == cls._line_ending_signature(
            new_content
        ):
            return ""
        return "\n".join((
            "@@ line endings @@",
            f"- old: {cls._format_line_endings(old_content)}",
            f"+ new: {cls._format_line_endings(new_content)}",
        ))

    @classmethod
    def _render_diff(cls, record):
        target = Path(record["path"])
        old_content = str(record.get("old_content", ""))
        new_content = str(record.get("new_content", ""))
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        environment_file = target.name.lower().startswith(".env")
        private_content = (
            target.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}
            or cls._contains_private_key_block(record.get("old_content", ""))
            or cls._contains_private_key_block(record.get("new_content", ""))
        )
        if private_content:
            old_lines = [cls._private_content_summary(record.get("old_content", ""))]
            new_lines = [cls._private_content_summary(record.get("new_content", ""))]
        elif environment_file:
            old_lines = [
                cls._mask_line(line, mask_all_assignments=True)
                for line in old_lines
            ]
            new_lines = [
                cls._mask_line(line, mask_all_assignments=True)
                for line in new_lines
            ]
        elif target.suffix.lower() == ".py":
            old_lines = cls._redact_python_content(
                record.get("old_content", ""),
            ).splitlines()
            new_lines = cls._redact_python_content(
                record.get("new_content", ""),
            ).splitlines()
        else:
            old_lines = [cls._mask_line(line) for line in old_lines]
            new_lines = [cls._mask_line(line) for line in new_lines]
        content_diff = "\n".join(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{target.name}", tofile=f"b/{target.name}", lineterm="",
        ))
        line_ending_diff = cls._line_ending_diff(old_content, new_content)
        return "\n".join(
            part for part in (content_diff, line_ending_diff) if part
        )

    @staticmethod
    def _raw_diff_length(record):
        target = Path(record["path"])
        raw_diff = "".join(difflib.unified_diff(
            str(record.get("old_content", "")).splitlines(keepends=True),
            str(record.get("new_content", "")).splitlines(keepends=True),
            fromfile=f"a/{target.name}", tofile=f"b/{target.name}", lineterm="\n",
        ))
        return len(raw_diff)

    def show(self, proposal_id, identity):
        with self._exclusive_lock():
            return self._show_locked(proposal_id, identity)

    def _show_locked(self, proposal_id, identity):
        _, record = self._proposal_record(proposal_id)
        if not (
            self.is_approver(identity)
            or self._identity_matches(record.get("owner", {}), identity)
        ):
            raise PermissionError("proposal is visible only to its owner or a bound approver")
        result = {
            key: record.get(key) for key in (
                "id", "status", "path", "reason", "risk", "created_at",
                "expires_at", "before_sha256", "after_sha256",
            )
        }
        result["reason"] = self._redact_visible_text(record.get("reason", ""))
        result["diff"] = self._render_diff(record)
        result["raw_diff_length"] = self._raw_diff_length(record)
        if record.get("risk") == "emergency" and self.is_approver(identity):
            result["challenge"] = record.get("challenge", "")
        return result

    def list_visible(self, identity):
        with self._exclusive_lock():
            return self._list_visible_locked(identity)

    def _list_visible_locked(self, identity):
        visible = []
        for proposal_dir in sorted(self.proposal_root.iterdir()):
            if not proposal_dir.is_dir() or proposal_dir.is_symlink():
                continue
            try:
                _, record = self._proposal_record(proposal_dir.name)
            except (OSError, ValueError):
                continue
            if self.is_approver(identity) or self._identity_matches(record.get("owner", {}), identity):
                item = {key: record.get(key) for key in (
                    "id", "status", "path", "reason", "risk", "created_at", "expires_at",
                )}
                item["reason"] = self._redact_visible_text(record.get("reason", ""))
                visible.append(item)
        return visible

    def reject(self, proposal_id, identity):
        with self._exclusive_lock():
            return self._reject_locked(proposal_id, identity)

    def _reject_locked(self, proposal_id, identity):
        proposal_path, record = self._proposal_record(proposal_id)
        if not (
            self.is_approver(identity)
            or self._identity_matches(record.get("owner", {}), identity)
        ):
            raise PermissionError("proposal rejection requires its owner or a bound approver")
        if record.get("status") != "pending":
            raise ValueError("source change proposal is not pending")
        record.update({"status": "rejected", "rejected_at": self.clock()})
        self._write_proposal_locked(proposal_path, record)
        return {"id": record["id"], "status": "rejected"}

    def rollback(self, proposal_id, identity):
        with self._exclusive_lock():
            return self._rollback_locked(proposal_id, identity)

    def _rollback_locked(self, proposal_id, identity):
        if not self.is_approver(identity):
            raise PermissionError("source change rollback requires a bound approver")
        proposal_path, record = self._proposal_record(proposal_id)
        if record.get("status") != "applied":
            raise ValueError("only an applied source change can be rolled back")
        target = self._resolve_target(record["path"])
        current = target.read_bytes()
        if hashlib.sha256(current).hexdigest() != record.get("after_sha256"):
            raise ValueError("source file changed after apply; automatic rollback refused")
        backup_bytes = self._trusted_backup_bytes_locked(record)
        mode = target.stat().st_mode & 0o777
        record.update({
            "status": "rolling_back",
            "rollback_started_at": self.clock(),
        })
        self._write_proposal_locked(proposal_path, record)
        self._atomic_bytes(target, backup_bytes, mode)
        record.update({"status": "rolled_back", "rolled_back_at": self.clock()})
        self._write_proposal_locked(proposal_path, record)
        print(f"[SOURCE-AUDIT] action=rollback id={record['id']}")
        return {"id": record["id"], "status": "rolled_back", "path": str(target)}

    def approve(self, proposal_id, identity, *, authorization="normal", challenge=""):
        with self._exclusive_lock():
            return self._approve_locked(
                proposal_id, identity,
                authorization=authorization, challenge=challenge,
            )

    def _approve_locked(self, proposal_id, identity, *, authorization, challenge):
        if not self.is_approver(identity):
            raise PermissionError("source change approval requires a bound approver")
        proposal_path, record = self._proposal_record(proposal_id)
        if record.get("status") != "pending":
            raise ValueError("source change proposal is not pending")
        if self.clock() > float(record.get("expires_at", 0)):
            raise ValueError("source change proposal expired")
        risk = record.get("risk", "normal")
        authorization = str(authorization or "normal").lower()
        if authorization != risk:
            raise PermissionError(
                f"{risk}-risk source change requires exact {risk} authorization"
            )
        if risk == "emergency":
            if not secrets.compare_digest(
                str(challenge or ""), str(record.get("challenge", ""))
            ):
                raise PermissionError("emergency source change requires the exact challenge")

        target = self._resolve_target(record["path"])
        current_bytes = target.read_bytes()
        current_hash = hashlib.sha256(current_bytes).hexdigest()
        if not secrets.compare_digest(current_hash, str(record.get("before_sha256", ""))):
            raise ValueError("source file changed after proposal creation")
        current = current_bytes.decode("utf-8")
        old_content = str(record.get("old_content", ""))
        if current.count(old_content) != 1:
            raise ValueError("approved patch no longer matches exactly once")
        updated_bytes = current.replace(old_content, str(record.get("new_content", "")), 1).encode("utf-8")
        updated_hash = hashlib.sha256(updated_bytes).hexdigest()
        if not secrets.compare_digest(updated_hash, str(record.get("after_sha256", ""))):
            raise ValueError("approved patch content hash mismatch")

        backup_dir = self.backup_root / record["id"]
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        self._chmod(backup_dir, 0o700)
        backup_path = backup_dir / "original"
        self._atomic_bytes(backup_path, current_bytes, 0o600)
        backup_sha256 = hashlib.sha256(current_bytes).hexdigest()
        record.update({
            "status": "applying",
            "apply_started_at": self.clock(),
            "approved_by": self._identity_record(identity),
            "backup_path": str(backup_path),
            "backup_sha256": backup_sha256,
        })
        self._write_proposal_locked(proposal_path, record)
        mode = target.stat().st_mode & 0o777
        self._atomic_bytes(target, updated_bytes, mode)

        record.update({
            "status": "applied",
            "applied_at": self.clock(),
        })
        self._write_proposal_locked(proposal_path, record)
        actor_token = hashlib.sha256(str(identity.actor).encode()).hexdigest()[:10]
        print(f"[SOURCE-AUDIT] action=apply id={record['id']} risk={risk} actor={actor_token}")
        return {
            "id": record["id"], "status": "applied", "risk": risk,
            "path": str(target), "backup_path": str(backup_path),
            "before_sha256": record["before_sha256"],
            "after_sha256": record["after_sha256"],
        }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Manage GenericAgent's QQ source-change approval bootstrap.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    issue = subparsers.add_parser(
        "issue-code", help="issue a one-time private-chat binding code",
    )
    issue.add_argument("--project-root", required=True)
    issue.add_argument("--ttl", type=int, default=600)
    args = parser.parse_args(argv)

    if args.command == "issue-code":
        project_root = Path(args.project_root).resolve()
        roots = [project_root]
        for raw in re.split(r"[,;]", os.environ.get("GENERICAGENT_CHANGE_ROOTS", "")):
            raw = raw.strip()
            if raw:
                roots.append(Path(raw).resolve())
        manager = ChangeApprovalManager(
            get_change_state_root(),
            roots,
        )
        print(manager.issue_binding_code(ttl_seconds=args.ttl), flush=True)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
