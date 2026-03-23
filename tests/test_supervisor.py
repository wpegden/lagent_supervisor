import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import supervisor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import init_formalization_project


class DummyAdapter(supervisor.ProviderAdapter):
    def __init__(self, cfg, role, config, state, command):
        super().__init__(cfg, role, config, state)
        self._command = list(command)

    def build_initial_command(self):
        return list(self._command)

    def build_continue_command(self):
        return list(self._command)


class SupervisorTestCase(unittest.TestCase):
    def make_config(
        self,
        repo_path: Path,
        *,
        start_phase: str = "proof_formalization",
        chat_root_dir: Path | None = None,
        git_remote_url: str | None = None,
        session_name: str | None = None,
        startup_timeout_seconds: float = 15.0,
        burst_timeout_seconds: float = 7200.0,
        kill_windows_after_capture: bool = True,
    ) -> supervisor.Config:
        return supervisor.Config(
            repo_path=repo_path,
            goal_file=repo_path / "GOAL.md",
            state_dir=repo_path / ".agent-supervisor",
            worker=supervisor.ProviderConfig(provider="claude", model=None, extra_args=[]),
            reviewer=supervisor.ProviderConfig(provider="claude", model=None, extra_args=[]),
            tmux=supervisor.TmuxConfig(
                session_name=session_name or f"lagent-test-{uuid.uuid4().hex[:8]}",
                dashboard_window_name="dashboard",
                kill_windows_after_capture=kill_windows_after_capture,
            ),
            workflow=supervisor.WorkflowConfig(
                start_phase=start_phase,
                sorry_mode="default",
                paper_tex_path=repo_path / "paper.tex",
                approved_axioms_path=repo_path / "APPROVED_AXIOMS.json",
                human_input_path=repo_path / "HUMAN_INPUT.md",
                input_request_path=repo_path / "INPUT_REQUEST.md",
            ),
            chat=supervisor.ChatConfig(
                root_dir=chat_root_dir or (repo_path.parent / "lagent-chats"),
                repo_name=supervisor.sanitize_repo_name(repo_path.name),
                public_base_url="https://packer.math.cmu.edu/lagent-chats/",
            ),
            git=supervisor.GitConfig(
                remote_url=git_remote_url,
                remote_name="origin",
                branch="main",
                author_name="Test User",
                author_email="test@example.com",
            ),
            max_cycles=0,
            sleep_seconds=0.0,
            startup_timeout_seconds=startup_timeout_seconds,
            burst_timeout_seconds=burst_timeout_seconds,
        )

    def make_repo(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="lagent supervisor "))
        self.addCleanup(shutil.rmtree, root, True)
        repo_path = root / "repo with spaces"
        repo_path.mkdir(parents=True, exist_ok=True)
        (repo_path / "GOAL.md").write_text("# Goal\n\nTest goal.\n", encoding="utf-8")
        (repo_path / "paper.tex").write_text("\\section{Test}\nA tiny paper.\n", encoding="utf-8")
        return repo_path

    def cleanup_tmux_session(self, session_name: str) -> None:
        if shutil.which("tmux"):
            supervisor.tmux_cmd("kill-session", "-t", session_name, check=False)

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)


class CommandTests(SupervisorTestCase):
    def test_codex_resume_uses_resume_safe_flags(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        adapter = supervisor.CodexAdapter(
            supervisor.ProviderConfig(
                provider="codex",
                model="gpt-5.4",
                extra_args=["--config", 'model_reasoning_effort="xhigh"'],
            ),
            "worker",
            config,
            {},
        )

        initial = adapter.build_initial_command()
        continued = adapter.build_continue_command()

        self.assertIn("--dangerously-bypass-approvals-and-sandbox", initial)
        self.assertIn("--color", initial)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", continued)
        self.assertNotIn("--ask-for-approval", continued)
        self.assertNotIn("--sandbox", continued)
        self.assertNotIn("--color", continued)

    def test_role_state_is_scoped_by_provider(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = {}

        codex_worker = supervisor.CodexAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "worker",
            config,
            state,
        )
        self.assertTrue(codex_worker.needs_initial_run())
        codex_worker.mark_initialized()

        same_provider_worker = supervisor.CodexAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "worker",
            config,
            state,
        )
        other_provider_worker = supervisor.ClaudeAdapter(
            supervisor.ProviderConfig(provider="claude", model="opus", extra_args=[]),
            "worker",
            config,
            state,
        )

        self.assertFalse(same_provider_worker.needs_initial_run())
        self.assertTrue(other_provider_worker.needs_initial_run())

    def test_reviewer_prompt_can_omit_terminal_output_for_chat_export(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        prompt = supervisor.build_reviewer_prompt(
            config,
            {"review_log": []},
            "planning",
            "very long terminal output",
            '{"status":"NOT_STUCK"}',
            {"build": {"ok": True}, "sorries": {"count": 0}},
            True,
            include_terminal_output=False,
        )
        self.assertIn("omitted from the web transcript", prompt)
        self.assertNotIn("very long terminal output", prompt)

    def test_worker_prompt_mentions_commit_and_push_when_git_remote_is_configured(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, git_remote_url="/tmp/example-remote.git")
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True, capture_output=True, text=True)
        subprocess.run(["git", "remote", "add", "origin", "/tmp/example-remote.git"], cwd=repo_path, check=True, capture_output=True, text=True)

        prompt = supervisor.build_worker_prompt(config, {}, "planning", True)

        self.assertIn("create a non-empty git commit", prompt)
        self.assertIn("git push origin HEAD:main", prompt)

    def test_worker_prompt_mentions_provider_context_first(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config = supervisor.Config(
            repo_path=config.repo_path,
            goal_file=config.goal_file,
            state_dir=config.state_dir,
            worker=supervisor.ProviderConfig(
                provider="codex",
                model="gpt-5.4",
                extra_args=["--config", 'model_reasoning_effort="xhigh"'],
            ),
            reviewer=config.reviewer,
            tmux=config.tmux,
            workflow=config.workflow,
            chat=config.chat,
            git=config.git,
            max_cycles=config.max_cycles,
            sleep_seconds=config.sleep_seconds,
            startup_timeout_seconds=config.startup_timeout_seconds,
            burst_timeout_seconds=config.burst_timeout_seconds,
        )

        prompt = supervisor.build_worker_prompt(config, {}, "proof_formalization", False)

        self.assertIn(".agents/skills/lean-formalizer/SKILL.md", prompt)
        self.assertIn("read or reread the installed `lean-formalizer` skill", prompt)
        self.assertIn("Follow the Lean-search, naming, proof-planning, and tool-usage suggestions", prompt)
        self.assertIn("paper-facing interface", prompt)
        self.assertIn("separate support files", prompt)
        self.assertIn("short wrappers around results proved elsewhere", prompt)

    def test_proof_phase_reviewer_prompt_prefers_support_file_refactors(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        prompt = supervisor.build_reviewer_prompt(
            config,
            {"review_log": []},
            "proof_formalization",
            "worker terminal output",
            '{"status":"NOT_STUCK"}',
            {"build": {"ok": True}, "sorries": {"count": 0}},
            False,
        )

        self.assertIn("paper-facing", prompt)
        self.assertIn("separate support files would be cleaner", prompt)


class ArtifactFallbackTests(SupervisorTestCase):
    def test_malformed_artifact_falls_back_to_last_matching_json_in_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lagent artifacts ") as tmpdir:
            artifact_path = Path(tmpdir) / "review_decision.json"
            artifact_path.write_text("{ not valid json", encoding="utf-8")
            captured = """
noise
{"other": 1}
more noise
```json
{"decision": "CONTINUE", "confidence": 0.9, "reason": "ok", "next_prompt": "keep going"}
```
"""
            data = supervisor.load_json_artifact_with_fallback(artifact_path, captured, "decision")
            self.assertEqual(data["decision"], "CONTINUE")


class WorkflowTests(SupervisorTestCase):
    def test_ensure_repo_files_respects_phase_artifacts(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase="paper_check")

        supervisor.ensure_repo_files(config, "paper_check")

        self.assertTrue((repo_path / "TASKS.md").exists())
        self.assertTrue((repo_path / "PAPERNOTES.md").exists())
        self.assertTrue((repo_path / "APPROVED_AXIOMS.json").exists())
        self.assertFalse((repo_path / "PLAN.md").exists())

        supervisor.ensure_repo_files(config, "planning")
        self.assertTrue((repo_path / "PLAN.md").exists())

    def test_run_validation_flags_unapproved_axioms_and_disallowed_sorries(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase="proof_formalization")
        (repo_path / "lakefile.toml").write_text(
            'name = "T"\nversion = "0.1.0"\ndefaultTargets = ["t"]\n\n[[lean_lib]]\nname = "T"\n',
            encoding="utf-8",
        )
        (repo_path / "lean-toolchain").write_text("leanprover/lean4:v4.28.0\n", encoding="utf-8")
        (repo_path / "T.lean").write_text("import T.Helper\n", encoding="utf-8")
        (repo_path / "T").mkdir(exist_ok=True)
        (repo_path / "T" / "Helper.lean").write_text(
            "axiom badAxiom : Nat\n\ntheorem helper : Nat := by\n  sorry\n",
            encoding="utf-8",
        )
        (repo_path / "PaperDefinitions.lean").write_text("def foo : Nat := 0\n", encoding="utf-8")
        (repo_path / "PaperTheorems.lean").write_text("theorem stated : True := by\n  sorry\n", encoding="utf-8")

        supervisor.ensure_repo_files(config, "proof_formalization")
        summary = supervisor.run_validation(config, "proof_formalization", 1)

        self.assertFalse(summary["policy_ok"])
        self.assertTrue(summary["axioms"]["unapproved"])
        self.assertTrue(summary["sorry_policy"]["disallowed_entries"])

    def test_human_input_is_only_consumed_after_new_request(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase="planning")
        supervisor.ensure_repo_files(config, "planning")
        config.workflow.human_input_path.write_text("old input\n", encoding="utf-8")
        time.sleep(0.02)
        config.workflow.input_request_path.write_text("new request\n", encoding="utf-8")

        state = {"awaiting_human_input": True, "phase": "planning", "roles": {}, "review_log": []}
        self.assertFalse(supervisor.maybe_consume_human_input(config, state))

        time.sleep(0.02)
        config.workflow.human_input_path.write_text("fresh input\n", encoding="utf-8")
        self.assertTrue(supervisor.maybe_consume_human_input(config, state))
        self.assertEqual(state["last_human_input"], "fresh input")

    def test_chat_event_export_builds_manifest_and_repo_files(self) -> None:
        repo_path = self.make_repo()
        chat_root = repo_path.parent / "chat site"
        config = self.make_config(repo_path, chat_root_dir=chat_root)
        state = {"phase": "planning", "cycle": 2, "awaiting_human_input": False}

        supervisor.record_chat_event(
            config,
            state,
            cycle=2,
            phase="planning",
            kind="worker_prompt",
            actor="supervisor",
            target="worker",
            content="Read the paper carefully.",
            content_type="text",
        )
        supervisor.record_chat_event(
            config,
            state,
            cycle=2,
            phase="planning",
            kind="reviewer_decision",
            actor="reviewer",
            target="supervisor",
            content={
                "phase": "planning",
                "decision": "CONTINUE",
                "confidence": 0.7,
                "reason": "Keep refining the roadmap.",
                "next_prompt": "Tighten the import plan.",
            },
            content_type="json",
        )

        self.assertTrue((chat_root / "index.html").exists())
        self.assertTrue((chat_root / "_assets" / "app.js").exists())
        self.assertTrue((chat_root / "_assets" / "styles.css").exists())
        self.assertTrue((chat_root / config.chat.repo_name / "index.html").exists())

        manifest = json.loads((chat_root / "repos.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["repos"][0]["repo_name"], config.chat.repo_name)
        self.assertEqual(manifest["repos"][0]["last_reviewer_decision"], "CONTINUE")

        meta = json.loads((chat_root / config.chat.repo_name / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["current_cycle"], 2)
        self.assertEqual(meta["last_event_kind"], "reviewer_decision")

        lines = (chat_root / config.chat.repo_name / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        first_event = json.loads(lines[0])
        self.assertEqual(first_event["kind"], "worker_prompt")


class ProviderContextTests(SupervisorTestCase):
    def test_install_personal_provider_context_files(self) -> None:
        repo_path = self.make_repo()
        home_dir = repo_path.parent / "home"
        installed = supervisor.install_personal_provider_context_files(home_dir, ["claude", "codex", "gemini"])

        self.assertIn(home_dir / ".claude" / "skills" / "lean-formalizer" / "SKILL.md", installed)
        self.assertIn(home_dir / ".codex" / "skills" / "lean-formalizer" / "SKILL.md", installed)
        self.assertIn(home_dir / ".gemini" / "GEMINI.md", installed)
        self.assertIn("lean-formalizer", (home_dir / ".claude" / "skills" / "lean-formalizer" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("Lean manuscript formalizer", (home_dir / ".codex" / "skills" / "lean-formalizer" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("Lean manuscript formalizer", (home_dir / ".gemini" / "GEMINI.md").read_text(encoding="utf-8"))

    def test_role_scope_dir_installs_provider_scoped_context_files(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        claude_scope = supervisor.role_scope_dir(config, "claude", "worker")
        codex_scope = supervisor.role_scope_dir(config, "codex", "worker")
        gemini_scope = supervisor.role_scope_dir(config, "gemini", "reviewer")

        self.assertTrue((claude_scope / ".claude" / "skills" / "lean-formalizer" / "SKILL.md").exists())
        self.assertTrue((codex_scope / ".agents" / "skills" / "lean-formalizer" / "SKILL.md").exists())
        self.assertTrue((gemini_scope / "GEMINI.md").exists())


class InitProjectTests(SupervisorTestCase):
    def test_parse_active_release_toolchain(self) -> None:
        output = """
installed toolchains
--------------------

leanprover/lean4:v4.28.0 (resolved from default 'stable')

active toolchain
----------------

leanprover/lean4:v4.28.0 (resolved from default 'stable')
Lean (version 4.28.0, x86_64-unknown-linux-gnu, commit abcdef, Release)
"""
        self.assertEqual(
            init_formalization_project.parse_active_release_toolchain(output),
            "leanprover/lean4:v4.28.0",
        )

    def test_repo_name_to_package_name(self) -> None:
        self.assertEqual(init_formalization_project.repo_name_to_package_name("connectivity-threshold-gnp"), "ConnectivityThresholdGnp")

    def test_lake_command_uses_explicit_toolchain_prefix(self) -> None:
        self.assertEqual(
            init_formalization_project.lake_command("leanprover/lean4:v4.29.0-rc6"),
            ["lake", "+leanprover/lean4:v4.29.0-rc6"],
        )
        self.assertEqual(init_formalization_project.lake_command(None), ["lake"])

    def test_initializer_default_max_cycles(self) -> None:
        self.assertEqual(init_formalization_project.DEFAULT_INIT_MAX_CYCLES, 150)

    def test_ensure_build_only_ci_workflow_rewrites_default_math_template(self) -> None:
        repo_path = self.make_repo()
        workflow_path = repo_path / ".github" / "workflows" / "lean_action_ci.yml"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            """name: Lean Action CI

on:
  push:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

jobs:
  build:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v5
      - uses: leanprover/lean-action@v1
      - uses: leanprover-community/docgen-action@v1
""",
            encoding="utf-8",
        )

        written = init_formalization_project.ensure_build_only_ci_workflow(repo_path)
        content = written.read_text(encoding="utf-8")

        self.assertEqual(written, workflow_path)
        self.assertIn("leanprover/lean-action@v1", content)
        self.assertNotIn("docgen-action", content)
        self.assertNotIn("pages: write", content)

    def test_build_config_json_uses_expected_defaults(self) -> None:
        repo_path = self.make_repo()
        spec = init_formalization_project.InitSpec(
            repo_path=repo_path,
            remote_url="git@github.com:wpegden/example.git",
            paper_source=repo_path / "paper.tex",
            paper_dest_rel=Path("paper/paper.tex"),
            config_path=repo_path.parent / "example.json",
            package_name="Example",
            goal_file_name="GOAL.md",
            branch="main",
            author_name="leanagent",
            author_email="leanagent@packer.math.cmu.edu",
            max_cycles=3,
            session_name="example-agents",
            kill_windows_after_capture=False,
            worker_provider="codex",
            reviewer_provider="claude",
        )

        data = init_formalization_project.build_config_json(spec)

        self.assertEqual(data["workflow"]["start_phase"], "paper_check")
        self.assertEqual(data["workflow"]["paper_tex_path"], "paper/paper.tex")
        self.assertEqual(data["tmux"]["session_name"], "example-agents")
        self.assertFalse(data["tmux"]["kill_windows_after_capture"])
        self.assertEqual(data["git"]["remote_url"], "git@github.com:wpegden/example.git")
        self.assertEqual(data["worker"]["provider"], "codex")
        self.assertEqual(data["reviewer"]["provider"], "claude")

    def test_build_config_json_uses_requested_providers(self) -> None:
        repo_path = self.make_repo()
        spec = init_formalization_project.InitSpec(
            repo_path=repo_path,
            remote_url="git@github.com:wpegden/example.git",
            paper_source=repo_path / "paper.tex",
            paper_dest_rel=Path("paper/paper.tex"),
            config_path=repo_path.parent / "example.json",
            package_name="Example",
            goal_file_name="GOAL.md",
            branch="main",
            author_name="leanagent",
            author_email="leanagent@packer.math.cmu.edu",
            max_cycles=3,
            session_name="example-agents",
            kill_windows_after_capture=False,
            worker_provider="codex",
            reviewer_provider="codex",
        )

        data = init_formalization_project.build_config_json(spec)

        self.assertEqual(data["worker"]["provider"], "codex")
        self.assertEqual(data["reviewer"]["provider"], "codex")
        self.assertEqual(data["reviewer"]["model"], "gpt-5.4")


@unittest.skipUnless(shutil.which("git"), "git is required for git integration tests")
class GitSetupTests(SupervisorTestCase):
    def test_ensure_git_repository_initializes_repo_and_remote(self) -> None:
        repo_path = self.make_repo()
        remote_root = repo_path.parent / "remote.git"
        self.git(repo_path.parent, "init", "--bare", str(remote_root))

        config = self.make_config(repo_path, git_remote_url=str(remote_root))
        supervisor.ensure_git_repository(config)

        self.assertTrue((repo_path / ".git").exists())
        self.assertIn(".agent-supervisor/", (repo_path / ".gitignore").read_text(encoding="utf-8"))
        self.assertEqual(self.git(repo_path, "remote", "get-url", "origin").stdout.strip(), str(remote_root))
        self.assertEqual(self.git(repo_path, "branch", "--show-current").stdout.strip(), "main")
        self.assertEqual(self.git(repo_path, "config", "--get", "user.name").stdout.strip(), "Test User")
        self.assertEqual(self.git(repo_path, "config", "--get", "user.email").stdout.strip(), "test@example.com")

    def test_ensure_git_repository_rejects_populated_remote_for_unborn_local_repo(self) -> None:
        repo_path = self.make_repo()
        remote_root = repo_path.parent / "remote-populated.git"
        self.git(repo_path.parent, "init", "--bare", str(remote_root))

        seed_repo = repo_path.parent / "seed-repo"
        seed_repo.mkdir(parents=True, exist_ok=True)
        self.git(seed_repo, "init", "-b", "main")
        self.git(seed_repo, "config", "user.name", "Seeder")
        self.git(seed_repo, "config", "user.email", "seed@example.com")
        (seed_repo / "README.md").write_text("seed\n", encoding="utf-8")
        self.git(seed_repo, "add", "README.md")
        self.git(seed_repo, "commit", "-m", "seed")
        self.git(seed_repo, "remote", "add", "origin", str(remote_root))
        self.git(seed_repo, "push", "origin", "HEAD:main")

        config = self.make_config(repo_path, git_remote_url=str(remote_root))
        with self.assertRaisesRegex(supervisor.SupervisorError, "already has branch"):
            supervisor.ensure_git_repository(config)


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for tmux integration tests")
class TmuxBurstTests(SupervisorTestCase):
    def test_launch_tmux_burst_handles_paths_with_spaces(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        supervisor.ensure_repo_files(config, "proof_formalization")
        supervisor.ensure_tmux_session(config)

        adapter = DummyAdapter(
            supervisor.ProviderConfig(provider="claude", model=None, extra_args=[]),
            "worker",
            config,
            {},
            [
                "bash",
                "-lc",
                "echo worker-output; printf '%s\\n' '{\"vibe_check\":\"NOT_STUCK\"}'",
            ],
        )

        try:
            run = supervisor.launch_tmux_burst(adapter, 1, "ignored prompt")
            self.assertEqual(run["exit_code"], 0)
            self.assertIn("worker-output", run["captured_output"])

            handoff = supervisor.load_json_artifact_with_fallback(
                Path(run["artifact_path"]),
                run["captured_output"],
                "vibe_check",
            )
            self.assertEqual(handoff["vibe_check"], "NOT_STUCK")

            latest_log = config.state_dir / "logs" / "worker.latest.ansi.log"
            self.assertTrue(latest_log.exists())
            self.assertIn("worker-output", latest_log.read_text(encoding="utf-8"))
        finally:
            self.cleanup_tmux_session(config.tmux.session_name)

    def test_launch_tmux_burst_times_out(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, burst_timeout_seconds=0.5, kill_windows_after_capture=False)
        supervisor.ensure_repo_files(config, "proof_formalization")
        supervisor.ensure_tmux_session(config)

        adapter = DummyAdapter(
            supervisor.ProviderConfig(provider="claude", model=None, extra_args=[]),
            "worker",
            config,
            {},
            ["bash", "-lc", "echo still-running; sleep 2"],
        )

        try:
            with self.assertRaisesRegex(supervisor.SupervisorError, "Timed out"):
                supervisor.launch_tmux_burst(adapter, 1, "ignored prompt")
        finally:
            self.cleanup_tmux_session(config.tmux.session_name)


if __name__ == "__main__":
    unittest.main()
