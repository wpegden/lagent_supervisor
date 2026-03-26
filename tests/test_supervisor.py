import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

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
                project_name=supervisor.sanitize_repo_name(repo_path.name),
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
    def test_sanitize_tmux_session_name_replaces_dots(self) -> None:
        self.assertEqual(
            supervisor.sanitize_tmux_session_name("arxiv-1702.07325-agents"),
            "arxiv-1702_07325-agents",
        )
        self.assertEqual(
            supervisor.sanitize_tmux_session_name(" weird session:name "),
            "weird_session_name",
        )

    def test_load_config_normalizes_tmux_session_name(self) -> None:
        repo_path = self.make_repo()
        config_path = repo_path.parent / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repo_path": str(repo_path),
                    "goal_file": "GOAL.md",
                    "worker": {"provider": "codex"},
                    "reviewer": {"provider": "claude"},
                    "tmux": {"session_name": "arxiv-1702.07325-agents"},
                }
            ),
            encoding="utf-8",
        )

        config = supervisor.load_config(config_path)

        self.assertEqual(config.tmux.session_name, "arxiv-1702_07325-agents")

    def test_load_config_uses_branching_defaults(self) -> None:
        repo_path = self.make_repo()
        config_path = repo_path.parent / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repo_path": str(repo_path),
                    "goal_file": "GOAL.md",
                    "worker": {"provider": "codex"},
                    "reviewer": {"provider": "claude"},
                }
            ),
            encoding="utf-8",
        )

        config = supervisor.load_config(config_path)

        self.assertEqual(config.branching.max_current_branches, 2)
        self.assertEqual(config.branching.evaluation_cycle_budget, supervisor.DEFAULT_BRANCH_EVALUATION_CYCLES)
        self.assertEqual(config.branching.poll_seconds, supervisor.DEFAULT_BRANCH_POLL_SECONDS)

    def test_load_config_reads_branching_overrides(self) -> None:
        repo_path = self.make_repo()
        config_path = repo_path.parent / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repo_path": str(repo_path),
                    "goal_file": "GOAL.md",
                    "worker": {"provider": "codex"},
                    "reviewer": {"provider": "claude"},
                    "branching": {
                        "max_current_branches": 4,
                        "evaluation_cycle_budget": 27,
                        "poll_seconds": 123.5,
                    },
                }
            ),
            encoding="utf-8",
        )

        config = supervisor.load_config(config_path)

        self.assertEqual(config.branching.max_current_branches, 4)
        self.assertEqual(config.branching.evaluation_cycle_budget, 27)
        self.assertEqual(config.branching.poll_seconds, 123.5)

    def test_load_config_defaults_chat_project_name_to_repo_name(self) -> None:
        repo_path = self.make_repo()
        config_path = repo_path.parent / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repo_path": str(repo_path),
                    "goal_file": "GOAL.md",
                    "worker": {"provider": "codex"},
                    "reviewer": {"provider": "claude"},
                    "chat": {"repo_name": "child-branch"},
                }
            ),
            encoding="utf-8",
        )

        config = supervisor.load_config(config_path)

        self.assertEqual(config.chat.repo_name, "child-branch")
        self.assertEqual(config.chat.project_name, "child-branch")

    def test_load_config_reads_chat_project_name_override(self) -> None:
        repo_path = self.make_repo()
        config_path = repo_path.parent / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repo_path": str(repo_path),
                    "goal_file": "GOAL.md",
                    "worker": {"provider": "codex"},
                    "reviewer": {"provider": "claude"},
                    "chat": {
                        "repo_name": "child-branch",
                        "project_name": "paper-project",
                    },
                }
            ),
            encoding="utf-8",
        )

        config = supervisor.load_config(config_path)

        self.assertEqual(config.chat.repo_name, "child-branch")
        self.assertEqual(config.chat.project_name, "paper-project")

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

    def test_gemini_includes_repo_and_state_dirs_in_workspace(self) -> None:
        repo_path = self.make_repo()
        base_config = self.make_config(repo_path)
        state_dir = repo_path.parent / "external-state"
        config = supervisor.Config(
            repo_path=base_config.repo_path,
            goal_file=base_config.goal_file,
            state_dir=state_dir,
            worker=supervisor.ProviderConfig(
                provider="gemini",
                model="gemini-3.1-pro-preview",
                extra_args=[],
            ),
            reviewer=base_config.reviewer,
            tmux=base_config.tmux,
            workflow=base_config.workflow,
            chat=base_config.chat,
            git=base_config.git,
            max_cycles=base_config.max_cycles,
            sleep_seconds=base_config.sleep_seconds,
            startup_timeout_seconds=base_config.startup_timeout_seconds,
            burst_timeout_seconds=base_config.burst_timeout_seconds,
        )
        adapter = supervisor.GeminiAdapter(config.worker, "worker", config, {})

        initial = adapter.build_initial_command()

        self.assertIn("--include-directories", initial)
        include_value = initial[initial.index("--include-directories") + 1]
        include_dirs = include_value.split(",")
        self.assertIn(str(state_dir), include_dirs)
        self.assertNotIn(str(repo_path), include_dirs)

    def test_gemini_burst_script_runs_in_repo_with_role_scoped_home(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config = supervisor.Config(
            repo_path=config.repo_path,
            goal_file=config.goal_file,
            state_dir=config.state_dir,
            worker=supervisor.ProviderConfig(
                provider="gemini",
                model="gemini-3.1-pro-preview",
                extra_args=[],
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
        adapter = supervisor.GeminiAdapter(config.worker, "worker", config, {})

        script_path = supervisor.build_burst_script(
            adapter,
            1,
            config.state_dir / "prompt.txt",
            config.state_dir / "start.txt",
            config.state_dir / "exit.txt",
        )
        script_text = script_path.read_text(encoding="utf-8")

        self.assertIn(f"WORK_DIR={shlex.quote(str(repo_path))}", script_text)
        self.assertIn(f"export GEMINI_CLI_HOME={shlex.quote(str(config.state_dir / 'scopes' / 'gemini-worker'))}", script_text)
        self.assertLess(script_text.index("export GEMINI_CLI_HOME="), script_text.index("cmd=("))

    def test_determine_resume_cycle_and_stage_starts_new_cycle_after_completed_review(self) -> None:
        cycle, stage = supervisor.determine_resume_cycle_and_stage(
            {
                "cycle": 12,
                "last_review": {"cycle": 12, "decision": "CONTINUE"},
            }
        )

        self.assertEqual((cycle, stage), (13, "worker"))

    def test_determine_resume_cycle_and_stage_retries_worker_after_interrupted_worker_burst(self) -> None:
        cycle, stage = supervisor.determine_resume_cycle_and_stage(
            {
                "cycle": 12,
                "last_review": {"cycle": 11, "decision": "CONTINUE"},
                "last_validation": {"cycle": 11},
            }
        )

        self.assertEqual((cycle, stage), (12, "worker"))

    def test_determine_resume_cycle_and_stage_retries_worker_when_no_review_has_run(self) -> None:
        cycle, stage = supervisor.determine_resume_cycle_and_stage({"cycle": 12})

        self.assertEqual((cycle, stage), (12, "worker"))

    def test_determine_resume_cycle_and_stage_retries_reviewer_after_interrupted_reviewer_burst(self) -> None:
        cycle, stage = supervisor.determine_resume_cycle_and_stage(
            {
                "cycle": 87,
                "last_review": {"cycle": 86, "decision": "CONTINUE"},
                "last_validation": {"cycle": 87},
                "last_worker_handoff": {"phase": "proof_formalization", "status": "NOT_STUCK"},
                "last_worker_output": "worker output",
            }
        )

        self.assertEqual((cycle, stage), (87, "reviewer"))

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
        self.assertIn("write your handoff JSON to `.agent-supervisor/worker_handoff.json`", prompt)
        self.assertNotIn("write your handoff JSON to `supervisor/worker_handoff.json`", prompt)
        self.assertIn("paper-facing interface", prompt)
        self.assertIn("separate support files", prompt)
        self.assertIn("short wrappers around results proved elsewhere", prompt)

    def test_gemini_worker_prompt_uses_repo_root_paths(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config = supervisor.Config(
            repo_path=config.repo_path,
            goal_file=config.goal_file,
            state_dir=config.state_dir,
            worker=supervisor.ProviderConfig(
                provider="gemini",
                model="gemini-3.1-pro-preview",
                extra_args=[],
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

        prompt = supervisor.build_worker_prompt(config, {}, "paper_check", True)

        self.assertIn("Goal file: GOAL.md", prompt)
        self.assertIn("write your handoff JSON to `.agent-supervisor/worker_handoff.json`", prompt)
        self.assertIn(".agent-supervisor/scopes/gemini-worker/GEMINI.md", prompt)
        self.assertNotIn("Goal file: repo/GOAL.md", prompt)

    def test_worker_prompt_includes_active_stuck_recovery_guidance(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = {
            "last_review": {
                "phase": "proof_formalization",
                "decision": "STUCK",
                "reason": "The current route is blocked.",
                "next_prompt": "",
                "cycle": 12,
            },
            "stuck_recovery_attempts": [
                {
                    "phase": "proof_formalization",
                    "attempt": 1,
                    "trigger_cycle": 12,
                    "diagnosis": "Missing support lemma.",
                    "creative_suggestion": "Switch to a local carrier-set reformulation first.",
                    "why_this_might_work": "It reduces the blocker to a finite combinatorial lemma.",
                    "worker_prompt": "Prove the carrier-set reformulation before touching the vertical-door argument.",
                }
            ],
        }

        prompt = supervisor.build_worker_prompt(config, state, "proof_formalization", False)

        self.assertIn("Supervisor stuck-recovery guidance", prompt)
        self.assertIn(f"recovery attempt 1 of {supervisor.MAX_STUCK_RECOVERY_ATTEMPTS}", prompt)
        self.assertIn("carrier-set reformulation", prompt)
        self.assertIn("Prove the carrier-set reformulation", prompt)

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

    def test_branch_strategy_prompt_mentions_eventual_whole_paper_success(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        prompt = supervisor.build_branch_strategy_prompt(
            config,
            {"review_log": []},
            "proof_formalization",
            "worker terminal output",
            '{"status":"STUCK"}',
            {"build": {"ok": True}, "sorries": {"count": 0}},
            {
                "phase": "proof_formalization",
                "decision": "STUCK",
                "confidence": 0.9,
                "reason": "The route may need a rewrite.",
                "next_prompt": "",
                "cycle": 20,
            },
            False,
        )

        self.assertIn("At most 2 branches may run concurrently", prompt)
        self.assertIn("eventually succeed at formalizing the whole paper", prompt)
        self.assertIn("If `branch_decision` is `BRANCH`, include between 2 and 2 strategies.", prompt)

    def test_branch_selection_prompt_mentions_eventual_whole_paper_success(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        prompt = supervisor.build_branch_selection_prompt(
            config,
            {"review_log": []},
            "proof_formalization",
            {
                "id": "episode-001",
                "selection_question": "Which branch seems more likely to eventually succeed at formalizing the whole paper?",
            },
            [
                {"name": "continue-current-route", "progress_reviews": 5, "latest_review_decision": "CONTINUE"},
                {"name": "major-rewrite", "progress_reviews": 5, "latest_review_decision": "CONTINUE"},
            ],
            False,
        )

        self.assertIn("Which branch seems more likely to eventually succeed at formalizing the whole paper?", prompt)
        self.assertIn("Do not default to the branch that is merely furthest along today.", prompt)

    def test_validate_branch_strategy_decision_accepts_configured_branch_limit(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config.branching.max_current_branches = 3

        decision = supervisor.validate_branch_strategy_decision(
            config,
            "proof_formalization",
            {
                "phase": "proof_formalization",
                "branch_decision": "branch",
                "confidence": 0.7,
                "reason": "There are three materially different routes.",
                "strategies": [
                    {
                        "name": "Continue Current Route",
                        "summary": "Keep the current support layer and push through.",
                        "worker_prompt": "Continue the current route.",
                        "why_this_might_eventually_succeed": "The local gap might still be bridgeable.",
                        "rewrite_scope": "incremental",
                    },
                    {
                        "name": "Paper Faithful Rewrite",
                        "summary": "Replace the local interface with a weaker paper-faithful theorem.",
                        "worker_prompt": "Rewrite the local continuation theorem.",
                        "why_this_might_eventually_succeed": "It aligns better with the manuscript.",
                        "rewrite_scope": "major",
                    },
                    {
                        "name": "Topological Route",
                        "summary": "Return to the topological surjectivity route.",
                        "worker_prompt": "Reopen the topological route.",
                        "why_this_might_eventually_succeed": "It may bypass the combinatorial bottleneck.",
                        "rewrite_scope": "major",
                    },
                ],
            },
        )

        self.assertEqual(decision["branch_decision"], "BRANCH")
        self.assertEqual([item["name"] for item in decision["strategies"]], ["continue-current-route", "paper-faithful-rewrite", "topological-route"])

    def test_should_consider_branching_for_cycle_20_style_route_pivot(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        should_branch = supervisor.should_consider_branching(
            config,
            {"cycle": 20, "last_branch_consideration_cycle": 0},
            "proof_formalization",
            {
                "phase": "proof_formalization",
                "decision": "CONTINUE",
                "confidence": 0.92,
                "reason": (
                    "The topological route now depends on a substantial noncontractibility theorem "
                    "that is not readily available in mathlib, but the paper still offers a "
                    "combinatorial Section 5 route."
                ),
                "next_prompt": "Pivot decisively to the paper's combinatorial Section 5 proof.",
                "cycle": 20,
            },
        )

        self.assertTrue(should_branch)

    def test_should_consider_branching_for_cycle_131_style_route_change(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        should_branch = supervisor.should_consider_branching(
            config,
            {"cycle": 131, "last_branch_consideration_cycle": 0},
            "proof_formalization",
            {
                "phase": "proof_formalization",
                "decision": "CONTINUE",
                "confidence": 0.86,
                "reason": (
                    "The current next-milestone lower-containment field now appears to be stronger "
                    "than what the manuscript actually states, and a more paper-faithful route has been identified."
                ),
                "next_prompt": (
                    "Treat this as a route change and replace the overstrong local theorem with "
                    "a same-level continuation witness."
                ),
                "cycle": 131,
            },
        )

        self.assertTrue(should_branch)

    def test_should_not_consider_branching_when_max_current_branches_is_one(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config.branching.max_current_branches = 1

        should_branch = supervisor.should_consider_branching(
            config,
            {"cycle": 130, "last_branch_consideration_cycle": 0},
            "proof_formalization",
            {
                "phase": "proof_formalization",
                "decision": "STUCK",
                "confidence": 0.89,
                "reason": "The route is blocked and may require a rewrite.",
                "next_prompt": "",
                "cycle": 130,
            },
        )

        self.assertFalse(should_branch)

    def test_stuck_recovery_attempt_state_machine(self) -> None:
        state = {
            "cycle": 51,
            "last_review": {"decision": "STUCK", "cycle": 51},
        }

        self.assertTrue(supervisor.can_attempt_stuck_recovery(state))

        first = supervisor.record_stuck_recovery_attempt(
            state,
            trigger_cycle=51,
            phase="proof_formalization",
            suggestion={
                "phase": "proof_formalization",
                "diagnosis": "d1",
                "creative_suggestion": "s1",
                "why_this_might_work": "w1",
                "worker_prompt": "p1",
            },
        )
        self.assertEqual(first["attempt"], 1)
        self.assertFalse(supervisor.can_attempt_stuck_recovery(state))

        for index in range(2, supervisor.MAX_STUCK_RECOVERY_ATTEMPTS + 1):
            trigger_cycle = 50 + index
            state["last_review"] = {"decision": "STUCK", "cycle": trigger_cycle}
            self.assertTrue(supervisor.can_attempt_stuck_recovery(state))
            attempt = supervisor.record_stuck_recovery_attempt(
                state,
                trigger_cycle=trigger_cycle,
                phase="proof_formalization",
                suggestion={
                    "phase": "proof_formalization",
                    "diagnosis": f"d{index}",
                    "creative_suggestion": f"s{index}",
                    "why_this_might_work": f"w{index}",
                    "worker_prompt": f"p{index}",
                },
            )
            self.assertEqual(attempt["attempt"], index)
            self.assertFalse(supervisor.can_attempt_stuck_recovery(state))

        state["last_review"] = {"decision": "STUCK", "cycle": 51 + supervisor.MAX_STUCK_RECOVERY_ATTEMPTS}
        self.assertFalse(supervisor.can_attempt_stuck_recovery(state))

        supervisor.clear_stuck_recovery(state)
        self.assertEqual(state["stuck_recovery_attempts"], [])
        self.assertIsNone(state["stuck_recovery_last_trigger_cycle"])

    def test_monitor_active_branch_episode_selects_winner_and_returns(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = {
            "active_branch_episode": {
                "id": "episode-001",
                "phase": "proof_formalization",
                "branches": [
                    {"name": "continue-current-route"},
                    {"name": "major-rewrite"},
                ],
            }
        }
        reviewer = DummyAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "reviewer",
            config,
            state,
            ["bash", "-lc", "exit 0"],
        )

        with (
            mock.patch.object(
                supervisor,
                "branch_episode_snapshots",
                return_value=[
                    {"name": "continue-current-route", "review_count": 30, "progress_reviews": 20, "cycle": 150},
                    {"name": "major-rewrite", "review_count": 30, "progress_reviews": 20, "cycle": 150},
                ],
            ),
            mock.patch.object(supervisor, "branch_episode_ready_for_selection", return_value=True),
            mock.patch.object(
                supervisor,
                "run_branch_selection_review",
                return_value={
                    "phase": "proof_formalization",
                    "selection_decision": "SELECT_BRANCH",
                    "confidence": 0.9,
                    "reason": "The rewrite is the better long-term route.",
                    "selected_branch": "major-rewrite",
                },
            ),
            mock.patch.object(
                supervisor,
                "prune_branch_episode",
                return_value={
                    "name": "major-rewrite",
                    "worktree_path": "/tmp/major-rewrite",
                    "config_path": "/tmp/major-rewrite.json",
                    "supervisor_session": "major-rewrite-supervisor",
                },
            ) as prune_mock,
        ):
            result = supervisor.monitor_active_branch_episode(config, state, reviewer, "proof_formalization")

        self.assertEqual(result, 0)
        prune_mock.assert_called_once_with(
            config,
            state,
            state["active_branch_episode"],
            "major-rewrite",
        )

    def test_build_child_branch_state_preserves_history_and_extends_lineage(self) -> None:
        state = {
            "branch_episode_counter": 2,
            "branch_history": [{"id": "episode-001", "selected_branch": "route-a", "status": "selected"}],
            "branch_lineage": [{"episode_id": "episode-001", "branch_name": "route-a", "summary": "old", "rewrite_scope": "major"}],
        }

        child_state = supervisor.build_child_branch_state(
            state,
            episode_id="episode-002",
            strategy={
                "name": "route-b",
                "summary": "new route",
                "worker_prompt": "do the rewrite",
                "why_this_might_eventually_succeed": "cleaner route",
                "rewrite_scope": "major",
            },
        )

        self.assertEqual(child_state["branch_episode_counter"], 2)
        self.assertEqual(len(child_state["branch_history"]), 1)
        self.assertEqual(
            [entry["branch_name"] for entry in child_state["branch_lineage"]],
            ["route-a", "route-b"],
        )

    def test_child_branch_config_payload_preserves_project_name(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config = supervisor.Config(
            repo_path=config.repo_path,
            goal_file=config.goal_file,
            state_dir=config.state_dir,
            worker=config.worker,
            reviewer=config.reviewer,
            tmux=config.tmux,
            workflow=config.workflow,
            chat=supervisor.ChatConfig(
                root_dir=config.chat.root_dir,
                repo_name="paper-project",
                project_name="paper-project",
                public_base_url=config.chat.public_base_url,
            ),
            git=config.git,
            max_cycles=config.max_cycles,
            sleep_seconds=config.sleep_seconds,
            startup_timeout_seconds=config.startup_timeout_seconds,
            burst_timeout_seconds=config.burst_timeout_seconds,
            branching=config.branching,
        )

        payload = supervisor.child_branch_config_payload(
            config,
            episode_id="episode-001",
            strategy={"name": "rewrite-route"},
            worktree_path=repo_path.parent / "paper-project--episode-001--rewrite-route",
            config_path=repo_path.parent / "episode-001" / "rewrite-route.json",
        )

        self.assertEqual(payload["chat"]["repo_name"], "paper-project-episode-001-rewrite-route")
        self.assertEqual(payload["chat"]["project_name"], "paper-project")

    def test_branch_overview_marks_dead_current_path(self) -> None:
        overview = supervisor.branch_overview(
            {
                "branch_lineage": [{"episode_id": "episode-001", "branch_name": "losing-route"}],
                "branch_history": [
                    {
                        "id": "episode-001",
                        "status": "selected",
                        "selected_branch": "winning-route",
                        "trigger_cycle": 20,
                        "phase": "proof_formalization",
                        "lineage": [],
                        "branches": [
                            {"name": "winning-route", "summary": "keep going", "rewrite_scope": "incremental"},
                            {"name": "losing-route", "summary": "dead end", "rewrite_scope": "major"},
                        ],
                    }
                ],
            }
        )

        self.assertEqual(overview["current_path_status"], "dead")
        self.assertEqual(overview["episodes"][0]["branches"][1]["status"], "dead")
        self.assertTrue(overview["episodes"][0]["branches"][1]["is_current_path"])

    def test_branch_overview_includes_child_chat_repo_names(self) -> None:
        overview = supervisor.branch_overview(
            {
                "branch_history": [
                    {
                        "id": "episode-001",
                        "status": "active",
                        "trigger_cycle": 17,
                        "phase": "proof_formalization",
                        "lineage": [],
                        "branches": [
                            {
                                "name": "boundary-subdivision",
                                "chat_repo_name": "paper-project-episode-001-boundary-subdivision",
                                "summary": "Incremental support-layer repair.",
                                "rewrite_scope": "incremental",
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual(
            overview["episodes"][0]["branches"][0]["repo_name"],
            "paper-project-episode-001-boundary-subdivision",
        )


class ArtifactFallbackTests(SupervisorTestCase):
    def test_burst_captured_output_prefers_raw_log_over_wrapped_pane_capture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lagent burst capture ") as tmpdir:
            log_path = Path(tmpdir) / "worker.log"
            log_path.write_text(
                '{"phase":"proof_formalization","status":"NOT_STUCK","summary_of_changes":"ok","current_frontier":"x","likely_next_step":"y","input_request":""}\n',
                encoding="utf-8",
            )
            pane_capture = '{"phase":"proof_formalization","status":"NOT_\nSTUCK"}'

            captured = supervisor.burst_captured_output(log_path, pane_capture)

            self.assertIn('"status":"NOT_STUCK"', captured)
            self.assertEqual(
                supervisor.extract_json_object(captured, required_key="status")["status"],
                "NOT_STUCK",
            )

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

    def test_fallback_can_require_full_schema_to_avoid_nested_status_object(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lagent artifacts ") as tmpdir:
            artifact_path = Path(tmpdir) / "worker_handoff.json"
            captured = """
noise
{"phase": "proof_formalization", "status": "NOT_STUCK", "summary_of_changes": "ok", "current_frontier": "f", "likely_next_step": "n", "input_request": ""}
later noise
{"status": "clean"}
"""
            data = supervisor.load_json_artifact_with_fallback(
                artifact_path,
                captured,
                ("phase", "status", "summary_of_changes", "current_frontier", "likely_next_step", "input_request"),
            )
            self.assertEqual(data["status"], "NOT_STUCK")
            self.assertEqual(data["summary_of_changes"], "ok")

    def test_legacy_supervisor_artifact_path_is_used_as_fallback(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = config.state_dir / "worker_handoff.json"
        legacy_path = repo_path / "supervisor" / "worker_handoff.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps({"phase": "proof_formalization", "status": "STUCK"}, indent=2),
            encoding="utf-8",
        )

        data = supervisor.load_json_artifact_with_fallback(
            artifact_path,
            "",
            "status",
            fallback_paths=supervisor.legacy_supervisor_artifact_paths(config, artifact_path),
        )

        self.assertEqual(data["status"], "STUCK")

    def test_recover_interrupted_worker_state_from_legacy_artifact(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        (config.state_dir / "logs").mkdir(parents=True, exist_ok=True)
        legacy_path = repo_path / "supervisor" / "worker_handoff.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps(
                {
                    "phase": "proof_formalization",
                    "status": "STUCK",
                    "summary_of_changes": "none",
                    "current_frontier": "frontier",
                    "likely_next_step": "next",
                    "input_request": "",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (config.state_dir / "logs" / "worker-cycle-0012.ansi.log").write_text("worker output", encoding="utf-8")
        state = {
            "cycle": 12,
            "last_review": {"cycle": 11, "decision": "CONTINUE"},
        }

        with (
            mock.patch.object(supervisor, "run_validation", return_value={"cycle": 12, "build": {"ok": True}}) as validation_mock,
            mock.patch.object(supervisor, "record_chat_event") as record_mock,
        ):
            recovered = supervisor.recover_interrupted_worker_state(config, state, "proof_formalization")

        self.assertTrue(recovered)
        self.assertEqual(state["last_worker_handoff"]["status"], "STUCK")
        self.assertEqual(state["last_worker_output"], "worker output")
        self.assertEqual(state["last_validation"]["cycle"], 12)
        validation_mock.assert_called_once_with(config, "proof_formalization", 12)
        self.assertEqual(record_mock.call_count, 2)

    def test_recover_interrupted_worker_state_from_log_only(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        (config.state_dir / "logs").mkdir(parents=True, exist_ok=True)
        (config.state_dir / "logs" / "worker-cycle-0012.ansi.log").write_text(
            """
worker output
{
  "phase": "proof_formalization",
  "status": "NOT_STUCK",
  "summary_of_changes": "made progress",
  "current_frontier": "frontier",
  "likely_next_step": "next",
  "input_request": ""
}
""",
            encoding="utf-8",
        )
        state = {
            "cycle": 12,
            "last_review": {"cycle": 11, "decision": "CONTINUE"},
        }

        with (
            mock.patch.object(supervisor, "run_validation", return_value={"cycle": 12, "build": {"ok": True}}) as validation_mock,
            mock.patch.object(supervisor, "record_chat_event") as record_mock,
        ):
            recovered = supervisor.recover_interrupted_worker_state(config, state, "proof_formalization")

        self.assertTrue(recovered)
        self.assertEqual(state["last_worker_handoff"]["status"], "NOT_STUCK")
        self.assertIn("made progress", state["last_worker_output"])
        self.assertEqual(state["last_validation"]["cycle"], 12)
        validation_mock.assert_called_once_with(config, "proof_formalization", 12)
        self.assertEqual(record_mock.call_count, 2)


class BurstRetryTests(SupervisorTestCase):
    def test_launch_tmux_burst_with_retries_retries_after_nonzero_exit(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        adapter = DummyAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "worker",
            config,
            {},
            ["bash", "-lc", "exit 0"],
        )

        failed = {
            "captured_output": "capacity error",
            "artifact_path": repo_path / "artifact.json",
            "per_cycle_log": repo_path / "failed.log",
            "exit_code": 1,
            "pane_id": "%1",
            "window_id": "@1",
        }
        succeeded = {
            "captured_output": "ok",
            "artifact_path": repo_path / "artifact.json",
            "per_cycle_log": repo_path / "success.log",
            "exit_code": 0,
            "pane_id": "%2",
            "window_id": "@2",
        }

        with (
            mock.patch.object(supervisor, "launch_tmux_burst", side_effect=[failed, succeeded]) as launch_mock,
            mock.patch.object(supervisor.time, "sleep") as sleep_mock,
        ):
            run = supervisor.launch_tmux_burst_with_retries(
                adapter,
                7,
                "prompt",
                stage_label="reviewer burst",
            )

        self.assertEqual(run, succeeded)
        self.assertEqual(launch_mock.call_count, 2)
        sleep_mock.assert_called_once_with(supervisor.AGENT_CLI_RETRY_DELAYS_SECONDS[0])

    def test_launch_tmux_burst_with_retries_raises_after_retry_budget(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        adapter = DummyAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "reviewer",
            config,
            {},
            ["bash", "-lc", "exit 1"],
        )

        failed = {
            "captured_output": "capacity error",
            "artifact_path": repo_path / "artifact.json",
            "per_cycle_log": repo_path / "failed.log",
            "exit_code": 1,
            "pane_id": "%1",
            "window_id": "@1",
        }

        with (
            mock.patch.object(
                supervisor,
                "launch_tmux_burst",
                side_effect=[dict(failed) for _ in range(len(supervisor.AGENT_CLI_RETRY_DELAYS_SECONDS) + 1)],
            ) as launch_mock,
            mock.patch.object(supervisor.time, "sleep") as sleep_mock,
        ):
            with self.assertRaisesRegex(supervisor.SupervisorError, "after 3 retry attempts"):
                supervisor.launch_tmux_burst_with_retries(
                    adapter,
                    11,
                    "prompt",
                    stage_label="reviewer burst",
                )

        self.assertEqual(launch_mock.call_count, len(supervisor.AGENT_CLI_RETRY_DELAYS_SECONDS) + 1)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            list(supervisor.AGENT_CLI_RETRY_DELAYS_SECONDS),
        )


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

    def test_load_chat_meta_refreshes_identity_fields_from_config(self) -> None:
        repo_path = self.make_repo()
        chat_root = repo_path.parent / "chat site"
        config = self.make_config(repo_path, chat_root_dir=chat_root)
        meta_path = supervisor.chat_repo_meta_path(config)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(
                {
                    "repo_name": "stale-repo",
                    "project_name": "stale-project",
                    "is_branch": False,
                    "repo_display_name": "stale display",
                    "repo_path": "/tmp/stale",
                    "goal_file": "stale/GOAL.md",
                    "chat_url": "https://example.com/stale",
                    "direct_url": "https://example.com/stale/",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        meta = supervisor.load_chat_meta(config)

        self.assertEqual(meta["repo_name"], config.chat.repo_name)
        self.assertEqual(meta["project_name"], config.chat.project_name)
        self.assertEqual(meta["is_branch"], config.chat.repo_name != config.chat.project_name)
        self.assertEqual(meta["repo_display_name"], config.repo_path.name)
        self.assertEqual(meta["repo_path"], str(config.repo_path))
        self.assertEqual(meta["goal_file"], "repo/GOAL.md")
        self.assertEqual(meta["chat_url"], supervisor.chat_repo_url(config))
        self.assertEqual(meta["direct_url"], supervisor.chat_repo_direct_url(config))
        self.assertEqual(meta["updated_at"], "2026-01-01T00:00:00Z")

    def test_chat_event_export_builds_manifest_and_repo_files(self) -> None:
        repo_path = self.make_repo()
        chat_root = repo_path.parent / "chat site"
        config = self.make_config(repo_path, chat_root_dir=chat_root, start_phase="planning")
        supervisor.ensure_repo_files(config, "planning")
        (repo_path / "TASKS.md").write_text("# Tasks\n\n- [ ] Do work.\n", encoding="utf-8")
        (repo_path / "PLAN.md").write_text("# High-Level Plan\n\n- Main step.\n", encoding="utf-8")
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
        self.assertTrue((chat_root / "_assets" / "markdown-viewer.html").exists())
        self.assertTrue((chat_root / "_assets" / "markdown-viewer.js").exists())
        self.assertTrue((chat_root / "_assets" / "styles.css").exists())
        self.assertTrue((chat_root / config.chat.repo_name / "index.html").exists())

        manifest = json.loads((chat_root / "repos.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["repos"][0]["repo_name"], config.chat.repo_name)
        self.assertEqual(manifest["repos"][0]["project_name"], config.chat.project_name)
        self.assertFalse(manifest["repos"][0]["is_branch"])
        self.assertEqual(manifest["repos"][0]["last_reviewer_decision"], "CONTINUE")

        meta = json.loads((chat_root / config.chat.repo_name / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["project_name"], config.chat.project_name)
        self.assertFalse(meta["is_branch"])
        self.assertEqual(meta["current_cycle"], 2)
        self.assertEqual(meta["last_event_kind"], "reviewer_decision")
        exported_paths = {entry["path"] for entry in meta["markdown_files"]}
        self.assertIn("repo/GOAL.md", exported_paths)
        self.assertIn("repo/TASKS.md", exported_paths)
        self.assertIn("repo/PLAN.md", exported_paths)
        self.assertTrue(all(entry.get("href") for entry in meta["markdown_files"]))
        self.assertTrue(all(entry.get("label") for entry in meta["markdown_files"]))
        self.assertIsNone(meta["branch_overview"])

    def test_chat_event_export_includes_branch_overview(self) -> None:
        repo_path = self.make_repo()
        chat_root = repo_path.parent / "chat site"
        config = self.make_config(repo_path, chat_root_dir=chat_root, start_phase="proof_formalization")
        state = {
            "phase": "proof_formalization",
            "cycle": 130,
            "awaiting_human_input": False,
            "branch_lineage": [{"episode_id": "episode-001", "branch_name": "paper-faithful-rewrite"}],
            "branch_history": [
                {
                    "id": "episode-001",
                    "status": "selected",
                    "selected_branch": "paper-faithful-rewrite",
                    "trigger_cycle": 130,
                    "phase": "proof_formalization",
                    "lineage": [],
                    "branches": [
                        {"name": "continue-current-route", "summary": "push forward", "rewrite_scope": "incremental"},
                        {"name": "paper-faithful-rewrite", "summary": "major rewrite", "rewrite_scope": "major"},
                    ],
                }
            ],
        }

        supervisor.record_chat_event(
            config,
            state,
            cycle=130,
            phase="proof_formalization",
            kind="reviewer_decision",
            actor="reviewer",
            target="supervisor",
            content={
                "phase": "proof_formalization",
                "decision": "CONTINUE",
                "confidence": 0.9,
                "reason": "A rewrite branch looks more promising.",
                "next_prompt": "Keep going.",
            },
            content_type="json",
        )

        meta = json.loads((chat_root / config.chat.repo_name / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["branch_overview"]["current_path_newest_to_oldest"], ["paper-faithful-rewrite", "mainline"])
        self.assertEqual(meta["branch_overview"]["episodes"][0]["branches"][1]["status"], "selected")
        self.assertTrue(meta["branch_overview"]["episodes"][0]["branches"][1]["is_current_path"])
        self.assertIsNone(meta["branch_overview"]["episodes"][0]["branches"][0]["repo_name"])

    def test_refresh_chat_markdown_metadata_updates_stale_export(self) -> None:
        repo_path = self.make_repo()
        chat_root = repo_path.parent / "chat site"
        config = self.make_config(repo_path, chat_root_dir=chat_root, start_phase="planning")
        supervisor.ensure_repo_files(config, "planning")
        plan_path = repo_path / "PLAN.md"
        plan_path.write_text("# High-Level Plan\n\n- First draft.\n", encoding="utf-8")

        state = {"phase": "planning", "cycle": 1, "awaiting_human_input": False}
        supervisor.record_chat_event(
            config,
            state,
            cycle=1,
            phase="planning",
            kind="worker_prompt",
            actor="supervisor",
            target="worker",
            content="Plan the project.",
            content_type="text",
        )

        exported_plan = chat_root / config.chat.repo_name / "files" / "repo" / "PLAN.md"
        self.assertEqual(exported_plan.read_text(encoding="utf-8"), "# High-Level Plan\n\n- First draft.\n")

        time.sleep(0.02)
        plan_path.write_text("# Formalization Plan\n\n- Expanded plan.\n", encoding="utf-8")
        supervisor.refresh_chat_markdown_metadata(config, update_manifest=False)

        self.assertEqual(exported_plan.read_text(encoding="utf-8"), "# Formalization Plan\n\n- Expanded plan.\n")
        meta = json.loads((chat_root / config.chat.repo_name / "meta.json").read_text(encoding="utf-8"))
        plan_entry = next(entry for entry in meta["markdown_files"] if entry["label"] == "PLAN.md")
        self.assertIn("Expanded plan", exported_plan.read_text(encoding="utf-8"))
        self.assertTrue(plan_entry["updated_at"])


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
    def test_build_config_json_normalizes_tmux_session_name(self) -> None:
        repo_path = self.make_repo()
        spec = init_formalization_project.InitSpec(
            repo_path=repo_path,
            remote_url=None,
            paper_source=repo_path / "paper.tex",
            paper_arxiv_id=None,
            paper_dest_rel=Path("paper/paper.tex"),
            config_path=repo_path.parent / "example.json",
            package_name="Example",
            goal_file_name="GOAL.md",
            branch="main",
            author_name="leanagent",
            author_email="leanagent@packer.math.cmu.edu",
            max_cycles=3,
            session_name="arxiv-1702.07325-agents",
            kill_windows_after_capture=False,
            worker_provider="codex",
            reviewer_provider="claude",
        )

        data = init_formalization_project.build_config_json(spec)

        self.assertEqual(data["tmux"]["session_name"], "arxiv-1702_07325-agents")

    def test_normalize_arxiv_id_accepts_prefixed_and_old_style_ids(self) -> None:
        self.assertEqual(init_formalization_project.normalize_arxiv_id("arXiv:1607.07814"), "1607.07814")
        self.assertEqual(init_formalization_project.normalize_arxiv_id("math/0301234v2"), "math/0301234v2")
        self.assertIsNone(init_formalization_project.normalize_arxiv_id("/tmp/paper.tex"))

    def test_default_paths_use_arxiv_identifier_stem(self) -> None:
        repo_path, config_path = init_formalization_project.default_paths(None, "1607.07814")
        self.assertEqual(repo_path.name, "arxiv-1607.07814")
        self.assertEqual(config_path.name, "arxiv-1607.07814.json")

    def test_flatten_arxiv_source_inlines_tex_and_bbl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lagent arxiv flatten ") as tmpdir:
            source_dir = Path(tmpdir)
            (source_dir / "sections").mkdir()
            (source_dir / "main.tex").write_text(
                textwrap.dedent(
                    r"""
                    \documentclass{article}
                    \begin{document}
                    % \input{ignored}
                    \input{sections/intro}
                    \bibliographystyle{alpha}
                    \bibliography{refs}
                    \end{document}
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            (source_dir / "sections" / "intro.tex").write_text(
                "Intro text.\n",
                encoding="utf-8",
            )
            (source_dir / "main.bbl").write_text(
                "\\begin{thebibliography}{1}\n\\bibitem{ref} Ref.\n\\end{thebibliography}\n",
                encoding="utf-8",
            )

            main_tex, flattened = init_formalization_project.flatten_arxiv_source(source_dir, "1607.07814")

            self.assertEqual(main_tex.name, "main.tex")
            self.assertIn("arXiv identifier: 1607.07814", flattened)
            self.assertIn("begin included file: sections/intro.tex", flattened)
            self.assertIn("Intro text.", flattened)
            self.assertIn("begin bibliography from: main.bbl", flattened)
            self.assertIn("\\bibitem{ref}", flattened)
            self.assertIn("% \\input{ignored}", flattened)

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
        release_path = repo_path / ".github" / "workflows" / "create-release.yml"
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
        release_path.write_text(
            """name: Create Release

on:
  push:
    paths:
      - 'lean-toolchain'
""",
            encoding="utf-8",
        )

        written = init_formalization_project.ensure_build_only_ci_workflow(repo_path)
        content = written.read_text(encoding="utf-8")

        self.assertEqual(written, workflow_path)
        self.assertIn("leanprover/lean-action@v1", content)
        self.assertNotIn("docgen-action", content)
        self.assertNotIn("pages: write", content)
        self.assertFalse(release_path.exists())

    def test_build_config_json_uses_expected_defaults(self) -> None:
        repo_path = self.make_repo()
        spec = init_formalization_project.InitSpec(
            repo_path=repo_path,
            remote_url="git@github.com:wpegden/example.git",
            paper_source=repo_path / "paper.tex",
            paper_arxiv_id=None,
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
            paper_arxiv_id=None,
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
    def test_branch_episode_preflight_error_requires_git_worktree(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        error = supervisor.branch_episode_preflight_error(config)

        self.assertIn("already be a git worktree", error or "")

    def test_branch_episode_preflight_error_accepts_clean_committed_repo(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        self.git(repo_path, "init", "-b", "main")
        self.git(repo_path, "config", "user.name", "Test User")
        self.git(repo_path, "config", "user.email", "test@example.com")
        self.git(repo_path, "add", "GOAL.md", "paper.tex")
        self.git(repo_path, "commit", "-m", "seed")

        error = supervisor.branch_episode_preflight_error(config)

        self.assertIsNone(error)

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
