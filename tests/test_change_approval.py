import io
import json
import multiprocessing
import os
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import change_approval
from change_approval import ChangeApprovalManager
from session_store import ConversationIdentity


def _concurrent_bind_worker(state, project, code, actor, start, results):
    manager = ChangeApprovalManager(state, [project])
    identity = ConversationIdentity("qq", "bot", f"private-{actor}", actor)
    start.wait(10)
    try:
        manager.bind(code, identity, is_private=True)
    except Exception as error:
        results.put(("error", type(error).__name__))
    else:
        results.put(("success", actor))


def _concurrent_terminal_worker(
    state, project, proposal_id, operation, start, results,
):
    manager = ChangeApprovalManager(state, [project])
    identity = ConversationIdentity("qq", "bot", "owner-private", "owner")
    start.wait(10)
    try:
        if operation.startswith("approve"):
            outcome = manager.approve(proposal_id, identity)
        else:
            outcome = manager.reject(proposal_id, identity)
    except Exception as error:
        results.put(("error", operation, type(error).__name__, str(error)))
    else:
        results.put(("success", operation, outcome["status"]))


def _concurrent_propose_worker(state, project, filename, start, results):
    manager = ChangeApprovalManager(state, [project])
    identity = ConversationIdentity("qq", "bot", "owner-private", "owner")
    start.wait(10)
    try:
        proposal = manager.propose_patch(
            identity,
            Path(project) / filename,
            "before",
            "after",
            f"concurrent proposal for {filename}",
        )
    except Exception as error:
        results.put(("error", filename, type(error).__name__, str(error)))
    else:
        results.put(("success", filename, proposal["id"]))


class ChangeApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.project = self.root / "project"
        self.project.mkdir()
        self.manager = ChangeApprovalManager(self.state, [self.project])
        self.owner = ConversationIdentity("qq", "bot", "owner-private", "owner")
        self.other = ConversationIdentity("qq", "bot", "other-private", "other")

    @staticmethod
    def exhaust(generator):
        while True:
            try:
                next(generator)
            except StopIteration as stopped:
                return stopped.value

    def race_terminal_operations(self, proposal_id, operations):
        context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_concurrent_terminal_worker,
                args=(
                    self.state, self.project, proposal_id, operation, start, results,
                ),
            )
            for operation in operations
        ]
        for worker in workers:
            worker.start()
        start.set()
        outcomes = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            self.assertEqual(worker.exitcode, 0)
        return outcomes

    def test_manager_initialization_fails_closed_when_chmod_fails(self):
        with patch(
            "change_approval.os.chmod",
            side_effect=PermissionError("simulated chmod denial"),
        ):
            with self.assertRaisesRegex(PermissionError, "chmod denial"):
                ChangeApprovalManager(self.root / "chmod-failure-state", [self.project])

    @unittest.skipIf(os.name == "nt", "POSIX private mode and owner verification")
    def test_private_state_paths_have_exact_modes_and_effective_owner(self):
        code = self.manager.issue_binding_code()
        self.manager.bind(code, self.owner, is_private=True)
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "verify private modes"
        )
        applied = self.manager.approve(proposal["id"], self.owner)
        proposal_dir = self.state / "proposals" / proposal["id"]
        backup_path = Path(applied["backup_path"])

        private_directories = (
            self.state,
            self.state / "auth",
            self.state / "proposals",
            self.state / "backups",
            proposal_dir,
            backup_path.parent,
        )
        private_files = (
            self.manager.lock_path,
            self.manager.binding_path,
            self.manager.approvers_path,
            self.manager.integrity_key_path,
            proposal_dir / "proposal.json",
            backup_path,
        )
        for path in private_directories:
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(path.stat().st_uid, os.geteuid())
        for path in private_files:
            with self.subTest(path=path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.stat().st_uid, os.geteuid())

    def test_one_time_private_binding_creates_the_only_initial_approver(self):
        code = self.manager.issue_binding_code(ttl_seconds=600)

        bound = self.manager.bind(code, self.owner, is_private=True)

        self.assertEqual(bound["status"], "bound")
        self.assertTrue(self.manager.is_approver(self.owner))
        self.assertFalse(self.manager.is_approver(self.other))
        with self.assertRaises(PermissionError):
            self.manager.bind(code, self.other, is_private=True)

    def test_concurrent_processes_can_bind_the_same_code_only_once(self):
        code = self.manager.issue_binding_code(ttl_seconds=600)
        context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_concurrent_bind_worker,
                args=(self.state, self.project, code, f"actor-{index}", start, results),
            )
            for index in range(6)
        ]
        for worker in workers:
            worker.start()
        start.set()
        outcomes = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            self.assertEqual(worker.exitcode, 0)

        self.assertEqual(
            len([item for item in outcomes if item[0] == "success"]), 1
        )
        lock_path = self.state / "change_approval.lock"
        self.assertTrue(lock_path.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_bind_registry_failure_reserves_code_for_exact_identity_retry(self):
        code = self.manager.issue_binding_code(ttl_seconds=600)
        original_atomic_json = self.manager._atomic_json

        def fail_approver_registry(path, value, mode=0o600):
            if Path(path) == self.manager.approvers_path:
                raise OSError("simulated approver registry write failure")
            return original_atomic_json(path, value, mode)

        with patch.object(
            self.manager, "_atomic_json", side_effect=fail_approver_registry
        ):
            with self.assertRaisesRegex(OSError, "registry write failure"):
                self.manager.bind(code, self.owner, is_private=True)

        binding = json.loads(self.manager.binding_path.read_text(encoding="utf-8"))
        self.assertTrue(binding["used"])
        self.assertEqual(binding["used_by"], {
            "platform": self.owner.platform,
            "account": self.owner.account,
            "conversation": self.owner.conversation,
            "actor": self.owner.actor,
        })
        self.manager.clock = lambda: float(binding["expires_at"]) + 1
        with self.assertRaises(PermissionError):
            self.manager.bind(code, self.other, is_private=True)

        self.assertEqual(
            self.manager.bind(code, self.owner, is_private=True)["status"],
            "bound",
        )
        self.assertTrue(self.manager.is_approver(self.owner))
        self.assertFalse(self.manager.is_approver(self.other))

    def test_concurrent_first_proposals_share_one_valid_integrity_key(self):
        filenames = ("first.py", "second.py")
        for filename in filenames:
            (self.project / filename).write_text("before", encoding="utf-8")
        context = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=_concurrent_propose_worker,
                args=(self.state, self.project, filename, start, results),
            )
            for filename in filenames
        ]
        for worker in workers:
            worker.start()
        start.set()
        outcomes = [results.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(15)
            self.assertEqual(worker.exitcode, 0)

        self.assertTrue(all(outcome[0] == "success" for outcome in outcomes), outcomes)
        integrity_key_path = self.state / "integrity.key"
        self.assertEqual(len(integrity_key_path.read_bytes()), 32)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(integrity_key_path.stat().st_mode), 0o600)
        verifier = ChangeApprovalManager(self.state, [self.project])
        for outcome in outcomes:
            self.assertEqual(verifier.show(outcome[2], self.owner)["status"], "pending")

    def test_binding_accepts_only_a_complete_private_qq_identity(self):
        invalid_identities = (
            ConversationIdentity("telegram", "bot", "private", "actor"),
            ConversationIdentity("qq", "", "private", "actor"),
            ConversationIdentity("qq", "bot", "", "actor"),
            ConversationIdentity("qq", "bot", "private", ""),
        )
        for identity in invalid_identities:
            with self.subTest(identity=identity):
                code = self.manager.issue_binding_code()
                with self.assertRaises(PermissionError):
                    self.manager.bind(code, identity, is_private=True)

        code = self.manager.issue_binding_code()
        self.manager.bind(code, self.owner, is_private=True)
        same_actor_other_conversation = ConversationIdentity(
            "qq", "bot", "different-private", self.owner.actor
        )
        self.assertFalse(self.manager.is_approver(same_actor_other_conversation))

    def test_issue_code_cli_prints_once_and_persists_only_its_digest(self):
        output = io.StringIO()

        with (
            patch.dict(
                os.environ,
                {"GENERICAGENT_CHANGE_STATE_ROOT": str(self.state)},
                clear=False,
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(change_approval.get_change_state_root(), self.state.resolve())
            exit_code = change_approval.main([
                "issue-code", "--project-root", str(self.project), "--ttl", "600",
            ])

        code = output.getvalue().strip()
        self.assertEqual(exit_code, 0)
        self.assertRegex(code, r"^[0-9A-F]{4}-[0-9A-F]{4}$")
        stored_path = self.state / "auth" / "binding.json"
        stored_text = stored_path.read_text(encoding="utf-8")
        self.assertNotIn(code, stored_text)
        self.assertEqual(
            json.loads(stored_text)["sha256"],
            sha256(code.encode("ascii")).hexdigest(),
        )

    def test_exact_patch_proposal_is_immutable_and_does_not_change_target(self):
        target = self.project / "ga.py"
        target.write_bytes(b"old\n")

        proposal = self.manager.propose_patch(
            self.owner, target, "old\n", "new\n", "repair behavior"
        )

        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(proposal["before_sha256"], sha256(b"old\n").hexdigest())
        self.assertTrue(proposal["id"].startswith("SC-"))
        self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_proposal_path_tampering_is_rejected_before_show(self):
        target = self.project / "ga.py"
        target.write_text("old", encoding="utf-8")
        other_target = self.project / "other.py"
        other_target.write_text("other", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "old", "new", "repair behavior"
        )
        proposal_path = (
            self.state / "proposals" / proposal["id"] / "proposal.json"
        )
        record = json.loads(proposal_path.read_text(encoding="utf-8"))
        record["path"] = str(other_target)
        proposal_path.write_text(json.dumps(record), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "integrity"):
            self.manager.show(proposal["id"], self.owner)

    def test_corrupt_proposal_json_is_reported_instead_of_defaulted(self):
        target = self.project / "ga.py"
        target.write_text("old", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "old", "new", "repair behavior"
        )
        proposal_path = (
            self.state / "proposals" / proposal["id"] / "proposal.json"
        )
        proposal_path.write_text('{"truncated":', encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            self.manager.show(proposal["id"], self.owner)

    def test_proposal_hmac_covers_security_and_state_fields(self):
        target = self.project / "ga.py"
        target.write_text("old", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "old", "new", "repair behavior"
        )
        proposal_path = (
            self.state / "proposals" / proposal["id"] / "proposal.json"
        )
        original = json.loads(proposal_path.read_text(encoding="utf-8"))
        mutations = {
            "risk": "emergency",
            "before_sha256": "0" * 64,
            "after_sha256": "f" * 64,
            "backup_path": str(self.root / "attacker-backup"),
            "status": "applied",
        }

        for field, value in mutations.items():
            with self.subTest(field=field):
                tampered = dict(original)
                tampered[field] = value
                proposal_path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "integrity"):
                    self.manager.show(proposal["id"], self.owner)
                proposal_path.write_text(json.dumps(original), encoding="utf-8")

    def test_proposal_rejects_an_empty_reason(self):
        target = self.project / "ga.py"
        target.write_text("old", encoding="utf-8")

        for reason in ("", "   "):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, "reason"):
                    self.manager.propose_patch(
                        self.owner, target, "old", "new", reason
                    )

    def test_reason_preserves_original_whitespace_and_rejects_over_1000_chars(self):
        target = self.project / "ga.py"
        target.write_text("old", encoding="utf-8")
        reason = "  preserve these exact surrounding spaces  "

        proposal = self.manager.propose_patch(
            self.owner, target, "old", "new", reason
        )
        proposal_path = self.state / "proposals" / proposal["id"] / "proposal.json"

        self.assertEqual(
            json.loads(proposal_path.read_text(encoding="utf-8"))["reason"],
            reason,
        )
        with self.assertRaisesRegex(ValueError, "1000"):
            self.manager.propose_patch(
                self.owner, target, "old", "new", "x" * 1001
            )

    def test_unbound_actor_cannot_approve_a_pending_change(self):
        target = self.project / "ga.py"
        target.write_text("old", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "old", "new", "repair behavior"
        )

        with self.assertRaises(PermissionError):
            self.manager.approve(proposal["id"], self.owner)

        self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_bound_approver_applies_exact_normal_patch_once_with_backup(self):
        code = self.manager.issue_binding_code()
        self.manager.bind(code, self.owner, is_private=True)
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "repair behavior"
        )

        result = self.manager.approve(proposal["id"], self.owner)

        self.assertEqual(result["status"], "applied")
        self.assertEqual(target.read_text(encoding="utf-8"), "after")
        self.assertEqual(Path(result["backup_path"]).read_text(encoding="utf-8"), "before")
        with self.assertRaises(ValueError):
            self.manager.approve(proposal["id"], self.owner)

    def test_concurrent_approve_and_reject_have_one_consistent_winner(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / "race.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "race terminal decisions"
        )

        outcomes = self.race_terminal_operations(
            proposal["id"], ("approve", "reject")
        )

        successes = [outcome for outcome in outcomes if outcome[0] == "success"]
        self.assertEqual(len(successes), 1, outcomes)
        shown = ChangeApprovalManager(self.state, [self.project]).show(
            proposal["id"], self.owner
        )
        record_path = self.state / "proposals" / proposal["id"] / "proposal.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], shown["status"])
        if shown["status"] == "applied":
            self.assertEqual(successes[0][1:], ("approve", "applied"))
            self.assertEqual(target.read_text(encoding="utf-8"), "after")
            backup = Path(record["backup_path"])
            self.assertEqual(backup.read_text(encoding="utf-8"), "before")
            self.assertEqual(sha256(backup.read_bytes()).hexdigest(), record["backup_sha256"])
            self.assertNotIn("rejected_at", record)
        else:
            self.assertEqual(successes[0][1:], ("reject", "rejected"))
            self.assertEqual(shown["status"], "rejected")
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            self.assertNotIn("backup_path", record)
            self.assertFalse((self.state / "backups" / proposal["id"]).exists())

    def test_two_concurrent_approvals_apply_and_back_up_exactly_once(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / "double-approve.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "race duplicate approvals"
        )

        outcomes = self.race_terminal_operations(
            proposal["id"], ("approve-1", "approve-2")
        )

        successes = [outcome for outcome in outcomes if outcome[0] == "success"]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(successes[0][2], "applied")
        shown = ChangeApprovalManager(self.state, [self.project]).show(
            proposal["id"], self.owner
        )
        self.assertEqual(shown["status"], "applied")
        self.assertEqual(target.read_text(encoding="utf-8"), "after")
        record_path = self.state / "proposals" / proposal["id"] / "proposal.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        backup = Path(record["backup_path"])
        self.assertEqual(backup.read_text(encoding="utf-8"), "before")
        self.assertEqual(sha256(backup.read_bytes()).hexdigest(), record["backup_sha256"])

    def test_normal_change_rejects_high_and_emergency_authorization(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "repair behavior"
        )

        for authorization in ("high", "emergency"):
            with self.subTest(authorization=authorization):
                with self.assertRaisesRegex(PermissionError, "exact normal"):
                    self.manager.approve(
                        proposal["id"], self.owner, authorization=authorization
                    )

        self.assertEqual(target.read_text(encoding="utf-8"), "before")
        self.assertEqual(
            self.manager.approve(proposal["id"], self.owner)["status"],
            "applied",
        )

    def test_crash_after_target_write_recovers_applying_as_applied(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "repair behavior"
        )
        original_atomic_json = self.manager._atomic_json

        def fail_final_applied_write(path, value, mode=0o600):
            if value.get("status") == "applied":
                raise OSError("simulated crash before final audit write")
            return original_atomic_json(path, value, mode)

        with patch.object(
            self.manager, "_atomic_json", side_effect=fail_final_applied_write
        ):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.manager.approve(proposal["id"], self.owner)

        self.assertEqual(target.read_text(encoding="utf-8"), "after")
        recovered = ChangeApprovalManager(self.state, [self.project]).show(
            proposal["id"], self.owner
        )
        self.assertEqual(recovered["status"], "applied")

    def test_failed_target_write_recovers_applying_to_retryable_pending(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "repair behavior"
        )
        original_atomic_bytes = self.manager._atomic_bytes

        def fail_target_write(path, content, mode):
            if Path(path) == target:
                raise OSError("simulated target write failure")
            return original_atomic_bytes(path, content, mode)

        with patch.object(
            self.manager, "_atomic_bytes", side_effect=fail_target_write
        ):
            with self.assertRaisesRegex(OSError, "target write failure"):
                self.manager.approve(proposal["id"], self.owner)

        recovered_manager = ChangeApprovalManager(self.state, [self.project])
        self.assertEqual(
            recovered_manager.show(proposal["id"], self.owner)["status"],
            "pending",
        )
        self.assertEqual(
            recovered_manager.approve(proposal["id"], self.owner)["status"],
            "applied",
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "after")

    def test_environment_change_requires_high_risk_authorization(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / ".env"
        target.write_text("MODE=old", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "MODE=old", "MODE=new", "switch mode"
        )

        self.assertEqual(proposal["risk"], "high")
        with self.assertRaises(PermissionError):
            self.manager.approve(proposal["id"], self.owner)
        with self.assertRaisesRegex(PermissionError, "exact high"):
            self.manager.approve(
                proposal["id"], self.owner, authorization="emergency"
            )
        result = self.manager.approve(
            proposal["id"], self.owner, authorization="high"
        )

        self.assertEqual(result["status"], "applied")
        self.assertEqual(target.read_text(encoding="utf-8"), "MODE=new")

    def test_environment_variants_and_dot_venv_are_risk_classified(self):
        environment = self.project / ".env.production"
        environment.write_text("MODE=old\n", encoding="utf-8")
        environment_proposal = self.manager.propose_patch(
            self.owner, environment, "MODE=old", "MODE=new", "deploy mode",
        )
        virtual_env = self.project / ".venv"
        virtual_env.mkdir()
        activation = virtual_env / "activate"
        activation.write_text("old", encoding="utf-8")
        virtual_env_proposal = self.manager.propose_patch(
            self.owner, activation, "old", "new", "repair environment",
        )

        self.assertEqual(environment_proposal["risk"], "high")
        self.assertEqual(virtual_env_proposal["risk"], "emergency")

    def test_sensitive_path_variants_have_unified_high_or_emergency_risk(self):
        cases = (
            (".env", "high"),
            (".env.local", "high"),
            (".envrc", "high"),
            (".environment", "high"),
            ("mykey.py", "high"),
            ("mykey.json", "high"),
            (".ssh/config", "emergency"),
            ("id_rsa", "emergency"),
            ("id_ed25519", "emergency"),
            ("id_ecdsa", "emergency"),
            ("id_dsa", "emergency"),
            ("id_xmss", "emergency"),
            ("authorized_keys", "emergency"),
            ("server.key", "emergency"),
            ("server.pem", "emergency"),
            ("server.p12", "emergency"),
            ("server.pfx", "emergency"),
        )

        for relative_path, expected_risk in cases:
            with self.subTest(relative_path=relative_path):
                target = self.project / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("old", encoding="utf-8")
                proposal = self.manager.propose_patch(
                    self.owner, target, "old", "new", "classify sensitive path"
                )
                self.assertEqual(proposal["risk"], expected_risk)

    def test_repository_internal_change_requires_emergency_challenge(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        git_dir = self.project / ".git"
        git_dir.mkdir()
        target = git_dir / "config"
        target.write_text("mode=old", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "mode=old", "mode=new", "repository repair"
        )

        shown = self.manager.show(proposal["id"], self.owner)
        self.assertEqual(shown["risk"], "emergency")
        for authorization in ("normal", "high"):
            with self.subTest(authorization=authorization):
                with self.assertRaisesRegex(PermissionError, "exact emergency"):
                    self.manager.approve(
                        proposal["id"], self.owner,
                        authorization=authorization, challenge=shown["challenge"],
                    )
        with self.assertRaises(PermissionError):
            self.manager.approve(
                proposal["id"], self.owner,
                authorization="emergency", challenge="000000",
            )
        result = self.manager.approve(
            proposal["id"], self.owner,
            authorization="emergency", challenge=shown["challenge"],
        )
        self.assertEqual(result["status"], "applied")

    def test_changed_file_invalidates_pending_authorization(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "repair behavior"
        )
        target.write_text("changed elsewhere", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "changed after proposal"):
            self.manager.approve(proposal["id"], self.owner)

        self.assertEqual(target.read_text(encoding="utf-8"), "changed elsewhere")

    def test_applied_change_can_be_rolled_back_once_by_approver(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "repair behavior"
        )
        self.manager.approve(proposal["id"], self.owner)

        result = self.manager.rollback(proposal["id"], self.owner)

        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(target.read_text(encoding="utf-8"), "before")

    def test_rollback_recovers_when_final_state_write_fails_after_target_write(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "repair behavior"
        )
        self.manager.approve(proposal["id"], self.owner)
        original_atomic_json = self.manager._atomic_json

        def fail_final_rollback_write(path, value, mode=0o600):
            if value.get("status") == "rolled_back":
                raise OSError("simulated crash before final rollback audit write")
            return original_atomic_json(path, value, mode)

        with patch.object(
            self.manager, "_atomic_json", side_effect=fail_final_rollback_write
        ):
            with self.assertRaisesRegex(OSError, "final rollback audit"):
                self.manager.rollback(proposal["id"], self.owner)

        self.assertEqual(target.read_text(encoding="utf-8"), "before")
        proposal_path = self.state / "proposals" / proposal["id"] / "proposal.json"
        self.assertEqual(
            json.loads(proposal_path.read_text(encoding="utf-8"))["status"],
            "rolling_back",
        )
        recovered = ChangeApprovalManager(self.state, [self.project]).show(
            proposal["id"], self.owner
        )
        self.assertEqual(recovered["status"], "rolled_back")

    def test_rollback_target_write_failure_recovers_to_retryable_applied(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "repair behavior"
        )
        self.manager.approve(proposal["id"], self.owner)
        original_atomic_bytes = self.manager._atomic_bytes

        def fail_rollback_target_write(path, content, mode):
            if Path(path) == target and content == b"before":
                raise OSError("simulated rollback target write failure")
            return original_atomic_bytes(path, content, mode)

        with patch.object(
            self.manager, "_atomic_bytes", side_effect=fail_rollback_target_write
        ):
            with self.assertRaisesRegex(OSError, "rollback target write failure"):
                self.manager.rollback(proposal["id"], self.owner)

        proposal_path = self.state / "proposals" / proposal["id"] / "proposal.json"
        interrupted = json.loads(proposal_path.read_text(encoding="utf-8"))
        self.assertEqual(interrupted["status"], "rolling_back")
        self.assertIn("rollback_started_at", interrupted)
        self.assertEqual(target.read_text(encoding="utf-8"), "after")

        recovered_manager = ChangeApprovalManager(self.state, [self.project])
        self.assertEqual(
            recovered_manager.show(proposal["id"], self.owner)["status"],
            "applied",
        )
        recovered_record = json.loads(proposal_path.read_text(encoding="utf-8"))
        self.assertNotIn("rollback_started_at", recovered_record)
        self.assertNotIn("rolled_back_at", recovered_record)
        self.assertEqual(
            recovered_manager.rollback(proposal["id"], self.owner)["status"],
            "rolled_back",
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "before")

    def test_tampered_backup_hash_is_rejected_before_rollback(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", "repair behavior"
        )
        applied = self.manager.approve(proposal["id"], self.owner)
        Path(applied["backup_path"]).write_text("attacker content", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "backup hash"):
            self.manager.rollback(proposal["id"], self.owner)

        self.assertEqual(target.read_text(encoding="utf-8"), "after")

    def test_sensitive_diff_is_redacted_but_exact_patch_remains_applicable(self):
        self.manager.bind(self.manager.issue_binding_code(), self.owner, is_private=True)
        target = self.project / ".env"
        target.write_text("API_KEY=secret-old", encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target,
            "API_KEY=secret-old", "API_KEY=secret-new", "rotate key",
        )

        shown = self.manager.show(proposal["id"], self.owner)

        self.assertNotIn("secret-old", shown["diff"])
        self.assertNotIn("secret-new", shown["diff"])
        self.assertIn("<redacted", shown["diff"])
        self.manager.approve(proposal["id"], self.owner, authorization="high")
        self.assertEqual(target.read_text(encoding="utf-8"), "API_KEY=secret-new")

    def test_ordinary_python_assignment_is_fully_visible_in_review_diff(self):
        target = self.project / "handlers.py"
        old = "HANDLER = safe_handler"
        new = "HANDLER = __import__('os').system('dangerous-command')"
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "change handler",
        )

        shown = self.manager.show(proposal["id"], self.owner)

        self.assertIn(old, shown["diff"])
        self.assertIn(new, shown["diff"])
        self.assertNotIn("<redacted", shown["diff"])
        self.assertGreaterEqual(shown["raw_diff_length"], len(old) + len(new))

    def test_diff_exposes_exact_line_endings_and_eof_newline_changes(self):
        cases = (
            ("eof.py", "VALUE = 1", "VALUE = 1\n", "NO_NEWLINE", "LF"),
            (
                "crlf.py", "FIRST = 1\nSECOND = 2\n",
                "FIRST = 1\r\nSECOND = 2\r\n", "LF", "CRLF",
            ),
            ("cr.py", "FIRST = 1\rSECOND = 2\r", "FIRST = 1\nSECOND = 2\n", "CR", "LF"),
        )
        for filename, old, new, old_marker, new_marker in cases:
            with self.subTest(filename=filename):
                target = self.project / filename
                target.write_bytes(old.encode("utf-8"))
                proposal = self.manager.propose_patch(
                    self.owner, target, old, new, "change exact line endings",
                )

                shown = self.manager.show(proposal["id"], self.owner)

                self.assertTrue(shown["diff"])
                self.assertIn("@@ line endings @@", shown["diff"])
                self.assertIn(f"- old: {old_marker}", shown["diff"])
                self.assertIn(f"+ new: {new_marker}", shown["diff"])
                self.assertGreater(shown["raw_diff_length"], 0)

    def test_line_ending_only_diff_does_not_expose_sensitive_python_value(self):
        target = self.project / "sensitive_eof.py"
        secret = "PLAIN-SECRET-LEAK"
        old = f'config={{"apiKey": get_secret("{secret}", scope="prod")}}'
        new = old + "\n"
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "add newline to sensitive config",
        )

        shown = self.manager.show(proposal["id"], self.owner)

        self.assertNotIn(secret, shown["diff"])
        self.assertNotIn("get_secret", shown["diff"])
        self.assertNotIn("scope=", shown["diff"])
        self.assertIn("@@ line endings @@", shown["diff"])
        self.assertGreater(shown["raw_diff_length"], 0)

    def test_sensitive_python_assignment_is_still_redacted(self):
        target = self.project / "settings.py"
        old = "api_key = 'SOURCE-SECRET-OLD'"
        new = "api_key = 'SOURCE-SECRET-NEW'"
        target.write_text(old, encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate source credential",
        )

        shown = self.manager.show(proposal["id"], self.owner)

        self.assertNotIn("SOURCE-SECRET-OLD", shown["diff"])
        self.assertNotIn("SOURCE-SECRET-NEW", shown["diff"])
        self.assertIn("<redacted", shown["diff"])

    def test_prefixed_sensitive_python_assignments_are_redacted(self):
        target = self.project / "credentials.py"
        identifiers = (
            "OPENAI_API_KEY", "AWS_ACCESS_KEY", "SSH_PRIVATE_KEY",
            "SIGNING_SECRET_KEY", "CLIENT_SECRET", "AUTH_TOKEN",
            "BOT_TOKEN", "DATABASE_PASSWORD", "WEBHOOK_SECRET",
            "openaiApiKey", "botToken", "databasePassword",
            "privateKeyPem", "OPENAI_API_KEY_V2",
        )
        old_lines = [
            f'{identifier} = "OLD-{index}-CREDENTIAL"'
            for index, identifier in enumerate(identifiers)
        ]
        new_lines = [
            f'{identifier} = "NEW-{index}-CREDENTIAL"'
            for index, identifier in enumerate(identifiers)
        ]
        old = "\n".join(old_lines)
        new = "\n".join(new_lines)
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate prefixed credentials",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        for index, identifier in enumerate(identifiers):
            self.assertIn(identifier, rendered)
            self.assertNotIn(f"OLD-{index}-CREDENTIAL", rendered)
            self.assertNotIn(f"NEW-{index}-CREDENTIAL", rendered)
        self.assertGreaterEqual(rendered.count("<redacted"), len(identifiers) * 2)

    def test_prefixed_sensitive_json_keys_are_redacted(self):
        target = self.project / "credentials.json"
        old = json.dumps({
            "OPENAI_API_KEY": "JSON-OPENAI-OLD",
            "BOT_TOKEN": "JSON-BOT-OLD",
            "DATABASE_PASSWORD": "JSON-DATABASE-OLD",
            "CLIENT_SECRET": "JSON-CLIENT-OLD",
            "openaiApiKey": "JSON-CAMEL-API-OLD",
            "privateKeyPem": "JSON-PRIVATE-PEM-OLD",
            "OPENAI_API_KEY_V2": "JSON-VERSIONED-OLD",
            "HANDLER": "ordinary-handler-old",
            "TOKENIZER": "ordinary-tokenizer-old",
        })
        new = (
            old.replace("-OLD", "-NEW")
            .replace("ordinary-handler-old", "ordinary-handler-new")
            .replace("ordinary-tokenizer-old", "ordinary-tokenizer-new")
        )
        target.write_text(old, encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate JSON credentials",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        for value in (
            "JSON-OPENAI", "JSON-BOT", "JSON-DATABASE", "JSON-CLIENT",
            "JSON-CAMEL-API", "JSON-PRIVATE-PEM", "JSON-VERSIONED",
        ):
            self.assertNotIn(value, rendered)
        self.assertIn("ordinary-handler-old", rendered)
        self.assertIn("ordinary-handler-new", rendered)
        self.assertIn("ordinary-tokenizer-old", rendered)
        self.assertIn("ordinary-tokenizer-new", rendered)

    def test_obvious_credential_values_are_redacted_under_ordinary_keys(self):
        target = self.project / "public_values.py"
        old_credentials = (
            "sk-abcdefghijklmnopqrstuvwx",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
            "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ_123456",
            "xoxb-1234567890-ABCDEFGHIJ",
            "AKIA1234567890ABCDEF",
            "AIza1234567890abcdefghijklmnop",
        )
        new_credentials = tuple(
            value[:-1] + ("Z" if value[-1] != "Z" else "Y")
            for value in old_credentials
        )
        old = "\n".join(
            f'PUBLIC_VALUE_{index} = "{value}"'
            for index, value in enumerate(old_credentials)
        )
        new = "\n".join(
            f'PUBLIC_VALUE_{index} = "{value}"'
            for index, value in enumerate(new_credentials)
        )
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate opaque values",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        for credential in old_credentials + new_credentials:
            self.assertNotIn(credential, rendered)
        self.assertGreaterEqual(
            rendered.count("<redacted credential"), len(old_credentials) * 2,
        )

    def test_credential_scan_runs_after_sensitive_expression_redaction(self):
        target = self.project / "credential_tail.py"
        old_comment_token = "sk-abcdefghijklmnopqrstuvwxyz123456"
        new_comment_token = "sk-zyxwvutsrqponmlkjihgfedcba654321"
        old_handler_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
        new_handler_token = "ghp_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        old = "\n".join((
            f'password = "plain old" # old comment remains {old_comment_token}',
            f'password="plain second old"; HANDLER="{old_handler_token}"',
        ))
        new = "\n".join((
            f'password = "plain new" # new comment remains {new_comment_token}',
            f'password="plain second new"; HANDLER="{new_handler_token}"',
        ))
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate credentials beside passwords",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        for hidden in (
            old_comment_token, new_comment_token,
            old_handler_token, new_handler_token,
            "plain old", "plain new", "plain second old", "plain second new",
        ):
            self.assertNotIn(hidden, rendered)
        for visible in (
            "# old comment remains", "# new comment remains", "HANDLER=",
        ):
            self.assertIn(visible, rendered)
        self.assertIn("<redacted expression length=", rendered)
        self.assertGreaterEqual(rendered.count("<redacted credential sha256="), 4)

        masked_once = self.manager._mask_credential_values(rendered)
        self.assertEqual(masked_once, self.manager._mask_credential_values(masked_once))

    def test_sensitive_key_parser_uses_components_not_substrings(self):
        for key in (
            "openaiApiKey", "botToken", "databasePassword", "privateKeyPem",
            "OPENAI_API_KEY_V2", "some.credentials.path", "service-auth-token",
        ):
            with self.subTest(key=key):
                self.assertTrue(self.manager._is_sensitive_key(key))
        for key in ("TOKENIZER", "HANDLER", "secretary", "keychain"):
            with self.subTest(key=key):
                self.assertFalse(self.manager._is_sensitive_key(key))

    def test_mixed_quote_dict_and_subscript_sensitive_values_are_redacted(self):
        target = self.project / "mixed_credentials.py"
        old = "\n".join((
            'config = {"api_key": \'plain-api-secret-old\'}',
            'other = {\'botToken\': "plain-bot-secret-old"}',
            'config["databasePassword"] = \'plain-db-secret-old\'',
            'config[\'clientSecret\'] = "plain-client-secret-old"',
            'ordinary = {"HANDLER": \'visible-handler-old\'}',
            'config[\'TOKENIZER\'] = "visible-tokenizer-old"',
        ))
        new = "\n".join((
            "config = {'api_key': \"plain-api-secret-new\"}",
            "other = {\"botToken\": 'plain-bot-secret-new'}",
            "config['databasePassword'] = \"plain-db-secret-new\"",
            'config["clientSecret"] = \'plain-client-secret-new\'',
            "ordinary = {'HANDLER': \"visible-handler-new\"}",
            'config["TOKENIZER"] = \'visible-tokenizer-new\'',
        ))
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate mixed quote credentials",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        for secret in (
            "plain-api-secret", "plain-bot-secret", "plain-db-secret",
            "plain-client-secret",
        ):
            self.assertNotIn(secret, rendered)
        for visible in (
            "visible-handler-old", "visible-handler-new",
            "visible-tokenizer-old", "visible-tokenizer-new",
        ):
            self.assertIn(visible, rendered)

    def test_sensitive_dict_values_and_call_keywords_redact_full_expressions(self):
        target = self.project / "container_credentials.py"
        old_dict_rhs = (
            'get_secret(\n        "PLAIN-DICT-SECRET-OLD",\n'
            '        scope="prod-old",\n    )'
        )
        new_dict_rhs = (
            "get_secret(\n        'PLAIN-DICT-SECRET-NEW',\n"
            "        scope='prod-new',\n    )"
        )
        old_keyword_rhs = (
            'build_secret(\n        "PLAIN-KEYWORD-SECRET-OLD",\n'
            '        source="vault-old",\n    )'
        )
        new_keyword_rhs = (
            "build_secret(\n        'PLAIN-KEYWORD-SECRET-NEW',\n"
            "        source='vault-new',\n    )"
        )
        old = "\n".join((
            "config = {",
            f'    "apiKey": {old_dict_rhs},',
            '    "HANDLER": build_handler("visible-dict-old"),',
            "}",
            "client = configure(",
            f"    authToken={old_keyword_rhs},",
            '    HANDLER=build_handler("visible-keyword-old"),',
            ")",
        ))
        new = "\n".join((
            "config = {",
            f"    'apiKey': {new_dict_rhs},",
            "    'HANDLER': build_handler('visible-dict-new'),",
            "}",
            "client = configure(",
            f"    authToken={new_keyword_rhs},",
            "    HANDLER=build_handler('visible-keyword-new'),",
            ")",
        ))
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate container credentials",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        for hidden in (
            "PLAIN-DICT-SECRET", "PLAIN-KEYWORD-SECRET", "get_secret(",
            "build_secret(", "scope=", "source=", "prod-old", "prod-new",
            "vault-old", "vault-new",
        ):
            self.assertNotIn(hidden, rendered)
        for visible in (
            '"apiKey": <redacted expression length=',
            "'apiKey': <redacted expression length=",
            "authToken=<redacted expression length=",
            'build_handler("visible-dict-old")',
            "build_handler('visible-dict-new')",
            'build_handler("visible-keyword-old")',
            "build_handler('visible-keyword-new')",
        ):
            self.assertIn(visible, rendered)
        for rhs in (
            old_dict_rhs, new_dict_rhs, old_keyword_rhs, new_keyword_rhs,
        ):
            self.assertIn(f"length={len(rhs.encode('utf-8'))}", rendered)
            self.assertIn(sha256(rhs.encode("utf-8")).hexdigest(), rendered)

    def test_sensitive_function_async_and_lambda_defaults_are_fully_redacted(self):
        target = self.project / "callable_defaults.py"

        def source(version):
            quote = '"' if version == "old" else "'"
            sync_api = (
                f"get_secret(\n        {quote}SYNC-API-{version.upper()}{quote},\n"
                f"        scope={quote}sync-{version}{quote},\n    )"
            )
            sync_openai = (
                "{\n"
                f"        {quote}parts{quote}: "
                f"[{quote}SYNC-OPENAI-{version.upper()}{quote}, "
                f"{quote}sync-tail-{version}{quote}],\n"
                "    }"
            )
            async_auth = (
                f"get_secret(\n        {quote}ASYNC-AUTH-{version.upper()}{quote},\n"
                f"        scope={quote}async-{version}{quote},\n    )"
            )
            lambda_openai = (
                f"get_secret(\n        {quote}LAMBDA-OPENAI-{version.upper()}{quote},\n"
                f"        scope={quote}lambda-{version}{quote},\n    )"
            )
            lambda_auth = (
                "{\n"
                f"        {quote}parts{quote}: "
                f"[{quote}LAMBDA-AUTH-{version.upper()}{quote}, "
                f"{quote}lambda-tail-{version}{quote}],\n"
                "    }"
            )
            content = "\n".join((
                "def sync_factory(",
                "    required_posonly, /,",
                f"    apiKey={sync_api},",
                f"    HANDLER=build_handler({quote}VISIBLE-SYNC-{version.upper()}{quote}),",
                "    *, required_kw,",
                f"    openaiApiKey={sync_openai},",
                f"    formatter={{{quote}mode{quote}: {quote}VISIBLE-FORMAT-{version.upper()}{quote}}},",
                "):",
                "    return required_posonly, required_kw",
                "",
                "async def async_factory(",
                "    required,",
                f"    HANDLER=build_handler({quote}VISIBLE-ASYNC-{version.upper()}{quote}),",
                f"    authToken={async_auth},",
                "    *, required_kw,",
                f"    formatter={quote}VISIBLE-ASYNC-FORMAT-{version.upper()}{quote},",
                "):",
                "    return required, required_kw",
                "",
                "factory = (",
                "    lambda required_posonly, /,",
                f"    openaiApiKey={lambda_openai},",
                f"    HANDLER=build_handler({quote}VISIBLE-LAMBDA-{version.upper()}{quote}),",
                "    *, required_kw,",
                f"    authToken={lambda_auth},",
                f"    formatter={quote}VISIBLE-LAMBDA-FORMAT-{version.upper()}{quote}:",
                "    (required_posonly, required_kw)",
                ")",
            ))
            return content, (
                sync_api, sync_openai, async_auth, lambda_openai, lambda_auth,
            )

        old, old_sensitive_defaults = source("old")
        new, new_sensitive_defaults = source("new")
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate callable defaults",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        for hidden in (
            "SYNC-API-", "SYNC-OPENAI-", "ASYNC-AUTH-",
            "LAMBDA-OPENAI-", "LAMBDA-AUTH-", "sync-tail-", "lambda-tail-",
            "scope=", "get_secret(",
        ):
            self.assertNotIn(hidden, rendered)
        for sensitive_parameter in ("apiKey", "openaiApiKey", "authToken"):
            self.assertIn(
                f"{sensitive_parameter}=<redacted expression length=", rendered,
            )
        for visible in (
            "VISIBLE-SYNC-OLD", "VISIBLE-SYNC-NEW", "VISIBLE-FORMAT-OLD",
            "VISIBLE-FORMAT-NEW", "VISIBLE-ASYNC-OLD", "VISIBLE-ASYNC-NEW",
            "VISIBLE-ASYNC-FORMAT-OLD", "VISIBLE-ASYNC-FORMAT-NEW",
            "VISIBLE-LAMBDA-OLD", "VISIBLE-LAMBDA-NEW",
            "VISIBLE-LAMBDA-FORMAT-OLD", "VISIBLE-LAMBDA-FORMAT-NEW",
        ):
            self.assertIn(visible, rendered)
        for default in old_sensitive_defaults + new_sensitive_defaults:
            self.assertIn(f"length={len(default.encode('utf-8'))}", rendered)
            self.assertIn(sha256(default.encode("utf-8")).hexdigest(), rendered)

    @staticmethod
    def _same_line_sensitive_source(count):
        dict_line = '配置 = {"普通键": "可见字典", ' + ", ".join(
            f'"api_key_dict_{index}": "字典秘密{index}"'
            for index in range(count)
        ) + "}"
        call_line = '结果 = configure(普通参数="可见调用", ' + ", ".join(
            f'api_key_call_{index}="调用秘密{index}"'
            for index in range(count)
        ) + ")"
        default_line = 'def 工厂(普通参数="可见默认", ' + ", ".join(
            f'api_key_default_{index}="默认秘密{index}"'
            for index in range(count)
        ) + "): return 普通参数"
        return "\n".join((dict_line, call_line, default_line))

    def test_many_non_ascii_same_line_sensitive_spans_are_exactly_redacted(self):
        count = 120
        source = self._same_line_sensitive_source(count)

        rendered = self.manager._redact_python_content(source)

        for hidden in ("字典秘密", "调用秘密", "默认秘密"):
            self.assertNotIn(hidden, rendered)
        for visible in ("配置", "普通键", "可见字典", "可见调用", "可见默认"):
            self.assertIn(visible, rendered)
        self.assertEqual(rendered.count("<redacted expression length="), count * 3)
        self.assertIn('"api_key_dict_119": <redacted expression', rendered)
        self.assertIn("api_key_call_119=<redacted expression", rendered)
        self.assertIn("api_key_default_119=<redacted expression", rendered)

    def test_same_line_sensitive_span_redaction_scales_near_linearly(self):
        def best_elapsed(count):
            source = self._same_line_sensitive_source(count)
            measurements = []
            for _ in range(2):
                started = time.perf_counter()
                self.manager._redact_python_content(source)
                measurements.append(time.perf_counter() - started)
            return min(measurements)

        small_elapsed = best_elapsed(300)
        large_elapsed = best_elapsed(600)

        self.assertLess(
            large_elapsed,
            (small_elapsed * 3.2) + 0.05,
            f"same-line redaction scaled poorly: {small_elapsed:.4f}s -> "
            f"{large_elapsed:.4f}s",
        )

    def test_annotated_sensitive_assignments_preserve_type_and_redact_rhs(self):
        target = self.project / "annotated_credentials.py"
        old = "\n".join((
            'password: str = "plain password old with spaces"',
            'openaiApiKey: Final[str] = get_secret("plain api old", scope="prod")',
            'authToken: SecretStr = SecretStr("plain auth old value")',
            'clientSecret = get_secret("plain client old", fallback="none")',
            "HANDLER: Callable[[str], str] = build_handler(old_arg, mode='safe')",
            'TOKENIZER: Final[str] = "visible-tokenizer-old"',
        ))
        new = "\n".join((
            "password: str = 'plain password new with spaces'",
            "openaiApiKey: Final[str] = get_secret('plain api new', scope='stage')",
            "authToken: SecretStr = SecretStr('plain auth new value')",
            "clientSecret = get_secret('plain client new', fallback='none')",
            'HANDLER: Callable[[str], str] = build_handler(new_arg, mode="safe")',
            "TOKENIZER: Final[str] = 'visible-tokenizer-new'",
        ))
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate annotated credentials",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        for hidden_fragment in (
            "plain password", "plain api", "plain auth", "plain client",
            "get_secret(", "SecretStr(", "scope=", "fallback=",
        ):
            self.assertNotIn(hidden_fragment, rendered)
        self.assertIn("password: str = <redacted expression length=", rendered)
        self.assertIn(
            "openaiApiKey: Final[str] = <redacted expression length=", rendered,
        )
        self.assertIn(
            "authToken: SecretStr = <redacted expression length=", rendered,
        )
        self.assertIn("clientSecret = <redacted expression length=", rendered)
        for visible in (
            "HANDLER: Callable[[str], str] = build_handler(old_arg, mode='safe')",
            'HANDLER: Callable[[str], str] = build_handler(new_arg, mode="safe")',
            'TOKENIZER: Final[str] = "visible-tokenizer-old"',
            "TOKENIZER: Final[str] = 'visible-tokenizer-new'",
        ):
            self.assertIn(visible, rendered)

    def test_multiline_sensitive_rhs_expressions_are_fully_redacted(self):
        target = self.project / "multiline_credentials.py"
        old_api_rhs = '(\n    "paren secret old "\n    "continued old"\n)'
        new_api_rhs = '(\n    "paren secret new "\n    "continued new"\n)'
        old = "\n".join((
            f"apiKey = {old_api_rhs}",
            'authToken = "backslash secret old " ' + chr(92),
            '    "continued token old"',
            "databasePassword: SecretStr = get_secret(",
            '    "function secret old",',
            '    scope="production old",',
            ")",
            "clientSecret = {",
            '    "parts": ["dict secret old", "second old"],',
            "}",
            "HANDLER = (",
            "    build_handler(old_arg,",
            '                  mode="visible old")',
            ")",
        ))
        new = "\n".join((
            f"apiKey = {new_api_rhs}",
            'authToken = "backslash secret new " ' + chr(92),
            '    "continued token new"',
            "databasePassword: SecretStr = get_secret(",
            '    "function secret new",',
            '    scope="staging new",',
            ")",
            "clientSecret = {",
            '    "parts": ["dict secret new", "second new"],',
            "}",
            "HANDLER = (",
            "    build_handler(new_arg,",
            '                  mode="visible new")',
            ")",
        ))
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate multiline credentials",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        for hidden in (
            "paren secret", "continued old", "continued new",
            "backslash secret", "continued token", "get_secret(",
            "function secret", "scope=", '"parts"', "dict secret", "second old",
            "second new",
        ):
            self.assertNotIn(hidden, rendered)
        for lhs in (
            "apiKey = <redacted expression", "authToken = <redacted expression",
            "databasePassword: SecretStr = <redacted expression",
            "clientSecret = <redacted expression",
        ):
            self.assertIn(lhs, rendered)
        for visible in (
            "build_handler(old_arg,", 'mode="visible old"',
            "build_handler(new_arg,", 'mode="visible new"',
        ):
            self.assertIn(visible, rendered)
        for rhs in (old_api_rhs, new_api_rhs):
            self.assertIn(f"length={len(rhs.encode('utf-8'))}", rendered)
            self.assertIn(sha256(rhs.encode("utf-8")).hexdigest(), rendered)

    def test_invalid_python_with_sensitive_assignment_fails_closed(self):
        target = self.project / "broken_credentials.py"
        old = 'apiKey = (\n    "syntax-error-secret-old"\n'
        new = 'apiKey = (\n    "syntax-error-secret-new"\n'
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "repair incomplete credential code",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        self.assertNotIn("syntax-error-secret-old", rendered)
        self.assertNotIn("syntax-error-secret-new", rendered)
        self.assertNotIn("apiKey", rendered)
        self.assertIn("<redacted length=", rendered)

    def test_invalid_non_sensitive_python_remains_reviewable(self):
        target = self.project / "broken_handler.py"
        old = 'HANDLER = (\n    "visible-handler-old"\n'
        new = 'HANDLER = (\n    "visible-handler-new"\n'
        target.write_bytes(old.encode("utf-8"))
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "repair incomplete handler code",
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        self.assertIn("visible-handler-old", rendered)
        self.assertIn("visible-handler-new", rendered)

    def test_private_key_diff_shows_only_lengths_and_sha256_digests(self):
        private_old = (
            "-----BEGIN PRIVATE KEY-----\n"
            "VERY-SECRET-BASE64-OLD\n"
            "-----END PRIVATE KEY-----\n"
        )
        private_new = private_old.replace("BASE64-OLD", "BASE64-NEW")

        for filename in ("server.pem", "ordinary.txt"):
            with self.subTest(filename=filename):
                target = self.project / filename
                target.write_bytes(private_old.encode("utf-8"))
                proposal = self.manager.propose_patch(
                    self.owner, target, private_old, private_new, "rotate private key"
                )

                rendered = self.manager.show(proposal["id"], self.owner)["diff"]

                self.assertNotIn("PRIVATE KEY", rendered)
                self.assertNotIn("VERY-SECRET-BASE64-OLD", rendered)
                self.assertNotIn("VERY-SECRET-BASE64-NEW", rendered)
                for content in (private_old, private_new):
                    self.assertIn(f"length={len(content.encode('utf-8'))}", rendered)
                    self.assertIn(sha256(content.encode("utf-8")).hexdigest(), rendered)

    def test_quoted_json_secret_values_are_redacted_from_text_diff(self):
        target = self.project / "settings.json"
        old = json.dumps({
            "api_key": "JSON-API-SECRET-OLD",
            "token": "JSON-TOKEN-OLD",
            "password": "JSON-PASSWORD-OLD",
            "secret": "JSON-SECRET-OLD",
        })
        new = old.replace("-OLD", "-NEW")
        target.write_text(old, encoding="utf-8")
        proposal = self.manager.propose_patch(
            self.owner, target, old, new, "rotate application credentials"
        )

        rendered = self.manager.show(proposal["id"], self.owner)["diff"]

        for sensitive in (
            "JSON-API-SECRET", "JSON-TOKEN", "JSON-PASSWORD", "JSON-SECRET",
        ):
            self.assertNotIn(sensitive, rendered)
        self.assertGreaterEqual(rendered.count("<redacted"), 8)

    def test_reason_is_redacted_in_responses_but_exact_in_private_record(self):
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        reason = (
            'rotate {"api_key": "REASON-API-SECRET", '
            '"token": "REASON-TOKEN-SECRET", "password": "REASON-PASSWORD"}'
        )

        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", reason
        )
        shown = self.manager.show(proposal["id"], self.owner)
        listed = self.manager.list_visible(self.owner)

        for visible in (proposal["reason"], shown["reason"], listed[0]["reason"]):
            self.assertNotIn("REASON-API-SECRET", visible)
            self.assertNotIn("REASON-TOKEN-SECRET", visible)
            self.assertNotIn("REASON-PASSWORD", visible)
            self.assertIn("<redacted", visible)
        proposal_path = self.state / "proposals" / proposal["id"] / "proposal.json"
        self.assertEqual(
            json.loads(proposal_path.read_text(encoding="utf-8"))["reason"],
            reason,
        )
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(proposal_path.stat().st_mode), 0o600)

    def test_private_key_reason_block_is_replaced_by_length_and_sha256(self):
        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        private_block = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "REASON-PRIVATE-BASE64-BODY\n"
            "-----END RSA PRIVATE KEY-----"
        )
        reason = f"  rotate credential:\n{private_block}\nkeep audit context  "

        proposal = self.manager.propose_patch(
            self.owner, target, "before", "after", reason
        )
        visible_reasons = (
            proposal["reason"],
            self.manager.show(proposal["id"], self.owner)["reason"],
            self.manager.list_visible(self.owner)[0]["reason"],
        )

        for visible in visible_reasons:
            self.assertNotIn("REASON-PRIVATE-BASE64-BODY", visible)
            self.assertNotIn("PRIVATE KEY", visible)
            self.assertIn(
                f"length={len(private_block.encode('utf-8'))}", visible
            )
            self.assertIn(
                sha256(private_block.encode("utf-8")).hexdigest(), visible
            )
        proposal_path = self.state / "proposals" / proposal["id"] / "proposal.json"
        self.assertEqual(
            json.loads(proposal_path.read_text(encoding="utf-8"))["reason"],
            reason,
        )

    def test_agent_tool_stages_change_without_writing_source(self):
        import ga

        target = self.project / "ga.py"
        target.write_text("before", encoding="utf-8")
        parent = SimpleNamespace(
            active_task={"conversation_identity": self.owner.as_dict()},
            change_approval=self.manager,
            verbose=False,
            task_dir=None,
        )
        handler = ga.GenericAgentHandler(parent, cwd=str(self.project))

        outcome = self.exhaust(handler.do_source_change_propose(
            {
                "path": str(target), "old_content": "before",
                "new_content": "after", "reason": "password=AGENT-REASON-SECRET",
            },
            SimpleNamespace(content=""),
        ))

        self.assertEqual(outcome.data["status"], "pending_approval")
        self.assertTrue(outcome.data["proposal_id"].startswith("SC-"))
        self.assertNotIn("AGENT-REASON-SECRET", json.dumps(outcome.data))
        self.assertEqual(target.read_text(encoding="utf-8"), "before")


if __name__ == "__main__":
    unittest.main()
