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
        theorem_frontier_phase: str = "off",
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
                theorem_frontier_phase=theorem_frontier_phase,
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
            policy_path=repo_path.parent / "policy.json",
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
    def test_phase_sequence_keeps_legacy_order_and_appends_cleanup(self) -> None:
        self.assertEqual(
            supervisor.PHASES,
            (
                "paper_check",
                "planning",
                "theorem_stating",
                "proof_formalization",
                supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
            ),
        )
        self.assertEqual(supervisor.next_phase("paper_check"), "planning")
        self.assertEqual(supervisor.next_phase("planning"), "theorem_stating")
        self.assertEqual(supervisor.next_phase("theorem_stating"), "proof_formalization")
        self.assertEqual(
            supervisor.next_phase("proof_formalization"),
            supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
        )

    def test_legacy_phase_reviewer_decisions_are_unchanged_before_cleanup(self) -> None:
        self.assertEqual(
            supervisor.phase_specific_reviewer_decisions("paper_check"),
            ("CONTINUE", "ADVANCE_PHASE", "STUCK"),
        )
        self.assertEqual(
            supervisor.phase_specific_reviewer_decisions("planning"),
            supervisor.REVIEWER_DECISIONS,
        )
        self.assertEqual(
            supervisor.phase_specific_reviewer_decisions("theorem_stating"),
            ("CONTINUE", "ADVANCE_PHASE", "STUCK"),
        )
        self.assertEqual(
            supervisor.phase_specific_reviewer_decisions("proof_formalization"),
            ("CONTINUE", "ADVANCE_PHASE", "STUCK"),
        )

    def test_theorem_stating_worker_prompt_requires_main_results_manifest(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase="theorem_stating", theorem_frontier_phase="full")

        prompt = supervisor.build_worker_prompt(config, {"phase": "theorem_stating"}, "theorem_stating", True)

        self.assertIn("paper_main_results.json", prompt)
        self.assertIn("machine-readable coarse paper-DAG manifest", prompt)
        self.assertIn("initial_active_edge_id", prompt)

    def test_theorem_stating_reviewer_prompt_mentions_main_results_manifest(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase="theorem_stating", theorem_frontier_phase="full")

        prompt = supervisor.build_reviewer_prompt(
            config,
            {"phase": "theorem_stating", "last_worker_output": "", "last_worker_handoff": {}},
            "theorem_stating",
            "",
            "{}",
            {"build": {"ok": True}, "syntax_checks": [], "sorry_policy": {"disallowed_entries": []}, "axioms": {"unapproved": []}},
            True,
        )

        self.assertIn("paper_main_results.json", prompt)
        self.assertIn("coarse paper-DAG manifest", prompt)
        self.assertIn("initial active obligation edge", prompt)

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
        self.assertEqual(config.policy_path, config_path.with_suffix(".policy.json"))

    def test_load_config_reads_provider_fallback_model(self) -> None:
        repo_path = self.make_repo()
        config_path = repo_path.parent / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repo_path": str(repo_path),
                    "goal_file": "GOAL.md",
                    "worker": {
                        "provider": "gemini",
                        "model": "gemini-3.1-pro-preview",
                        "fallback_model": "gemini-2.5-flash",
                    },
                    "reviewer": {"provider": "claude"},
                }
            ),
            encoding="utf-8",
        )

        config = supervisor.load_config(config_path)

        self.assertEqual(config.worker.provider, "gemini")
        self.assertEqual(config.worker.model, "gemini-3.1-pro-preview")
        self.assertEqual(config.worker.fallback_model, "gemini-2.5-flash")

    def test_load_config_reads_explicit_relative_policy_path(self) -> None:
        repo_path = self.make_repo()
        config_path = repo_path.parent / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repo_path": str(repo_path),
                    "goal_file": "GOAL.md",
                    "worker": {"provider": "codex"},
                    "reviewer": {"provider": "claude"},
                    "policy_path": "policies/runtime-policy.json",
                }
            ),
            encoding="utf-8",
        )

        config = supervisor.load_config(config_path)

        self.assertEqual(config.policy_path, (config_path.parent / "policies" / "runtime-policy.json").resolve())

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

    def test_load_config_defaults_theorem_frontier_phase_full(self) -> None:
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

        self.assertEqual(config.workflow.theorem_frontier_phase, "full")

    def test_load_config_reads_theorem_frontier_phase_override(self) -> None:
        repo_path = self.make_repo()
        config_path = repo_path.parent / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repo_path": str(repo_path),
                    "goal_file": "GOAL.md",
                    "worker": {"provider": "codex"},
                    "reviewer": {"provider": "claude"},
                    "workflow": {"theorem_frontier_phase": "phase0"},
                }
            ),
            encoding="utf-8",
        )

        config = supervisor.load_config(config_path)

        self.assertEqual(config.workflow.theorem_frontier_phase, "phase0")

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


class PolicyTests(SupervisorTestCase):
    def test_policy_manager_writes_default_policy_file_and_records_state(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = supervisor.load_state(config)

        manager = supervisor.PolicyManager(config)
        policy = manager.reload(state=state, force=True, persist=True)

        self.assertTrue(config.policy_path.exists())
        self.assertEqual(policy.branching.evaluation_cycle_budget, config.branching.evaluation_cycle_budget)
        self.assertEqual(policy.branching.poll_seconds, config.branching.poll_seconds)
        self.assertEqual(
            policy.branching.selection_recheck_increments_reviews,
            supervisor.DEFAULT_BRANCH_SELECTION_RECHECK_INCREMENTS_REVIEWS,
        )
        self.assertEqual(policy.timing.sleep_seconds, config.sleep_seconds)
        self.assertEqual(
            state["policy"]["effective"]["stuck_recovery"]["mainline_max_attempts"],
            supervisor.DEFAULT_MAINLINE_STUCK_RECOVERY_ATTEMPTS,
        )
        self.assertEqual(
            state["policy"]["effective"]["codex_budget_pause"]["weekly_percent_left_threshold"],
            supervisor.DEFAULT_CODEX_WEEKLY_BUDGET_PAUSE_THRESHOLD_PERCENT_LEFT,
        )

    def test_policy_manager_reloads_updated_values(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = supervisor.load_state(config)
        manager = supervisor.PolicyManager(config)
        manager.reload(state=state, force=True)
        supervisor.JsonFile.dump(
            config.policy_path,
            {
                "stuck_recovery": {
                    "mainline_max_attempts": 7,
                    "branch_max_attempts": 3,
                },
                "branching": {
                    "evaluation_cycle_budget": 31,
                    "poll_seconds": 17.5,
                    "proposal_cooldown_reviews": 9,
                    "replacement_min_confidence": 0.93,
                    "selection_recheck_increments_reviews": [12, 6],
                },
                "timing": {
                    "sleep_seconds": 2.5,
                    "agent_retry_delays_seconds": [11, 22],
                },
                "codex_budget_pause": {
                    "weekly_percent_left_threshold": 12.5,
                    "poll_seconds": 123,
                },
                "prompt_notes": {
                    "worker": "Use the paper-facing interface.",
                    "reviewer": "Prefer the narrowest sound frontier.",
                    "branching": "Avoid speculative rewrites.",
                },
            },
        )

        policy = manager.reload(state=state, force=True)

        self.assertEqual(policy.stuck_recovery.mainline_max_attempts, 7)
        self.assertEqual(policy.stuck_recovery.branch_max_attempts, 3)
        self.assertEqual(policy.branching.evaluation_cycle_budget, 31)
        self.assertEqual(policy.branching.poll_seconds, 17.5)
        self.assertEqual(policy.branching.proposal_cooldown_reviews, 9)
        self.assertEqual(policy.branching.replacement_min_confidence, 0.93)
        self.assertEqual(policy.branching.selection_recheck_increments_reviews, (12, 6))
        self.assertEqual(policy.timing.sleep_seconds, 2.5)
        self.assertEqual(policy.timing.agent_retry_delays_seconds, (11.0, 22.0))
        self.assertEqual(policy.codex_budget_pause.weekly_percent_left_threshold, 12.5)
        self.assertEqual(policy.codex_budget_pause.poll_seconds, 123.0)
        self.assertEqual(policy.prompt_notes.worker, "Use the paper-facing interface.")

    def test_latest_codex_token_count_event_in_file_reads_tail_record(self) -> None:
        repo_path = self.make_repo()
        session_log = repo_path.parent / "session.jsonl"
        session_log.write_text(
            "\n".join(
                [
                    json.dumps({"timestamp": "2026-03-26T18:00:00Z", "payload": {"type": "agent_message"}}),
                    json.dumps(
                        {
                            "timestamp": "2026-03-26T18:01:00Z",
                            "payload": {
                                "type": "token_count",
                                "rate_limits": {
                                    "plan_type": "pro",
                                    "primary": {"used_percent": 22.0, "window_minutes": 300},
                                    "secondary": {
                                        "used_percent": 44.0,
                                        "window_minutes": 10080,
                                        "resets_at": 1775081890,
                                    },
                                },
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        record = supervisor.latest_codex_token_count_event_in_file(session_log)

        self.assertIsNotNone(record)
        self.assertEqual(record["timestamp"], "2026-03-26T18:01:00Z")

    def test_wait_for_codex_weekly_budget_if_needed_pauses_until_threshold_clears(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = supervisor.load_state(config)

        low = {
            "timestamp": "2026-03-26T18:34:00Z",
            "source_path": "/tmp/session.jsonl",
            "used_percent": 90.0,
            "percent_left": 10.0,
            "window_minutes": 10080,
            "resets_at": 1775081890,
        }
        recovered = {
            "timestamp": "2026-03-26T18:40:00Z",
            "source_path": "/tmp/session.jsonl",
            "used_percent": 80.0,
            "percent_left": 20.0,
            "window_minutes": 10080,
            "resets_at": 1775081890,
        }

        with (
            mock.patch.object(supervisor, "latest_codex_weekly_budget_status", side_effect=[low, recovered]),
            mock.patch.object(supervisor.time, "sleep") as sleep_mock,
        ):
            supervisor.wait_for_codex_weekly_budget_if_needed(
                config,
                state,
                phase="proof_formalization",
                stage_label="worker burst",
            )

        sleep_mock.assert_called_once_with(
            supervisor.DEFAULT_CODEX_WEEKLY_BUDGET_PAUSE_POLL_SECONDS
        )
        self.assertIsNone(state["codex_budget_pause"])

    def test_wait_for_codex_weekly_budget_if_needed_skips_when_threshold_disabled(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        supervisor.JsonFile.dump(
            config.policy_path,
            {
                "codex_budget_pause": {
                    "weekly_percent_left_threshold": 0,
                    "poll_seconds": 123,
                }
            },
        )
        state = supervisor.load_state(config)

        with (
            mock.patch.object(supervisor, "latest_codex_weekly_budget_status") as status_mock,
            mock.patch.object(supervisor.time, "sleep") as sleep_mock,
        ):
            supervisor.wait_for_codex_weekly_budget_if_needed(
                config,
                state,
                phase="proof_formalization",
                stage_label="worker burst",
            )

        status_mock.assert_not_called()
        sleep_mock.assert_not_called()

    def test_branch_selection_schedule_helpers_and_legacy_migration(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = {"active_branch_episode": None}
        episode = {
            "id": "episode-002",
            "phase": "proof_formalization",
            "base_review_count": 44,
            "next_selection_review_target": 84,
            "evaluation_cycle_budget": 20,
        }

        continue_count = supervisor.branch_selection_continue_count(config, episode)
        self.assertEqual(continue_count, 1)
        self.assertEqual(
            supervisor.branch_selection_target_for_continue_count(config, episode, continue_count),
            69,
        )

        supervisor.normalize_branch_episode_selection_schedule(config, state, episode)
        self.assertEqual(episode["selection_continue_count"], 1)
        self.assertEqual(episode["next_selection_review_target"], 69)

        episode["selection_continue_count"] = 1
        self.assertEqual(
            supervisor.branch_selection_target_for_continue_count(config, episode, 2),
            74,
        )

    def test_policy_manager_keeps_last_good_policy_after_invalid_edit(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = supervisor.load_state(config)
        manager = supervisor.PolicyManager(config)
        original = manager.reload(state=state, force=True)
        config.policy_path.write_text("{not json", encoding="utf-8")

        with mock.patch("builtins.print") as print_mock:
            fallback = manager.reload(state=state, force=True)

        self.assertEqual(fallback, original)
        self.assertIn("warning", state["policy"])
        self.assertTrue(state["policy"]["warning"])
        print_mock.assert_called()

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

    def test_prepare_gemini_cli_home_merges_fail_fast_setting_when_fallback_enabled(self) -> None:
        source_home = Path(tempfile.mkdtemp(prefix="lagent gemini source "))
        self.addCleanup(shutil.rmtree, source_home, True)
        (source_home / ".gemini").mkdir(parents=True, exist_ok=True)
        (source_home / ".gemini" / "settings.json").write_text(
            json.dumps(
                {
                    "security": {"auth": {"selectedType": "oauth-personal"}},
                    "general": {"retryFetchErrors": True},
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        scope_dir = self.make_repo() / ".agent-supervisor" / "scopes" / "gemini-worker"
        with mock.patch.object(supervisor.Path, "home", return_value=source_home):
            supervisor.prepare_gemini_cli_home(scope_dir, fail_fast_on_rate_limit=True)

        settings = json.loads((scope_dir / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["security"]["auth"]["selectedType"], "oauth-personal")
        self.assertTrue(settings["general"]["retryFetchErrors"])
        self.assertEqual(settings["general"]["maxAttempts"], 1)

    def test_gemini_burst_env_enables_fail_fast_only_when_fallback_model_is_configured(self) -> None:
        source_home = Path(tempfile.mkdtemp(prefix="lagent gemini source "))
        self.addCleanup(shutil.rmtree, source_home, True)
        (source_home / ".gemini").mkdir(parents=True, exist_ok=True)
        (source_home / ".gemini" / "settings.json").write_text(
            json.dumps({"general": {"retryFetchErrors": True}}, indent=2),
            encoding="utf-8",
        )

        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        with mock.patch.object(supervisor.Path, "home", return_value=source_home):
            adapter_no_fallback = supervisor.GeminiAdapter(
                supervisor.ProviderConfig(provider="gemini", model="gemini-3.1-pro-preview", extra_args=[]),
                "worker",
                config,
                {},
            )
            adapter_no_fallback.burst_env()
            no_fallback_settings = json.loads(
                (config.state_dir / "scopes" / "gemini-worker" / ".gemini" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("maxAttempts", no_fallback_settings.get("general", {}))

        fallback_scope = config.state_dir / "scopes" / "gemini-reviewer"
        with mock.patch.object(supervisor.Path, "home", return_value=source_home):
            adapter_with_fallback = supervisor.GeminiAdapter(
                supervisor.ProviderConfig(
                    provider="gemini",
                    model="gemini-3.1-pro-preview",
                    extra_args=[],
                    fallback_model="gemini-2.5-flash",
                ),
                "reviewer",
                config,
                {},
            )
            adapter_with_fallback.burst_env()
            fallback_settings = json.loads((fallback_scope / ".gemini" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(fallback_settings["general"]["maxAttempts"], 1)

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
        self.assertIn(f"recovery attempt 1 of {supervisor.stuck_recovery_attempt_limit(state)}", prompt)
        self.assertIn("carrier-set reformulation", prompt)
        self.assertIn("Prove the carrier-set reformulation", prompt)

    def test_branch_worker_prompt_uses_branch_stuck_recovery_limit(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = {
            "branch_lineage": [
                {
                    "episode_id": "episode-001",
                    "branch_name": "major-rewrite",
                    "summary": "rewrite",
                    "rewrite_scope": "major",
                }
            ],
            "last_review": {
                "phase": "proof_formalization",
                "decision": "STUCK",
                "reason": "The current branch is blocked.",
                "next_prompt": "",
                "cycle": 12,
            },
            "stuck_recovery_attempts": [
                {
                    "phase": "proof_formalization",
                    "attempt": 1,
                    "trigger_cycle": 12,
                    "diagnosis": "Missing support lemma.",
                    "creative_suggestion": "Try a branch-local rewrite.",
                    "why_this_might_work": "It weakens the local target.",
                    "worker_prompt": "Rewrite the local target first.",
                }
            ],
        }

        prompt = supervisor.build_worker_prompt(config, state, "proof_formalization", False)

        self.assertIn(
            f"recovery attempt 1 of {supervisor.MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS}",
            prompt,
        )

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

    def test_proof_phase_worker_prompt_keeps_legacy_done_wording(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        prompt = supervisor.build_worker_prompt(config, {}, "proof_formalization", False)

        self.assertIn("`DONE` means the full workflow is complete.", prompt)

    def test_cleanup_phase_prompts_focus_on_safe_polish(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        worker_prompt = supervisor.build_worker_prompt(
            config,
            {},
            supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
            False,
        )
        reviewer_prompt = supervisor.build_reviewer_prompt(
            config,
            {"review_log": []},
            supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
            "worker terminal output",
            '{"status":"NOT_STUCK"}',
            {"build": {"ok": True}, "sorries": {"count": 0}, "axioms": {"unapproved": []}},
            False,
        )

        self.assertIn("PROOF COMPLETE - style cleanup", worker_prompt)
        self.assertIn("every burst must end with a fully buildable proof state", worker_prompt)
        self.assertIn("warning cleanup", worker_prompt)
        self.assertIn("optional polish, not mission-critical", reviewer_prompt)
        self.assertIn("preserve the last good proof-complete commit", reviewer_prompt)

    def test_cleanup_phase_reviewer_decisions(self) -> None:
        self.assertEqual(
            supervisor.phase_specific_reviewer_decisions(supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP),
            ("CONTINUE", "STUCK", "DONE"),
        )

    def test_prompts_include_policy_notes_when_configured(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        policy = supervisor.Policy(
            stuck_recovery=supervisor.StuckRecoveryPolicy(),
            branching=supervisor.BranchingPolicy(
                evaluation_cycle_budget=config.branching.evaluation_cycle_budget,
                poll_seconds=config.branching.poll_seconds,
                proposal_cooldown_reviews=4,
                replacement_min_confidence=0.85,
            ),
            timing=supervisor.TimingPolicy(
                sleep_seconds=config.sleep_seconds,
                agent_retry_delays_seconds=supervisor.DEFAULT_AGENT_CLI_RETRY_DELAYS_SECONDS,
            ),
            codex_budget_pause=supervisor.CodexBudgetPausePolicy(),
            prompt_notes=supervisor.PromptNotesPolicy(
                worker="Prefer paper-facing names.",
                reviewer="Require a narrower frontier before continuing.",
                branching="Prefer materially different routes.",
            ),
        )

        worker_prompt = supervisor.build_worker_prompt(
            config,
            {"review_log": []},
            "proof_formalization",
            False,
            policy=policy,
        )
        reviewer_prompt = supervisor.build_reviewer_prompt(
            config,
            {"review_log": []},
            "proof_formalization",
            "worker terminal output",
            '{"status":"NOT_STUCK"}',
            {"build": {"ok": True}, "sorries": {"count": 0}},
            False,
            policy=policy,
        )
        branching_prompt = supervisor.build_branch_strategy_prompt(
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
            policy=policy,
        )

        self.assertIn("Prefer paper-facing names.", worker_prompt)
        self.assertIn("Require a narrower frontier before continuing.", reviewer_prompt)
        self.assertIn("Prefer materially different routes.", branching_prompt)

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

    def test_branch_strategy_prompt_mentions_parent_managed_replacement_mode(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config.branching.max_current_branches = 1

        prompt = supervisor.build_branch_strategy_prompt(
            config,
            {"review_log": [], "branch_parent_max_current_branches": 2},
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

        self.assertIn("parent-managed branch frontier", prompt)
        self.assertIn("proposing up to 2 replacement child strategies", prompt)

    def test_branch_selection_question_for_state_uses_active_theorem_node(self) -> None:
        state = {
            "theorem_frontier": {
                **supervisor.default_theorem_frontier_payload("full"),
                "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                "active_leaf_id": "ri.local.graph_pair",
                "current": {
                    "blocker_cluster": "graph-pair local RI collapse",
                },
                "nodes": {
                    "ri.local.graph_pair": {
                        "node_id": "ri.local.graph_pair",
                        "kind": "support",
                        "status": "active",
                        "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                        "lean_statement": "theorem ri_local_graph_pair : True := by trivial",
                        "lean_anchor": "Twobites.IndependentSets.ri_local_graph_pair",
                        "paper_provenance": "Section 4 RI collapse.",
                        "closure_mode": "leaf",
                        "parent_ids": [],
                        "child_ids": [],
                        "blocker_cluster": "graph-pair local RI collapse",
                        "acceptance_evidence": "Close the theorem.",
                        "notes": "The live theorem bottleneck.",
                    }
                },
            }
        }

        question = supervisor.branch_selection_question_for_state(state)

        self.assertIn("`ri.local.graph_pair|direct_proof|ri.local.graph_pair`", question)
        self.assertIn("`Twobites.IndependentSets.ri_local_graph_pair`", question)
        self.assertIn("finish formalizing the whole paper", question)

    def test_full_theorem_frontier_branch_strategy_prompt_mentions_active_node_rule(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        state = {
            "review_log": [],
                "theorem_frontier": {
                    **supervisor.default_theorem_frontier_payload("full"),
                    "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                    "active_leaf_id": "ri.local.graph_pair",
                    "nodes": {
                    "ri.local.graph_pair": {
                        "node_id": "ri.local.graph_pair",
                        "kind": "support",
                        "status": "active",
                        "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                        "lean_statement": "theorem ri_local_graph_pair : True := by trivial",
                        "lean_anchor": "Twobites.IndependentSets.ri_local_graph_pair",
                        "paper_provenance": "Section 4 RI collapse.",
                        "closure_mode": "leaf",
                        "parent_ids": [],
                        "child_ids": [],
                        "blocker_cluster": "graph-pair local RI collapse",
                        "acceptance_evidence": "Close the theorem.",
                        "notes": "The live theorem bottleneck.",
                    }
                },
            },
        }

        prompt = supervisor.build_branch_strategy_prompt(
            config,
            state,
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

        self.assertIn("active theorem node `ri.local.graph_pair`", prompt)
        self.assertIn("Do not propose branches that widen the frontier above or outside that node's subtree.", prompt)
        self.assertIn("If the routes still share the same blocker cluster and unresolved hypothesis set, prefer `NO_BRANCH`.", prompt)
        self.assertIn("wrapper-building or bookkeeping variants", prompt)

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

    def test_full_theorem_frontier_branch_selection_prompt_mentions_dependency_reduction(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        state = {
            "review_log": [],
            "theorem_frontier": {
                **supervisor.default_theorem_frontier_payload("full"),
                "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                "active_leaf_id": "ri.local.graph_pair",
                "nodes": {
                    "ri.local.graph_pair": {
                        "node_id": "ri.local.graph_pair",
                        "kind": "support",
                        "status": "active",
                        "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                        "lean_statement": "theorem ri_local_graph_pair : True := by trivial",
                        "lean_anchor": "Twobites.IndependentSets.ri_local_graph_pair",
                        "paper_provenance": "Section 4 RI collapse.",
                        "closure_mode": "leaf",
                        "parent_ids": [],
                        "child_ids": [],
                        "blocker_cluster": "graph-pair local RI collapse",
                        "acceptance_evidence": "Close the theorem.",
                        "notes": "The live theorem bottleneck.",
                    }
                },
            },
        }

        prompt = supervisor.build_branch_selection_prompt(
            config,
            state,
            "proof_formalization",
            {
                "id": "episode-001",
                "selection_question": "Which branch seems more likely to close theorem-frontier node `ri.local.graph_pair` and then finish formalizing the whole paper?",
            },
            [
                {
                    "name": "continue-current-route",
                    "progress_reviews": 5,
                    "latest_review_decision": "CONTINUE",
                    "theorem_frontier_open_hypotheses_count": 3,
                },
                {
                    "name": "major-rewrite",
                    "progress_reviews": 5,
                    "latest_review_decision": "CONTINUE",
                    "theorem_frontier_open_hypotheses_count": 2,
                },
            ],
            False,
        )

        self.assertIn("strictly reduces the unresolved hypothesis set", prompt)
        self.assertIn("shrinking the anchored node's unresolved dependency set", prompt)
        self.assertIn("Penalize branches that mainly add wrappers", prompt)

    def test_branch_selection_prompt_tightens_after_initial_checkpoint(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)

        prompt = supervisor.build_branch_selection_prompt(
            config,
            {"review_log": []},
            "proof_formalization",
            {
                "id": "episode-001",
                "base_review_count": 0,
                "next_selection_review_target": 30,
                "selection_continue_count": 1,
                "selection_question": "Which branch seems more likely to eventually succeed at formalizing the whole paper?",
            },
            [
                {"name": "continue-current-route", "progress_reviews": 15, "latest_review_decision": "CONTINUE"},
                {"name": "major-rewrite", "progress_reviews": 15, "latest_review_decision": "CONTINUE"},
            ],
            False,
        )

        self.assertIn("already past the initial 20-review checkpoint", prompt)
        self.assertIn("Do not keep a clearly less promising branch alive merely because it is still making local progress.", prompt)
        self.assertIn("genuinely close", prompt)

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

    def test_validate_branch_strategy_decision_accepts_parent_controlled_limit(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config.branching.max_current_branches = 1

        decision = supervisor.validate_branch_strategy_decision(
            config,
            "proof_formalization",
            {
                "phase": "proof_formalization",
                "branch_decision": "branch",
                "confidence": 0.85,
                "reason": "The parent frontier should consider a replacement split.",
                "strategies": [
                    {
                        "name": "Local Transversality",
                        "summary": "Push the endpoint-face extraction route.",
                        "worker_prompt": "Pursue the local transversality route.",
                        "why_this_might_eventually_succeed": "It may isolate the exact missing one-cell hypothesis.",
                        "rewrite_scope": "incremental",
                    },
                    {
                        "name": "Boundary Genericity",
                        "summary": "Switch to the boundary genericity route.",
                        "worker_prompt": "Pursue the boundary genericity route.",
                        "why_this_might_eventually_succeed": "It matches the paper's local geometry more closely.",
                        "rewrite_scope": "major",
                    },
                ],
            },
            {"branch_parent_max_current_branches": 2},
        )

        self.assertEqual(decision["branch_decision"], "BRANCH")
        self.assertEqual(len(decision["strategies"]), 2)

    def test_validate_branch_strategy_decision_requires_frontier_anchor_in_full_mode(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        state = {
            "theorem_frontier": {
                **supervisor.default_theorem_frontier_payload("full"),
                "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                "active_leaf_id": "ri.local.graph_pair",
                "nodes": {
                    "ri.local.graph_pair": supervisor.theorem_frontier_node_record(
                        {
                            "node_id": "ri.local.graph_pair",
                            "kind": "support",
                            "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                            "lean_statement": "theorem ri_local_graph_pair : True := by trivial",
                            "lean_anchor": "Twobites.IndependentSets.ri_local_graph_pair",
                            "paper_provenance": "Section 4 RI collapse.",
                            "closure_mode": "leaf",
                            "blocker_cluster": "graph-pair local RI collapse",
                            "acceptance_evidence": "Close the theorem.",
                            "notes": "Live frontier node.",
                        },
                        status="active",
                        parent_ids=[],
                        child_ids=[],
                    )
                },
            }
        }

        with self.assertRaisesRegex(supervisor.SupervisorError, "frontier_anchor_node_id"):
            supervisor.validate_branch_strategy_decision(
                config,
                "proof_formalization",
                {
                    "phase": "proof_formalization",
                    "branch_decision": "branch",
                    "confidence": 0.7,
                    "reason": "There are two routes.",
                    "strategies": [
                        {
                            "name": "route-a",
                            "summary": "A",
                            "worker_prompt": "A",
                            "why_this_might_eventually_succeed": "A",
                            "rewrite_scope": "incremental",
                        },
                        {
                            "name": "route-b",
                            "summary": "B",
                            "worker_prompt": "B",
                            "why_this_might_eventually_succeed": "B",
                            "rewrite_scope": "major",
                        },
                    ],
                },
                state,
            )

    def test_validate_branch_selection_decision_requires_frontier_anchor_in_full_mode(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        state = {
            "theorem_frontier": {
                **supervisor.default_theorem_frontier_payload("full"),
                "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                "active_leaf_id": "ri.local.graph_pair",
                "nodes": {
                    "ri.local.graph_pair": supervisor.theorem_frontier_node_record(
                        {
                            "node_id": "ri.local.graph_pair",
                            "kind": "support",
                            "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                            "lean_statement": "theorem ri_local_graph_pair : True := by trivial",
                            "lean_anchor": "Twobites.IndependentSets.ri_local_graph_pair",
                            "paper_provenance": "Section 4 RI collapse.",
                            "closure_mode": "leaf",
                            "blocker_cluster": "graph-pair local RI collapse",
                            "acceptance_evidence": "Close the theorem.",
                            "notes": "Live frontier node.",
                        },
                        status="active",
                        parent_ids=[],
                        child_ids=[],
                    )
                },
            }
        }

        with self.assertRaisesRegex(supervisor.SupervisorError, "frontier_anchor_node_id"):
            supervisor.validate_branch_selection_decision(
                config,
                "proof_formalization",
                {
                    "phase": "proof_formalization",
                    "selection_decision": "SELECT_BRANCH",
                    "confidence": 0.9,
                    "reason": "One route is better.",
                    "selected_branch": "major-rewrite",
                },
                ["major-rewrite", "continue-current-route"],
                state,
            )

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

    def test_should_consider_branching_when_parent_coordinated_replacement_is_enabled(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config.branching.max_current_branches = 1

        should_branch = supervisor.should_consider_branching(
            config,
            {
                "cycle": 130,
                "last_branch_consideration_cycle": 0,
                "branch_parent_max_current_branches": 2,
            },
            "proof_formalization",
            {
                "phase": "proof_formalization",
                "decision": "STUCK",
                "confidence": 0.89,
                "reason": "The route is blocked and now clearly splits into two better alternatives.",
                "next_prompt": "",
                "cycle": 130,
            },
        )

        self.assertTrue(should_branch)

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

    def test_branch_stuck_recovery_attempt_state_machine_uses_branch_limit(self) -> None:
        state = {
            "cycle": 51,
            "branch_lineage": [
                {
                    "episode_id": "episode-001",
                    "branch_name": "major-rewrite",
                    "summary": "rewrite",
                    "rewrite_scope": "major",
                }
            ],
            "last_review": {"decision": "STUCK", "cycle": 51},
        }

        self.assertEqual(
            supervisor.stuck_recovery_attempt_limit(state),
            supervisor.MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS,
        )
        for index in range(1, supervisor.MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS + 1):
            self.assertTrue(supervisor.can_attempt_stuck_recovery(state))
            attempt = supervisor.record_stuck_recovery_attempt(
                state,
                trigger_cycle=50 + index,
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
            if index < supervisor.MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS:
                state["last_review"] = {"decision": "STUCK", "cycle": 51 + index}

        state["last_review"] = {
            "decision": "STUCK",
            "cycle": 51 + supervisor.MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS,
        }
        self.assertTrue(supervisor.stuck_recovery_exhausted(state))
        self.assertFalse(supervisor.can_attempt_stuck_recovery(state))

    def test_branch_episode_snapshots_marks_exhausted_stuck_branch(self) -> None:
        repo_path = self.make_repo()
        branch_path = repo_path.parent / "branch"
        branch_path.mkdir(parents=True, exist_ok=True)
        state_dir = branch_path / ".agent-supervisor"
        state_dir.mkdir(parents=True, exist_ok=True)
        supervisor.JsonFile.dump(
            state_dir / "state.json",
            {
                "branch_lineage": [
                    {
                        "episode_id": "episode-001",
                        "branch_name": "major-rewrite",
                        "summary": "rewrite",
                        "rewrite_scope": "major",
                    }
                ],
                "cycle": 40,
                "phase": "proof_formalization",
                "last_review": {
                    "decision": "STUCK",
                    "reason": "Still blocked.",
                    "cycle": 40,
                },
                "stuck_recovery_last_trigger_cycle": 39,
                "stuck_recovery_attempts": [
                    {"attempt": 1, "trigger_cycle": 11},
                    {"attempt": 2, "trigger_cycle": 18},
                    {"attempt": 3, "trigger_cycle": 27},
                    {"attempt": 4, "trigger_cycle": 39},
                ],
            },
        )

        snapshots = supervisor.branch_episode_snapshots(
            {
                "base_review_count": 0,
                "branches": [
                    {
                        "name": "major-rewrite",
                        "status": "active",
                        "worktree_path": str(branch_path),
                        "config_path": str(branch_path / "branch.json"),
                    }
                ],
            }
        )

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["branch_status"], "active")
        self.assertEqual(
            snapshots[0]["stuck_recovery_attempt_limit"],
            supervisor.MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS,
        )
        self.assertTrue(snapshots[0]["stuck_recovery_exhausted"])

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
        prune_mock.assert_called_once()
        self.assertEqual(prune_mock.call_args.args[:4], (config, state, state["active_branch_episode"], "major-rewrite"))
        self.assertIn("policy", prune_mock.call_args.kwargs)

    def test_monitor_active_branch_episode_auto_selects_last_survivor_after_pruning_exhausted_branch(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = {
            "cycle": 44,
            "active_branch_episode": {
                "id": "episode-002",
                "phase": "proof_formalization",
                "branches": [
                    {"name": "boundary-genericity", "status": "active"},
                    {"name": "local-transversality", "status": "active"},
                ],
            },
        }
        reviewer = DummyAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "reviewer",
            config,
            state,
            ["bash", "-lc", "exit 0"],
        )

        first_snapshots = [
            {
                "name": "boundary-genericity",
                "branch_status": "active",
                "stuck_recovery_exhausted": True,
                "stuck_recovery_attempt_limit": supervisor.MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS,
                "cycle": 44,
                "review_count": 50,
                "progress_reviews": 6,
                "phase": "proof_formalization",
            },
            {
                "name": "local-transversality",
                "branch_status": "active",
                "stuck_recovery_exhausted": False,
                "stuck_recovery_attempt_limit": supervisor.MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS,
                "cycle": 45,
                "review_count": 51,
                "progress_reviews": 7,
                "phase": "proof_formalization",
            },
        ]
        second_snapshots = [
            {
                "name": "boundary-genericity",
                "branch_status": "dead",
                "stuck_recovery_exhausted": False,
                "stuck_recovery_attempt_limit": supervisor.MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS,
                "cycle": 44,
                "review_count": 50,
                "progress_reviews": 6,
                "phase": "proof_formalization",
            },
            {
                "name": "local-transversality",
                "branch_status": "active",
                "stuck_recovery_exhausted": False,
                "stuck_recovery_attempt_limit": supervisor.MAX_BRANCH_STUCK_RECOVERY_ATTEMPTS,
                "cycle": 45,
                "review_count": 51,
                "progress_reviews": 7,
                "phase": "proof_formalization",
            },
        ]

        def fake_mark_dead(_config, _state, episode, branch_name, *, reason, cycle):
            for branch in episode["branches"]:
                if branch["name"] == branch_name:
                    branch["status"] = "dead"
                    branch["pruned_reason"] = reason
                    branch["pruned_cycle"] = cycle
                    return True
            return False

        with (
            mock.patch.object(supervisor, "branch_episode_snapshots", side_effect=[first_snapshots, second_snapshots]),
            mock.patch.object(supervisor, "mark_branch_dead_in_episode", side_effect=fake_mark_dead) as mark_dead_mock,
            mock.patch.object(
                supervisor,
                "record_automatic_branch_selection",
                return_value={
                    "phase": "proof_formalization",
                    "selection_decision": "SELECT_BRANCH",
                    "confidence": 1.0,
                    "reason": "All other branches were pruned.",
                    "selected_branch": "local-transversality",
                    "automatic": True,
                },
            ) as auto_select_mock,
            mock.patch.object(supervisor, "run_branch_selection_review") as selection_review_mock,
            mock.patch.object(
                supervisor,
                "prune_branch_episode",
                return_value={
                    "name": "local-transversality",
                    "worktree_path": "/tmp/local-transversality",
                },
            ) as prune_mock,
        ):
            result = supervisor.monitor_active_branch_episode(config, state, reviewer, "proof_formalization")

        self.assertEqual(result, 0)
        mark_dead_mock.assert_called_once()
        auto_select_mock.assert_called_once()
        selection_review_mock.assert_not_called()
        prune_mock.assert_called_once()
        self.assertEqual(
            prune_mock.call_args.args[:4],
            (config, state, state["active_branch_episode"], "local-transversality"),
        )
        self.assertIn("policy", prune_mock.call_args.kwargs)

    def test_monitor_active_branch_episode_rejects_pending_replacement_and_restarts_branch(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = {
            "cycle": 44,
            "active_branch_episode": {
                "id": "episode-002",
                "phase": "proof_formalization",
                "branches": [
                    {"name": "boundary-genericity", "status": "active"},
                    {"name": "local-transversality", "status": "active"},
                ],
            },
        }
        reviewer = DummyAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "reviewer",
            config,
            state,
            ["bash", "-lc", "exit 0"],
        )
        snapshots = [
            {
                "name": "boundary-genericity",
                "branch_status": "active",
                "stuck_recovery_exhausted": False,
                "config_path": "/tmp/boundary-genericity.json",
                "supervisor_session": "boundary-genericity-supervisor",
                "review_count": 50,
                "progress_reviews": 6,
                "cycle": 44,
                "phase": "proof_formalization",
                "pending_branch_proposal": {
                    "branch_decision": "BRANCH",
                    "confidence": 0.91,
                    "strategies": [
                        {"name": "route-a"},
                        {"name": "route-b"},
                    ],
                },
                "pending_branch_proposal_confidence": 0.91,
                "pending_branch_proposal_strategy_count": 2,
            },
            {
                "name": "local-transversality",
                "branch_status": "active",
                "stuck_recovery_exhausted": False,
                "config_path": "/tmp/local-transversality.json",
                "supervisor_session": "local-transversality-supervisor",
                "review_count": 51,
                "progress_reviews": 7,
                "cycle": 45,
                "phase": "proof_formalization",
            },
        ]
        snapshots_after_reject = [
            {
                **snapshots[0],
                "pending_branch_proposal": None,
                "pending_branch_proposal_confidence": None,
                "pending_branch_proposal_strategy_count": 0,
            },
            dict(snapshots[1]),
        ]

        with (
            mock.patch.object(supervisor, "branch_episode_snapshots", side_effect=[snapshots, snapshots_after_reject]),
            mock.patch.object(
                supervisor,
                "run_branch_replacement_review",
                return_value={
                    "phase": "proof_formalization",
                    "replacement_decision": "KEEP_FRONTIER",
                    "confidence": 0.72,
                    "reason": "The proposal is still too speculative.",
                },
            ) as replacement_mock,
            mock.patch.object(supervisor, "clear_pending_branch_proposal_in_snapshot") as clear_mock,
            mock.patch.object(supervisor, "restart_branch_supervisor_from_snapshot") as restart_mock,
            mock.patch.object(supervisor, "branch_episode_ready_for_selection", return_value=False),
            mock.patch.object(supervisor.time, "sleep", side_effect=RuntimeError("stop loop")),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                supervisor.monitor_active_branch_episode(config, state, reviewer, "proof_formalization")

        replacement_mock.assert_called_once()
        clear_mock.assert_called_once_with(snapshots[0], cooldown_reviews=supervisor.BRANCH_PROPOSAL_COOLDOWN_REVIEWS)
        restart_mock.assert_called_once_with(snapshots[0])

    def test_monitor_active_branch_episode_accepts_pending_replacement_and_launches_nested_episode(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = {
            "cycle": 44,
            "active_branch_episode": {
                "id": "episode-002",
                "phase": "proof_formalization",
                "branches": [
                    {"name": "boundary-genericity", "status": "active"},
                    {"name": "local-transversality", "status": "active"},
                ],
            },
        }
        reviewer = DummyAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "reviewer",
            config,
            state,
            ["bash", "-lc", "exit 0"],
        )
        proposal = {
            "phase": "proof_formalization",
            "branch_decision": "BRANCH",
            "confidence": 0.91,
            "reason": "Both proposed child routes are better long-term bets.",
            "strategies": [
                {"name": "route-a", "summary": "a", "worker_prompt": "a", "why_this_might_eventually_succeed": "a", "rewrite_scope": "major"},
                {"name": "route-b", "summary": "b", "worker_prompt": "b", "why_this_might_eventually_succeed": "b", "rewrite_scope": "major"},
            ],
        }
        snapshots = [
            {
                "name": "boundary-genericity",
                "branch_status": "active",
                "stuck_recovery_exhausted": False,
                "config_path": "/tmp/boundary-genericity.json",
                "supervisor_session": "boundary-genericity-supervisor",
                "review_count": 50,
                "progress_reviews": 6,
                "cycle": 44,
                "phase": "proof_formalization",
                "pending_branch_proposal": proposal,
                "pending_branch_proposal_confidence": 0.91,
                "pending_branch_proposal_strategy_count": 2,
            },
            {
                "name": "local-transversality",
                "branch_status": "active",
                "stuck_recovery_exhausted": False,
                "config_path": "/tmp/local-transversality.json",
                "supervisor_session": "local-transversality-supervisor",
                "review_count": 51,
                "progress_reviews": 7,
                "cycle": 45,
                "phase": "proof_formalization",
            },
        ]

        with (
            mock.patch.object(supervisor, "branch_episode_snapshots", return_value=snapshots),
            mock.patch.object(
                supervisor,
                "run_branch_replacement_review",
                return_value={
                    "phase": "proof_formalization",
                    "replacement_decision": "REPLACE_WITH_PROPOSAL",
                    "confidence": 0.91,
                    "reason": "The proposed split is clearly superior.",
                },
            ) as replacement_mock,
            mock.patch.object(
                supervisor,
                "record_branch_selection_decision",
                return_value={
                    "phase": "proof_formalization",
                    "selection_decision": "SELECT_BRANCH",
                    "confidence": 0.91,
                    "reason": "The proposed split is clearly superior.",
                    "selected_branch": "boundary-genericity",
                    "replacement": True,
                },
            ) as record_selection_mock,
            mock.patch.object(
                supervisor,
                "prune_branch_episode",
                return_value={
                    "name": "boundary-genericity",
                    "config_path": "/tmp/boundary-genericity.json",
                    "supervisor_session": "boundary-genericity-supervisor",
                },
            ) as prune_mock,
            mock.patch.object(
                supervisor,
                "launch_nested_branch_episode_from_snapshot",
                return_value={"id": "episode-003", "branches": [{"name": "route-a"}, {"name": "route-b"}]},
            ) as launch_mock,
        ):
            result = supervisor.monitor_active_branch_episode(config, state, reviewer, "proof_formalization")

        self.assertEqual(result, 0)
        replacement_mock.assert_called_once()
        record_selection_mock.assert_called_once()
        prune_mock.assert_called_once()
        self.assertEqual(
            prune_mock.call_args.args[:4],
            (config, state, state["active_branch_episode"], "boundary-genericity"),
        )
        self.assertIn("policy", prune_mock.call_args.kwargs)
        launch_mock.assert_called_once()

    def test_monitor_active_branch_episode_continue_branching_uses_shorter_rechecks(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = {
            "active_branch_episode": {
                "id": "episode-002",
                "phase": "proof_formalization",
                "base_review_count": 44,
                "next_selection_review_target": 64,
                "evaluation_cycle_budget": 20,
                "selection_continue_count": 0,
                "branches": [
                    {"name": "boundary-genericity", "status": "active"},
                    {"name": "local-transversality", "status": "active"},
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
        snapshots = [
            {"name": "boundary-genericity", "branch_status": "active", "review_count": 64, "progress_reviews": 20, "cycle": 65},
            {"name": "local-transversality", "branch_status": "active", "review_count": 64, "progress_reviews": 12, "cycle": 60},
        ]

        with (
            mock.patch.object(supervisor, "branch_episode_snapshots", return_value=snapshots),
            mock.patch.object(supervisor, "branch_episode_ready_for_selection", return_value=True),
            mock.patch.object(
                supervisor,
                "run_branch_selection_review",
                return_value={
                    "phase": "proof_formalization",
                    "selection_decision": "CONTINUE_BRANCHING",
                    "confidence": 0.82,
                    "reason": "Too early to prune.",
                    "selected_branch": "",
                },
            ),
            mock.patch.object(supervisor.time, "sleep", side_effect=RuntimeError("stop loop")),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                supervisor.monitor_active_branch_episode(config, state, reviewer, "proof_formalization")

        episode = state["active_branch_episode"]
        self.assertEqual(episode["selection_continue_count"], 1)
        self.assertEqual(episode["next_selection_review_target"], 69)

    def test_build_child_branch_state_preserves_history_and_extends_lineage(self) -> None:
        state = {
            "branch_episode_counter": 2,
            "branch_history": [{"id": "episode-001", "selected_branch": "route-a", "status": "selected"}],
            "branch_lineage": [{"episode_id": "episode-001", "branch_name": "route-a", "summary": "old", "rewrite_scope": "major"}],
            "last_theorem_frontier_worker_update": {"phase": "proof_formalization"},
            "last_theorem_frontier_review": {"phase": "proof_formalization"},
            "last_theorem_frontier_paper_review": {"decision": "APPROVE"},
            "last_theorem_frontier_nl_proof_review": {"decision": "APPROVE"},
            "theorem_frontier": {
                **supervisor.default_theorem_frontier_payload("full"),
                "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                "active_leaf_id": "ri.local.graph_pair",
                "current_action": "CLOSE",
                "current": {
                    "cycle": 40,
                    "reviewed_node_id": "ri.local.graph_pair",
                    "next_active_leaf_id": "ri.local.graph_pair",
                    "requested_action": "CLOSE",
                    "assessed_action": "CLOSE",
                    "outcome": "STILL_OPEN",
                    "blocker_cluster": "graph-pair local RI collapse",
                    "cone_purity": "LOW",
                    "open_hypotheses": ["hLossGap"],
                    "justification": "Still open.",
                    "updated_at": "2026-03-29T00:00:00-04:00",
                },
                "metrics": {
                    "active_leaf_age": 4,
                    "blocker_cluster_age": 4,
                    "closed_nodes_count": 1,
                    "refuted_nodes_count": 0,
                    "paper_nodes_closed": 0,
                    "failed_close_attempts": 2,
                    "low_cone_purity_streak": 2,
                    "cone_purity": "LOW",
                    "structural_churn": 3,
                },
                "escalation": {"required": True, "reasons": ["same blocker cluster persisted for five reviews; mandatory escalation"]},
                "nodes": {
                    "ri.local.graph_pair": {
                        "node_id": "ri.local.graph_pair",
                        "kind": "support",
                        "status": "active",
                        "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                        "lean_statement": "theorem ri_local_graph_pair : True := by trivial",
                        "lean_anchor": "Twobites.IndependentSets.ri_local_graph_pair",
                        "paper_provenance": "Section 4 RI collapse.",
                        "closure_mode": "leaf",
                        "parent_ids": [],
                        "child_ids": [],
                        "blocker_cluster": "graph-pair local RI collapse",
                        "acceptance_evidence": "Close the theorem.",
                        "notes": "The live theorem bottleneck.",
                    }
                },
            },
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
            parent_max_current_branches=2,
        )

        self.assertEqual(child_state["branch_episode_counter"], 2)
        self.assertEqual(len(child_state["branch_history"]), 1)
        self.assertEqual(child_state["branch_parent_max_current_branches"], 2)
        self.assertEqual(
            [entry["branch_name"] for entry in child_state["branch_lineage"]],
            ["route-a", "route-b"],
        )
        self.assertIsNone(child_state["last_theorem_frontier_worker_update"])
        self.assertIsNone(child_state["last_theorem_frontier_review"])
        self.assertIsNone(child_state["last_theorem_frontier_paper_review"])
        self.assertIsNone(child_state["last_theorem_frontier_nl_proof_review"])
        self.assertEqual(child_state["theorem_frontier"]["metrics"]["active_leaf_age"], 0)
        self.assertEqual(child_state["theorem_frontier"]["metrics"]["blocker_cluster_age"], 0)
        self.assertEqual(child_state["theorem_frontier"]["metrics"]["failed_close_attempts"], 0)
        self.assertEqual(child_state["theorem_frontier"]["metrics"]["low_cone_purity_streak"], 0)
        self.assertEqual(child_state["theorem_frontier"]["metrics"]["structural_churn"], 0)
        self.assertIsNone(child_state["theorem_frontier"]["metrics"]["cone_purity"])
        self.assertEqual(child_state["theorem_frontier"]["current_action"], None)
        self.assertEqual(child_state["theorem_frontier"]["escalation"], {"required": False, "reasons": []})
        self.assertIsNone(child_state["theorem_frontier"]["current"])

    def test_create_branch_episode_writes_child_theorem_frontier_file(self) -> None:
        repo_path = self.make_repo()
        self.git(repo_path, "init", "-b", "main")
        self.git(repo_path, "config", "user.name", "Test User")
        self.git(repo_path, "config", "user.email", "test@example.com")
        (repo_path / "Main.lean").write_text("theorem t : True := by trivial\n", encoding="utf-8")
        self.git(repo_path, "add", ".")
        self.git(repo_path, "commit", "-m", "init")

        config = self.make_config(repo_path, theorem_frontier_phase="full")
        state = {
            "cycle": 12,
            "review_log": [],
            "branch_history": [],
            "branch_lineage": [],
            "last_review": {
                "phase": "proof_formalization",
                "decision": "CONTINUE",
                "reason": "Try alternatives.",
                "cycle": 12,
            },
            "theorem_frontier": {
                **supervisor.default_theorem_frontier_payload("full"),
                "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                "active_leaf_id": "ri.local.graph_pair",
                "current_action": "CLOSE",
                "current": {
                    "cycle": 12,
                    "reviewed_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                    "reviewed_node_id": "ri.local.graph_pair",
                    "next_active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                    "next_active_leaf_id": "ri.local.graph_pair",
                    "requested_action": "CLOSE",
                    "assessed_action": "CLOSE",
                    "outcome": "STILL_OPEN",
                    "blocker_cluster": "graph-pair local RI collapse",
                    "cone_purity": "HIGH",
                    "open_hypotheses": ["hLossGap"],
                    "justification": "Still open.",
                    "updated_at": "2026-03-29T00:00:00-04:00",
                },
                "metrics": {
                    "active_edge_age": 3,
                    "active_leaf_age": 3,
                    "blocker_cluster_age": 3,
                    "closed_nodes_count": 0,
                    "refuted_nodes_count": 0,
                    "paper_nodes_closed": 0,
                    "failed_close_attempts": 1,
                    "low_cone_purity_streak": 0,
                    "cone_purity": "HIGH",
                    "structural_churn": 1,
                },
                "nodes": {
                    "ri.local.graph_pair": {
                        "node_id": "ri.local.graph_pair",
                        "kind": "support",
                        "status": "active",
                        "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                        "lean_statement": "theorem ri_local_graph_pair : True := by trivial",
                        "lean_anchor": "Main.ri_local_graph_pair",
                        "paper_provenance": "Section 4 RI collapse.",
                        "closure_mode": "leaf",
                        "parent_ids": [],
                        "child_ids": [],
                        "blocker_cluster": "graph-pair local RI collapse",
                        "acceptance_evidence": "Close the theorem.",
                        "notes": "The live theorem bottleneck.",
                    }
                },
            },
        }
        supervisor.save_state(config, state)
        self.git(repo_path, "add", ".")
        self.git(repo_path, "commit", "-m", "add frontier state")

        proposal = {
            "phase": "proof_formalization",
            "branch_decision": "BRANCH",
            "frontier_anchor_node_id": "ri.local.graph_pair",
            "confidence": 0.8,
            "reason": "Two materially different routes.",
            "strategies": [
                {
                    "name": "route-a",
                    "summary": "continue",
                    "worker_prompt": "continue",
                    "why_this_might_eventually_succeed": "close directly",
                    "rewrite_scope": "incremental",
                },
                {
                    "name": "route-b",
                    "summary": "rewrite",
                    "worker_prompt": "rewrite",
                    "why_this_might_eventually_succeed": "replace route",
                    "rewrite_scope": "major",
                },
            ],
        }

        with mock.patch.object(supervisor, "start_supervisor_tmux_session"):
            episode = supervisor.create_branch_episode(
                config,
                state,
                "proof_formalization",
                state["last_review"],
                proposal,
            )

        self.assertEqual(episode["frontier_anchor_edge_id"], "ri.local.graph_pair|direct_proof|ri.local.graph_pair")
        self.assertEqual(episode["frontier_anchor_node_id"], "ri.local.graph_pair")
        child_path = Path(episode["branches"][0]["worktree_path"]) / ".agent-supervisor" / "theorem_frontier.json"
        self.assertTrue(child_path.exists())
        child_payload = supervisor.JsonFile.load(child_path, {})
        self.assertEqual(child_payload["active_edge_id"], "ri.local.graph_pair|direct_proof|ri.local.graph_pair")
        self.assertEqual(child_payload["active_leaf_id"], "ri.local.graph_pair")
        self.assertEqual(child_payload["metrics"]["active_edge_age"], 0)
        self.assertEqual(child_payload["metrics"]["active_leaf_age"], 0)
        self.assertEqual(child_payload["metrics"]["blocker_cluster_age"], 0)
        self.assertEqual(child_payload["current"], None)

    def test_branch_episode_snapshots_include_theorem_frontier_summary(self) -> None:
        repo_path = self.make_repo()
        branch_path = repo_path.parent / "branch"
        branch_path.mkdir(parents=True, exist_ok=True)
        state_dir = branch_path / ".agent-supervisor"
        state_dir.mkdir(parents=True, exist_ok=True)
        supervisor.JsonFile.dump(
            state_dir / "state.json",
            {
                "cycle": 40,
                "phase": "proof_formalization",
                "last_review": {
                    "decision": "CONTINUE",
                    "reason": "The frontier is narrowing.",
                    "cycle": 40,
                },
                "last_validation": {"git": {"head": "abc123"}},
                "theorem_frontier": {
                    **supervisor.default_theorem_frontier_payload("full"),
                    "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                    "active_leaf_id": "ri.local.graph_pair",
                    "current_action": "EXPAND",
                    "current": {
                        "cycle": 40,
                        "reviewed_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                        "reviewed_node_id": "ri.local.graph_pair",
                        "next_active_edge_id": "ri.local.child|direct_proof|ri.local.child",
                        "next_active_leaf_id": "ri.local.child",
                        "requested_action": "EXPAND",
                        "assessed_action": "EXPAND",
                        "outcome": "EXPANDED",
                        "blocker_cluster": "graph-pair local RI collapse",
                        "cone_purity": "HIGH",
                        "open_hypotheses": ["hLossGap", "hBlueCap"],
                        "justification": "Split the local theorem.",
                        "updated_at": "2026-03-29T00:00:00-04:00",
                    },
                    "metrics": {
                        "active_edge_age": 1,
                        "active_leaf_age": 1,
                        "blocker_cluster_age": 3,
                        "closed_nodes_count": 0,
                        "refuted_nodes_count": 0,
                        "paper_nodes_closed": 0,
                        "failed_close_attempts": 1,
                        "low_cone_purity_streak": 0,
                        "cone_purity": "HIGH",
                        "structural_churn": 0,
                    },
                    "escalation": {"required": True, "reasons": ["same blocker cluster persisted for five reviews; mandatory escalation"]},
                    "nodes": {
                        "ri.local.graph_pair": {
                            "node_id": "ri.local.graph_pair",
                            "kind": "support",
                            "status": "active",
                            "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                            "lean_statement": "theorem ri_local_graph_pair : True := by trivial",
                            "lean_anchor": "Twobites.IndependentSets.ri_local_graph_pair",
                            "paper_provenance": "Section 4 RI collapse.",
                            "closure_mode": "leaf",
                            "parent_ids": [],
                            "child_ids": [],
                            "blocker_cluster": "graph-pair local RI collapse",
                            "acceptance_evidence": "Close the theorem.",
                            "notes": "The live theorem bottleneck.",
                        }
                    },
                },
            },
        )

        snapshots = supervisor.branch_episode_snapshots(
            {
                "base_review_count": 0,
                "frontier_anchor_node_id": "ri.local.graph_pair",
                "branches": [
                    {
                        "name": "major-rewrite",
                        "status": "active",
                        "frontier_anchor_node_id": "ri.local.graph_pair",
                        "worktree_path": str(branch_path),
                        "config_path": str(branch_path / "branch.json"),
                    }
                ],
            }
        )

        self.assertEqual(snapshots[0]["frontier_anchor_node_id"], "ri.local.graph_pair")
        self.assertEqual(snapshots[0]["theorem_frontier_active_edge_id"], "ri.local.graph_pair|direct_proof|ri.local.graph_pair")
        self.assertEqual(snapshots[0]["theorem_frontier_active_leaf_id"], "ri.local.graph_pair")
        self.assertEqual(snapshots[0]["theorem_frontier_open_hypotheses_count"], 2)
        self.assertEqual(snapshots[0]["theorem_frontier_blocker_cluster"], "graph-pair local RI collapse")
        self.assertTrue(snapshots[0]["theorem_frontier_escalation_required"])

    def test_proposal_snapshot_can_replace_frontier_rejects_anchor_drift(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        episode = {
            "id": "episode-002",
            "frontier_anchor_node_id": "ri.local.graph_pair",
            "branches": [
                {"name": "boundary-genericity", "status": "active"},
                {"name": "local-transversality", "status": "active"},
            ],
        }
        snapshots = [
            {"name": "boundary-genericity", "branch_status": "active"},
            {"name": "local-transversality", "branch_status": "active"},
        ]
        proposal_snapshot = {
            "name": "boundary-genericity",
            "pending_branch_proposal_confidence": 0.95,
            "pending_branch_proposal": {
                "phase": "proof_formalization",
                "branch_decision": "BRANCH",
                "frontier_anchor_node_id": "ri.local.child",
                "confidence": 0.95,
                "reason": "Split lower in the tree.",
                "strategies": [{"name": "a"}, {"name": "b"}],
            },
        }

        self.assertFalse(
            supervisor.proposal_snapshot_can_replace_frontier(
                config,
                episode,
                snapshots,
                proposal_snapshot,
            )
        )

    def test_monitor_active_branch_episode_rejects_replacement_proposal_with_anchor_drift(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = {
            "cycle": 44,
            "active_branch_episode": {
                "id": "episode-002",
                "phase": "proof_formalization",
                "frontier_anchor_node_id": "ri.local.graph_pair",
                "branches": [
                    {"name": "boundary-genericity", "status": "active"},
                    {"name": "local-transversality", "status": "active"},
                ],
            },
        }
        reviewer = DummyAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "reviewer",
            config,
            state,
            ["bash", "-lc", "exit 0"],
        )
        snapshots = [
            {
                "name": "boundary-genericity",
                "branch_status": "active",
                "frontier_anchor_node_id": "ri.local.graph_pair",
                "pending_branch_proposal": {
                    "phase": "proof_formalization",
                    "branch_decision": "BRANCH",
                    "frontier_anchor_node_id": "ri.local.child",
                    "confidence": 0.91,
                    "reason": "Both proposed child routes are better long-term bets.",
                    "strategies": [
                        {"name": "route-a"},
                        {"name": "route-b"},
                    ],
                },
                "pending_branch_proposal_confidence": 0.91,
                "pending_branch_proposal_strategy_count": 2,
                "review_count": 50,
                "progress_reviews": 6,
                "cycle": 44,
                "phase": "proof_formalization",
                "config_path": "/tmp/boundary-genericity.json",
                "supervisor_session": "boundary-genericity-supervisor",
            },
            {
                "name": "local-transversality",
                "branch_status": "active",
                "frontier_anchor_node_id": "ri.local.graph_pair",
                "review_count": 51,
                "progress_reviews": 7,
                "cycle": 45,
                "phase": "proof_formalization",
            },
        ]

        with (
            mock.patch.object(supervisor, "branch_episode_snapshots", side_effect=[snapshots, RuntimeError("stop loop")]),
            mock.patch.object(supervisor, "clear_pending_branch_proposal_in_snapshot") as clear_mock,
            mock.patch.object(supervisor, "restart_branch_supervisor_from_snapshot") as restart_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                supervisor.monitor_active_branch_episode(config, state, reviewer, "proof_formalization")

        clear_mock.assert_called_once()
        restart_mock.assert_called_once_with(snapshots[0])

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
        validation_mock.assert_called_once_with(config, "proof_formalization", 12, previous_validation=None)
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
        validation_mock.assert_called_once_with(config, "proof_formalization", 12, previous_validation=None)
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

    def test_launch_tmux_burst_with_retries_uses_policy_retry_delays(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        adapter = DummyAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "worker",
            config,
            {},
            ["bash", "-lc", "exit 0"],
        )
        policy = supervisor.Policy(
            stuck_recovery=supervisor.StuckRecoveryPolicy(),
            branching=supervisor.BranchingPolicy(
                evaluation_cycle_budget=config.branching.evaluation_cycle_budget,
                poll_seconds=config.branching.poll_seconds,
                proposal_cooldown_reviews=5,
                replacement_min_confidence=0.8,
            ),
            timing=supervisor.TimingPolicy(
                sleep_seconds=config.sleep_seconds,
                agent_retry_delays_seconds=(5.0, 7.0),
            ),
            codex_budget_pause=supervisor.CodexBudgetPausePolicy(),
            prompt_notes=supervisor.PromptNotesPolicy(),
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
            mock.patch.object(supervisor, "launch_tmux_burst", side_effect=[failed, succeeded]),
            mock.patch.object(supervisor.time, "sleep") as sleep_mock,
        ):
            supervisor.launch_tmux_burst_with_retries(
                adapter,
                9,
                "prompt",
                stage_label="worker burst",
                policy=policy,
            )

        sleep_mock.assert_called_once_with(5.0)

    def test_launch_tmux_burst_with_retries_uses_gemini_fallback_on_rate_limit(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        adapter = supervisor.GeminiAdapter(
            supervisor.ProviderConfig(
                provider="gemini",
                model="gemini-3.1-pro-preview",
                extra_args=[],
                fallback_model="gemini-2.5-flash",
            ),
            "worker",
            config,
            {},
        )
        failed_log = repo_path / "gemini-failed.log"
        failed_log.write_text("429 RESOURCE_EXHAUSTED MODEL_CAPACITY_EXHAUSTED", encoding="utf-8")
        failed = {
            "captured_output": "Attempt 1 failed with status 429. MODEL_CAPACITY_EXHAUSTED",
            "artifact_path": repo_path / "artifact.json",
            "per_cycle_log": failed_log,
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
                stage_label="worker burst",
            )

        self.assertEqual(run, succeeded)
        self.assertEqual(launch_mock.call_count, 2)
        self.assertEqual(launch_mock.call_args_list[0].args[0].cfg.model, "gemini-3.1-pro-preview")
        self.assertEqual(launch_mock.call_args_list[1].args[0].cfg.model, "gemini-2.5-flash")
        sleep_mock.assert_not_called()

    def test_launch_tmux_burst_with_retries_does_not_use_gemini_fallback_for_non_rate_limit_failure(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        adapter = supervisor.GeminiAdapter(
            supervisor.ProviderConfig(
                provider="gemini",
                model="gemini-3.1-pro-preview",
                extra_args=[],
                fallback_model="gemini-2.5-flash",
            ),
            "worker",
            config,
            {},
        )
        failed = {
            "captured_output": "plain parse failure",
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
                stage_label="worker burst",
            )

        self.assertEqual(run, succeeded)
        self.assertEqual(launch_mock.call_count, 2)
        self.assertEqual([call.args[0].cfg.model for call in launch_mock.call_args_list], ["gemini-3.1-pro-preview", "gemini-3.1-pro-preview"])
        sleep_mock.assert_called_once_with(supervisor.AGENT_CLI_RETRY_DELAYS_SECONDS[0])

    def test_launch_tmux_burst_with_retries_waits_fifteen_minutes_on_budget_error(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        adapter = DummyAdapter(
            supervisor.ProviderConfig(provider="claude", model="opus", extra_args=["--effort", "max"]),
            "worker",
            config,
            {},
            ["bash", "-lc", "exit 0"],
        )
        failed_log = repo_path / "budget-failed.log"
        failed_log.write_text("usage limit reached; please retry later", encoding="utf-8")
        failed = {
            "captured_output": "usage limit reached",
            "artifact_path": repo_path / "artifact.json",
            "per_cycle_log": failed_log,
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
                8,
                "prompt",
                stage_label="worker burst",
            )

        self.assertEqual(run, succeeded)
        self.assertEqual(launch_mock.call_count, 2)
        sleep_mock.assert_called_once_with(supervisor.BUDGET_ERROR_RETRY_DELAY_SECONDS)

    def test_burst_hit_budget_error_detects_claude_limit_message(self) -> None:
        repo_path = self.make_repo()
        log_path = repo_path / "claude-limit.log"
        log_path.write_text("You've hit your limit. Please wait until 3am to continue.", encoding="utf-8")

        self.assertTrue(
            supervisor.burst_hit_budget_error(
                {
                    "captured_output": "",
                    "per_cycle_log": log_path,
                    "exit_code": 1,
                }
            )
        )

    def test_burst_hit_productive_local_failure_detects_lean_build_errors(self) -> None:
        repo_path = self.make_repo()
        log_path = repo_path / "lean-failed.log"
        log_path.write_text(
            "Building Twobites.IndependentSets\n"
            "error: Twobites/IndependentSets.lean:494:6: Type mismatch\n",
            encoding="utf-8",
        )

        self.assertTrue(
            supervisor.burst_hit_productive_local_failure(
                {
                    "captured_output": "",
                    "per_cycle_log": log_path,
                    "exit_code": 1,
                }
            )
        )

    def test_productive_local_failure_does_not_count_as_budget_error(self) -> None:
        repo_path = self.make_repo()
        log_path = repo_path / "lean-failed.log"
        log_path.write_text(
            "Building Twobites.IndependentSets\n"
            "error: Twobites/IndependentSets.lean:494:6: Type mismatch\n",
            encoding="utf-8",
        )

        self.assertFalse(
            supervisor.burst_hit_budget_error(
                {
                    "captured_output": "",
                    "per_cycle_log": log_path,
                    "exit_code": 1,
                }
            )
        )

    def test_launch_tmux_burst_with_retries_caps_delay_for_productive_local_failure(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        adapter = DummyAdapter(
            supervisor.ProviderConfig(provider="codex", model="gpt-5.4", extra_args=[]),
            "worker",
            config,
            {},
            ["bash", "-lc", "exit 0"],
        )
        failed_log = repo_path / "lean-failed.log"
        failed_log.write_text(
            "Building Twobites.IndependentSets\n"
            "error: Twobites/IndependentSets.lean:494:6: Type mismatch\n",
            encoding="utf-8",
        )
        failed = {
            "captured_output": "stream disconnected after local build failure",
            "artifact_path": repo_path / "artifact.json",
            "per_cycle_log": failed_log,
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
        policy = supervisor.Policy(
            stuck_recovery=supervisor.StuckRecoveryPolicy(),
            branching=supervisor.BranchingPolicy(
                evaluation_cycle_budget=config.branching.evaluation_cycle_budget,
                poll_seconds=config.branching.poll_seconds,
                proposal_cooldown_reviews=5,
                replacement_min_confidence=0.8,
            ),
            timing=supervisor.TimingPolicy(
                sleep_seconds=config.sleep_seconds,
                agent_retry_delays_seconds=(3600.0, 7200.0),
            ),
            codex_budget_pause=supervisor.CodexBudgetPausePolicy(),
            prompt_notes=supervisor.PromptNotesPolicy(),
        )

        with (
            mock.patch.object(supervisor, "launch_tmux_burst", side_effect=[failed, succeeded]) as launch_mock,
            mock.patch.object(supervisor.time, "sleep") as sleep_mock,
        ):
            run = supervisor.launch_tmux_burst_with_retries(
                adapter,
                12,
                "prompt",
                stage_label="worker burst",
                policy=policy,
            )

        self.assertEqual(run, succeeded)
        self.assertEqual(launch_mock.call_count, 2)
        sleep_mock.assert_called_once_with(supervisor.PRODUCTIVE_LOCAL_FAILURE_MAX_RETRY_DELAY_SECONDS)

    def test_budget_error_does_not_consume_normal_retry_budget(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        adapter = DummyAdapter(
            supervisor.ProviderConfig(provider="claude", model="opus", extra_args=["--effort", "max"]),
            "reviewer",
            config,
            {},
            ["bash", "-lc", "exit 0"],
        )
        policy = supervisor.Policy(
            stuck_recovery=supervisor.StuckRecoveryPolicy(),
            branching=supervisor.BranchingPolicy(
                evaluation_cycle_budget=config.branching.evaluation_cycle_budget,
                poll_seconds=config.branching.poll_seconds,
                proposal_cooldown_reviews=5,
                replacement_min_confidence=0.8,
            ),
            timing=supervisor.TimingPolicy(
                sleep_seconds=config.sleep_seconds,
                agent_retry_delays_seconds=(5.0, 7.0),
            ),
            codex_budget_pause=supervisor.CodexBudgetPausePolicy(),
            prompt_notes=supervisor.PromptNotesPolicy(),
        )
        budget_log = repo_path / "budget-failed.log"
        budget_log.write_text("quota exceeded", encoding="utf-8")
        budget_failed = {
            "captured_output": "quota exceeded",
            "artifact_path": repo_path / "artifact.json",
            "per_cycle_log": budget_log,
            "exit_code": 1,
            "pane_id": "%1",
            "window_id": "@1",
        }
        non_budget_failed = {
            "captured_output": "plain parse failure",
            "artifact_path": repo_path / "artifact.json",
            "per_cycle_log": repo_path / "failed.log",
            "exit_code": 1,
            "pane_id": "%2",
            "window_id": "@2",
        }
        succeeded = {
            "captured_output": "ok",
            "artifact_path": repo_path / "artifact.json",
            "per_cycle_log": repo_path / "success.log",
            "exit_code": 0,
            "pane_id": "%3",
            "window_id": "@3",
        }

        with (
            mock.patch.object(supervisor, "launch_tmux_burst", side_effect=[budget_failed, non_budget_failed, succeeded]) as launch_mock,
            mock.patch.object(supervisor.time, "sleep") as sleep_mock,
        ):
            run = supervisor.launch_tmux_burst_with_retries(
                adapter,
                9,
                "prompt",
                stage_label="reviewer burst",
                policy=policy,
            )

        self.assertEqual(run, succeeded)
        self.assertEqual(launch_mock.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [supervisor.BUDGET_ERROR_RETRY_DELAY_SECONDS, 5.0],
        )

    def test_clear_supervisor_artifacts_removes_primary_and_auxiliary_files(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        primary = config.state_dir / "worker_handoff.json"
        frontier = supervisor.theorem_frontier_worker_update_path(config)
        legacy = config.repo_path / "supervisor" / "worker_handoff.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        for path in (primary, frontier, legacy):
            path.write_text("{}", encoding="utf-8")

        supervisor.clear_supervisor_artifacts(config, primary, frontier)

        self.assertFalse(primary.exists())
        self.assertFalse(frontier.exists())
        self.assertFalse(legacy.exists())


class WorkflowTests(SupervisorTestCase):
    def _run_main_with_mocked_bursts(
        self,
        config: supervisor.Config,
        state: dict,
        *,
        artifacts: list[dict],
        validations: list[dict],
        restore_cleanup_last_good_commit: mock.Mock | None = None,
    ) -> tuple[int, list[dict], mock.Mock, mock.Mock]:
        launches: list[dict] = []

        def fake_make_adapter(role: str, cfg: supervisor.Config, current_state: dict) -> DummyAdapter:
            provider_cfg = cfg.worker if role == "worker" else cfg.reviewer
            return DummyAdapter(provider_cfg, role, cfg, current_state, ["bash", "-lc", "exit 0"])

        def fake_launch(*args, **kwargs):
            launches.append(
                {
                    "cycle": args[1],
                    "prompt": args[2],
                    "phase": kwargs.get("phase"),
                    "stage_label": kwargs.get("stage_label"),
                    "reuse_existing_window": kwargs.get("reuse_existing_window"),
                }
            )
            return {
                "captured_output": "",
                "artifact_path": f"/tmp/fake-artifact-{len(launches)}.json",
            }

        record_chat_event_mock = mock.Mock()
        save_state_mock = mock.Mock()
        restore_mock = restore_cleanup_last_good_commit or mock.Mock()

        with (
            mock.patch.object(sys, "argv", ["supervisor.py", "--config", str(config.repo_path.parent / "config.json")]),
            mock.patch.object(supervisor, "load_config", return_value=config),
            mock.patch.object(supervisor, "load_state", return_value=state),
            mock.patch.object(supervisor, "check_dependencies"),
            mock.patch.object(supervisor, "ensure_git_repository"),
            mock.patch.object(supervisor, "install_personal_provider_context_files", return_value=[]),
            mock.patch.object(supervisor, "ensure_repo_files"),
            mock.patch.object(supervisor, "ensure_chat_site"),
            mock.patch.object(supervisor, "ensure_tmux_session"),
            mock.patch.object(supervisor, "maybe_consume_human_input", return_value=True),
            mock.patch.object(supervisor, "recover_interrupted_worker_state", return_value=False),
            mock.patch.object(supervisor, "make_adapter", side_effect=fake_make_adapter),
            mock.patch.object(supervisor, "launch_tmux_burst_with_retries", side_effect=fake_launch),
            mock.patch.object(supervisor, "load_json_artifact_with_fallback", side_effect=artifacts),
            mock.patch.object(supervisor, "run_validation", side_effect=validations),
            mock.patch.object(supervisor, "restore_cleanup_last_good_commit", restore_mock),
            mock.patch.object(supervisor, "record_chat_event", record_chat_event_mock),
            mock.patch.object(supervisor, "append_jsonl"),
            mock.patch.object(supervisor, "save_state", save_state_mock),
            mock.patch.object(supervisor.time, "sleep"),
        ):
            result = supervisor.main()

        return result, launches, record_chat_event_mock, save_state_mock

    def test_main_advances_from_proof_formalization_into_cleanup_phase(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase="proof_formalization")
        state = {
            "phase": "proof_formalization",
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
        }
        result, launches, record_chat_event_mock, _ = self._run_main_with_mocked_bursts(
            config,
            state,
            artifacts=[
                {
                    "phase": "proof_formalization",
                    "status": "DONE",
                    "summary_of_changes": "proof complete",
                    "current_frontier": "none",
                    "likely_next_step": "cleanup",
                    "input_request": "",
                },
                {
                    "phase": "proof_formalization",
                    "decision": "ADVANCE_PHASE",
                    "confidence": 0.95,
                    "reason": "Proof complete; move to cleanup.",
                    "next_prompt": "",
                },
                {
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "status": "STUCK",
                    "summary_of_changes": "No worthwhile cleanup found.",
                    "current_frontier": "cleanup done",
                    "likely_next_step": "stop",
                    "input_request": "",
                },
            ],
            validations=[
                {
                    "cycle": 1,
                    "phase": "proof_formalization",
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "proof-head"},
                },
                {
                    "cycle": 2,
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "proof-head"},
                },
            ],
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            [entry["stage_label"] for entry in launches],
            ["worker burst", "reviewer burst", "worker burst"],
        )
        self.assertEqual(launches[0]["phase"], "proof_formalization")
        self.assertEqual(launches[1]["phase"], "proof_formalization")
        self.assertEqual(launches[2]["phase"], supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP)
        self.assertIn("PROOF COMPLETE - style cleanup", launches[2]["prompt"])
        self.assertEqual(state["phase"], supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP)
        self.assertEqual(state["cleanup_last_good_commit"], "proof-head")
        transition_events = [
            call.kwargs for call in record_chat_event_mock.mock_calls if call.kwargs.get("kind") == "phase_transition"
        ]
        self.assertEqual(len(transition_events), 1)
        self.assertEqual(transition_events[0]["phase"], supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP)

    def test_main_advances_from_theorem_stating_and_seeds_initial_theorem_frontier(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(
            repo_path,
            start_phase="theorem_stating",
            theorem_frontier_phase="full",
        )
        config.max_cycles = 1
        config.state_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "phase": "theorem_stating",
            "nodes": [
                {
                    "node_id": "paper.main",
                    "kind": "paper",
                    "natural_language_statement": "The paper main theorem holds.",
                    "lean_statement": "def paperMainStatement : Prop := True",
                    "lean_anchor": "PaperTheorems.paperMainStatement",
                    "paper_provenance": "Paper Theorem `main`.",
                    "closure_mode": "all_children",
                    "blocker_cluster": "paper main result",
                    "acceptance_evidence": "Close this paper-facing result or approved descendants that prove it.",
                    "notes": "Primary graph theorem.",
                },
                {
                    "node_id": "paper.main2",
                    "kind": "paper",
                    "natural_language_statement": "The paper Ramsey corollary holds.",
                    "lean_statement": "def paperMain2Statement : Prop := True",
                    "lean_anchor": "PaperTheorems.paperMain2Statement",
                    "paper_provenance": "Paper Theorem `main2`.",
                    "closure_mode": "all_children",
                    "blocker_cluster": "paper ramsey corollary",
                    "acceptance_evidence": "Close this paper-facing result or approved descendants that prove it.",
                    "notes": "Ramsey-number corollary.",
                },
            ],
            "edges": [
                {
                    "parent": "paper.main2",
                    "child": "paper.main",
                    "edge_type": "reduction",
                    "justification": "The Ramsey corollary reduces to the main graph theorem.",
                    "natural_language_proof": "The paper derives the Ramsey corollary from the main theorem plus the witness-to-Ramsey conversion.",
                },
                {
                    "parent": "paper.main",
                    "child": "paper.main",
                    "edge_type": "direct_proof",
                    "justification": "The main theorem itself is the first coarse obligation to attack.",
                    "natural_language_proof": "The remaining proof of the main theorem is deferred to later proof-formalization steps starting from this explicit direct obligation.",
                }
            ],
            "initial_active_edge_id": supervisor.theorem_frontier_edge_id("paper.main", "direct_proof", "paper.main"),
        }
        supervisor.paper_main_results_manifest_path(config).write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        state = {
            "phase": "theorem_stating",
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
        }

        result, launches, record_chat_event_mock, _ = self._run_main_with_mocked_bursts(
            config,
            state,
            artifacts=[
                {
                    "phase": "theorem_stating",
                    "status": "DONE",
                    "summary_of_changes": "statement files and manifest are ready",
                    "current_frontier": "main results stated",
                    "likely_next_step": "proof formalization",
                    "input_request": "",
                },
                {
                    "phase": "theorem_stating",
                    "decision": "ADVANCE_PHASE",
                    "confidence": 0.95,
                    "reason": "Statements are ready for proof formalization.",
                    "next_prompt": "",
                },
            ],
            validations=[
                {
                    "cycle": 1,
                    "phase": "theorem_stating",
                    "build": {"ok": True},
                    "syntax_checks": [{"ok": True}, {"ok": True}],
                    "sorry_policy": {"disallowed_entries": []},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "statement-head"},
                },
            ],
        )

        self.assertEqual(result, 0)
        self.assertEqual([entry["stage_label"] for entry in launches], ["worker burst", "reviewer burst"])
        self.assertEqual(state["phase"], "proof_formalization")
        self.assertEqual(state["theorem_frontier"]["active_edge_id"], supervisor.theorem_frontier_edge_id("paper.main", "direct_proof", "paper.main"))
        self.assertEqual(state["theorem_frontier"]["active_leaf_id"], "paper.main")
        self.assertEqual(sorted(state["theorem_frontier"]["nodes"].keys()), ["paper.main", "paper.main2"])
        seed_events = [
            call.kwargs for call in record_chat_event_mock.mock_calls if call.kwargs.get("kind") == "theorem_frontier_seed"
        ]
        self.assertEqual(len(seed_events), 1)
        self.assertEqual(seed_events[0]["phase"], "proof_formalization")

    def test_main_theorem_stating_transition_requires_manifest_seeding_in_full_mode(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(
            repo_path,
            start_phase="theorem_stating",
            theorem_frontier_phase="full",
        )
        config.max_cycles = 1
        config.state_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "phase": "theorem_stating",
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
        }

        with self.assertRaisesRegex(supervisor.SupervisorError, "paper coarse-DAG manifest"):
            self._run_main_with_mocked_bursts(
                config,
                state,
                artifacts=[
                    {
                        "phase": "theorem_stating",
                        "status": "DONE",
                        "summary_of_changes": "statement files are ready",
                        "current_frontier": "main results stated",
                        "likely_next_step": "proof formalization",
                        "input_request": "",
                    },
                    {
                        "phase": "theorem_stating",
                        "decision": "ADVANCE_PHASE",
                        "confidence": 0.95,
                        "reason": "Statements are ready for proof formalization.",
                        "next_prompt": "",
                    },
                ],
                validations=[
                    {
                        "cycle": 1,
                        "phase": "theorem_stating",
                        "build": {"ok": True},
                        "syntax_checks": [{"ok": True}, {"ok": True}],
                        "sorry_policy": {"disallowed_entries": []},
                        "sorries": {"count": 0},
                        "axioms": {"unapproved": []},
                        "git": {"head": "statement-head"},
                    },
                ],
            )
        self.assertEqual(state["phase"], "theorem_stating")

    def test_main_records_blocked_transition_error_when_theorem_stating_cannot_advance(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase="theorem_stating", theorem_frontier_phase="off")
        config.max_cycles = 1
        config.state_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "phase": "theorem_stating",
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
        }

        with self.assertRaisesRegex(supervisor.SupervisorError, "disallowed sorrys"):
            self._run_main_with_mocked_bursts(
                config,
                state,
                artifacts=[
                    {
                        "phase": "theorem_stating",
                        "status": "DONE",
                        "summary_of_changes": "statement files are ready",
                        "current_frontier": "main results stated",
                        "likely_next_step": "proof formalization",
                        "input_request": "",
                    },
                    {
                        "phase": "theorem_stating",
                        "decision": "ADVANCE_PHASE",
                        "confidence": 0.9,
                        "reason": "advance",
                        "next_prompt": "",
                    },
                ],
                validations=[
                    {
                        "cycle": 1,
                        "phase": "theorem_stating",
                        "build": {"ok": True},
                        "syntax_checks": [{"ok": True}, {"ok": True}],
                        "sorry_policy": {"disallowed_entries": [{"path": "repo/PaperTheorems.lean", "line": 1}]},
                        "sorries": {"count": 1},
                        "axioms": {"unapproved": []},
                        "git": {"head": "statement-head"},
                    },
                ],
            )

        self.assertEqual(state["phase"], "theorem_stating")
        self.assertEqual(state["last_transition_error"]["decision"], "ADVANCE_PHASE")
        self.assertIn("disallowed sorrys", state["last_transition_error"]["error"])

    def test_main_full_mode_rejects_invalid_worker_frontier_artifact(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(
            repo_path,
            start_phase="proof_formalization",
            theorem_frontier_phase="full",
        )
        config.state_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "phase": "proof_formalization",
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        with self.assertRaisesRegex(supervisor.SupervisorError, "Theorem-frontier worker update phase mismatch"):
            self._run_main_with_mocked_bursts(
                config,
                state,
                artifacts=[
                    {
                        "phase": "proof_formalization",
                        "status": "NOT_STUCK",
                        "summary_of_changes": "Stayed on the active theorem.",
                        "current_frontier": "graph-pair RI collapse",
                        "likely_next_step": "continue",
                        "input_request": "",
                    },
                    {
                        "phase": "planning",
                        "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                        "active_node_id": "ri.local.graph_pair",
                        "active_node": {
                            "node_id": "ri.local.graph_pair",
                            "kind": "support",
                            "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                            "lean_statement": "theorem graph_pair_bad_event_bound : True := by trivial",
                            "lean_anchor": "Twobites.IndependentSets.graph_pair_bad_event_bound",
                            "paper_provenance": "Paper Lemma RISI local graph-pair bound.",
                            "closure_mode": "all_children",
                            "blocker_cluster": "graph-pair local RI collapse",
                            "acceptance_evidence": "Close this theorem or approved descendants.",
                            "notes": "Primary local blocker.",
                        },
                        "requested_action": "CLOSE",
                        "cone_scope": "Only local graph-pair lemmas.",
                        "allowed_edit_paths": ["Twobites/IndependentSets.lean"],
                        "result_summary": "Stayed on the active theorem.",
                        "proposed_nodes": [],
                        "proposed_edges": [],
                        "next_candidate_edge_ids": ["ri.local.graph_pair|direct_proof|ri.local.graph_pair"],
                        "structural_change_reason": "",
                    },
                ],
                validations=[
                    {
                        "cycle": 1,
                        "phase": "proof_formalization",
                        "build": {"ok": True},
                        "syntax_checks": [{"ok": True}, {"ok": True}],
                        "sorry_policy": {"disallowed_entries": []},
                        "sorries": {"count": 0},
                        "axioms": {"unapproved": []},
                        "git": {"head": "proof-head"},
                    },
                ],
            )

    def test_main_full_mode_rejects_invalid_frontier_review_artifact(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(
            repo_path,
            start_phase="proof_formalization",
            theorem_frontier_phase="full",
        )
        config.state_dir.mkdir(parents=True, exist_ok=True)
        active_node = supervisor.theorem_frontier_node_record(
            {
                "node_id": "ri.local.graph_pair",
                "kind": "support",
                "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                "lean_statement": "theorem graph_pair_bad_event_bound : True := by trivial",
                "lean_anchor": "Twobites.IndependentSets.graph_pair_bad_event_bound",
                "paper_provenance": "Paper Lemma RISI local graph-pair bound.",
                "closure_mode": "all_children",
                "blocker_cluster": "graph-pair local RI collapse",
                "acceptance_evidence": "Close this theorem or approved descendants.",
                "notes": "Primary local blocker.",
            },
            status="active",
            parent_ids=[],
            child_ids=[],
        )
        state = {
            "phase": "proof_formalization",
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
            "theorem_frontier": {
                **supervisor.default_theorem_frontier_payload("full"),
                "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                "active_leaf_id": "ri.local.graph_pair",
                "nodes": {"ri.local.graph_pair": active_node},
            },
        }

        with self.assertRaisesRegex(supervisor.SupervisorError, "Theorem-frontier review phase mismatch"):
            self._run_main_with_mocked_bursts(
                config,
                state,
                artifacts=[
                    {
                        "phase": "proof_formalization",
                        "status": "NOT_STUCK",
                        "summary_of_changes": "Stayed on the active theorem.",
                        "current_frontier": "graph-pair RI collapse",
                        "likely_next_step": "continue",
                        "input_request": "",
                    },
                    {
                        "phase": "proof_formalization",
                        "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                        "active_node_id": "ri.local.graph_pair",
                        "active_node": {
                            "node_id": "ri.local.graph_pair",
                            "kind": "support",
                            "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                            "lean_statement": "theorem graph_pair_bad_event_bound : True := by trivial",
                            "lean_anchor": "Twobites.IndependentSets.graph_pair_bad_event_bound",
                            "paper_provenance": "Paper Lemma RISI local graph-pair bound.",
                            "closure_mode": "all_children",
                            "blocker_cluster": "graph-pair local RI collapse",
                            "acceptance_evidence": "Close this theorem or approved descendants.",
                            "notes": "Primary local blocker.",
                        },
                        "requested_action": "CLOSE",
                        "cone_scope": "Only local graph-pair lemmas.",
                        "allowed_edit_paths": ["Twobites/IndependentSets.lean"],
                        "result_summary": "Stayed on the active theorem.",
                        "proposed_nodes": [],
                        "proposed_edges": [],
                        "next_candidate_edge_ids": ["ri.local.graph_pair|direct_proof|ri.local.graph_pair"],
                        "structural_change_reason": "",
                    },
                    {
                        "phase": "proof_formalization",
                        "decision": "CONTINUE",
                        "confidence": 0.8,
                        "reason": "Keep working the same local theorem.",
                        "next_prompt": "Continue.",
                    },
                    {
                        "phase": "planning",
                        "active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                        "active_theorem_id": "ri.local.graph_pair",
                        "active_theorem_nl_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
                        "active_theorem_lean_statement": "theorem graph_pair_bad_event_bound : True := by trivial",
                        "active_theorem_anchor": "Twobites.IndependentSets.graph_pair_bad_event_bound",
                        "assessed_action": "CLOSE",
                        "blocker_cluster": "graph-pair local RI collapse",
                        "outcome": "STILL_OPEN",
                        "next_active_edge_id": "ri.local.graph_pair|direct_proof|ri.local.graph_pair",
                        "cone_purity": "HIGH",
                        "open_hypotheses": ["Close the local graph-pair theorem."],
                        "justification": "Still the main blocker.",
                    },
                ],
                validations=[
                    {
                        "cycle": 1,
                        "phase": "proof_formalization",
                        "build": {"ok": True},
                        "syntax_checks": [{"ok": True}, {"ok": True}],
                        "sorry_policy": {"disallowed_entries": []},
                        "sorries": {"count": 0},
                        "axioms": {"unapproved": []},
                        "git": {"head": "proof-head"},
                    },
                ],
            )

    def test_main_phase0_records_theorem_frontier_artifacts_and_state(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(
            repo_path,
            start_phase="proof_formalization",
            theorem_frontier_phase="phase0",
        )
        config.state_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "phase": "proof_formalization",
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
        }
        worker_frontier = {
            "phase": "proof_formalization",
            "active_theorem_id": "ri.local.graph_pair",
            "active_theorem_nl_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
            "active_theorem_lean_statement": "theorem graph_pair_bad_event_bound : True := by trivial",
            "active_theorem_anchor": "Twobites.IndependentSets.graph_pair_bad_event_bound",
            "requested_action": "CLOSE",
            "blocker_cluster": "graph-pair local RI collapse",
            "cone_scope": "Only local graph-pair support lemmas are allowed.",
            "result_summary": "The burst stayed on the graph-pair theorem.",
        }
        reviewer_frontier = {
            "phase": "proof_formalization",
            "active_theorem_id": "ri.local.graph_pair",
            "active_theorem_nl_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
            "active_theorem_lean_statement": "theorem graph_pair_bad_event_bound : True := by trivial",
            "active_theorem_anchor": "Twobites.IndependentSets.graph_pair_bad_event_bound",
            "assessed_action": "CLOSE",
            "blocker_cluster": "graph-pair local RI collapse",
            "outcome": "STILL_OPEN",
            "justification": "The active theorem remains the central blocker.",
        }

        result, launches, record_chat_event_mock, _ = self._run_main_with_mocked_bursts(
            config,
            state,
            artifacts=[
                {
                    "phase": "proof_formalization",
                    "status": "DONE",
                    "summary_of_changes": "proof complete",
                    "current_frontier": "none",
                    "likely_next_step": "cleanup",
                    "input_request": "",
                },
                worker_frontier,
                {
                    "phase": "proof_formalization",
                    "decision": "ADVANCE_PHASE",
                    "confidence": 0.95,
                    "reason": "Proof complete; move to cleanup.",
                    "next_prompt": "",
                },
                reviewer_frontier,
                {
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "status": "STUCK",
                    "summary_of_changes": "No worthwhile cleanup found.",
                    "current_frontier": "cleanup done",
                    "likely_next_step": "stop",
                    "input_request": "",
                },
            ],
            validations=[
                {
                    "cycle": 1,
                    "phase": "proof_formalization",
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "proof-head"},
                },
                {
                    "cycle": 2,
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "proof-head"},
                },
            ],
        )

        self.assertEqual(result, 0)
        self.assertIn("Theorem-frontier artifact requirements (Phase 0)", launches[0]["prompt"])
        self.assertIn("Theorem-frontier review requirements (Phase 0)", launches[1]["prompt"])
        self.assertNotIn("Theorem-frontier artifact requirements (Phase 0)", launches[2]["prompt"])
        self.assertEqual(state["last_theorem_frontier_worker_update"]["active_theorem_id"], "ri.local.graph_pair")
        self.assertEqual(state["theorem_frontier"]["active_theorem_id"], "ri.local.graph_pair")
        self.assertEqual(state["theorem_frontier"]["active_theorem_age"], 1)
        saved_frontier = json.loads(supervisor.theorem_frontier_state_path(config).read_text(encoding="utf-8"))
        self.assertEqual(saved_frontier["current"]["active_theorem_id"], "ri.local.graph_pair")
        kinds = [call.kwargs.get("kind") for call in record_chat_event_mock.mock_calls]
        self.assertIn("theorem_frontier_update", kinds)
        self.assertIn("theorem_frontier_review", kinds)

    def test_main_full_theorem_frontier_runs_paper_verifier_and_updates_dag(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(
            repo_path,
            start_phase="proof_formalization",
            theorem_frontier_phase="full",
        )
        config.state_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "phase": "proof_formalization",
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
        }
        worker_frontier = {
            "phase": "proof_formalization",
            "active_edge_id": supervisor.theorem_frontier_edge_id("paper.main", "direct_proof", "paper.main"),
            "active_node_id": "paper.main",
            "active_node": {
                "node_id": "paper.main",
                "kind": "paper",
                "natural_language_statement": "The paper main theorem witness exists.",
                "natural_language_proof": "This paper-facing theorem is discharged directly by the witness theorem established in the same cycle.",
                "lean_statement": "theorem paper_main : True := by trivial",
                "lean_anchor": "PaperTheorems.paper_main",
                "paper_provenance": "Main theorem in the paper.",
                "closure_mode": "leaf",
                "blocker_cluster": "final witness supply",
                "acceptance_evidence": "Close the public paper theorem wrapper.",
                "notes": "Final paper-facing theorem shell.",
            },
            "requested_action": "CLOSE",
            "cone_scope": "Only the paper main theorem shell and its direct witness lemma.",
            "allowed_edit_paths": ["PaperTheorems.lean"],
            "result_summary": "Closed the paper theorem shell.",
            "proposed_nodes": [],
            "proposed_edges": [
                {
                    "parent": "paper.main",
                    "child": "paper.main",
                    "edge_type": "direct_proof",
                    "justification": "At this coarse level, the public paper theorem is attacked directly.",
                    "natural_language_proof": "The paper-facing theorem is introduced as a direct coarse obligation to be closed immediately in this cycle.",
                }
            ],
            "next_candidate_edge_ids": [supervisor.theorem_frontier_edge_id("paper.main", "direct_proof", "paper.main")],
            "structural_change_reason": "Create the initial direct-proof obligation edge for the public paper theorem.",
        }
        paper_review = {
            "phase": "proof_formalization",
            "parent_node_id": "paper.main",
            "change_kind": "CREATE_ACTIVE",
            "decision": "APPROVE",
            "classification": "paper_exact",
            "approved_node_ids": ["paper.main"],
            "approved_edges": [{"parent": "paper.main", "child": "paper.main", "edge_type": "direct_proof"}],
            "justification": "This is the paper theorem itself.",
            "caveat": "",
        }
        nl_proof_review = {
            "phase": "proof_formalization",
            "parent_node_id": "paper.main",
            "change_kind": "CREATE_ACTIVE",
            "decision": "APPROVE",
            "approved_node_ids": ["paper.main"],
            "approved_edges": [{"parent": "paper.main", "child": "paper.main", "edge_type": "direct_proof"}],
            "justification": "The leaf theorem's natural-language proof is complete and rigorous.",
            "caveat": "",
        }
        reviewer_frontier = {
            "phase": "proof_formalization",
            "active_edge_id": supervisor.theorem_frontier_edge_id("paper.main", "direct_proof", "paper.main"),
            "active_theorem_id": "paper.main",
            "active_theorem_nl_statement": "The paper main theorem witness exists.",
            "active_theorem_lean_statement": "theorem paper_main : True := by trivial",
            "active_theorem_anchor": "PaperTheorems.paper_main",
            "assessed_action": "CLOSE",
            "blocker_cluster": "final witness supply",
            "outcome": "CLOSED",
            "next_active_edge_id": "",
            "cone_purity": "HIGH",
            "open_hypotheses": [],
            "justification": "The active paper theorem closed cleanly.",
        }

        result, launches, record_chat_event_mock, _ = self._run_main_with_mocked_bursts(
            config,
            state,
            artifacts=[
                {
                    "phase": "proof_formalization",
                    "status": "DONE",
                    "summary_of_changes": "proof complete",
                    "current_frontier": "none",
                    "likely_next_step": "cleanup",
                    "input_request": "",
                },
                worker_frontier,
                paper_review,
                nl_proof_review,
                {
                    "phase": "proof_formalization",
                    "decision": "ADVANCE_PHASE",
                    "confidence": 0.95,
                    "reason": "Proof complete; move to cleanup.",
                    "next_prompt": "",
                },
                reviewer_frontier,
                {
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "status": "STUCK",
                    "summary_of_changes": "No worthwhile cleanup found.",
                    "current_frontier": "cleanup done",
                    "likely_next_step": "stop",
                    "input_request": "",
                },
            ],
            validations=[
                {
                    "cycle": 1,
                    "phase": "proof_formalization",
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "proof-head"},
                },
                {
                    "cycle": 2,
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "proof-head"},
                },
            ],
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            [entry["stage_label"] for entry in launches],
            ["worker burst", "paper-verifier burst", "NL-proof-verifier burst", "reviewer burst", "worker burst"],
        )
        self.assertIn("paper-verifier", launches[1]["prompt"])
        self.assertIn("NL-proof verifier", launches[2]["prompt"])
        self.assertEqual(state["theorem_frontier"]["active_leaf_id"], None)
        self.assertEqual(state["theorem_frontier"]["nodes"]["paper.main"]["status"], "closed")
        self.assertEqual(state["theorem_frontier"]["metrics"]["paper_nodes_closed"], 1)
        kinds = [call.kwargs.get("kind") for call in record_chat_event_mock.mock_calls]
        self.assertIn("theorem_frontier_paper_verifier_review", kinds)
        self.assertIn("theorem_frontier_nl_proof_verifier_review", kinds)
        self.assertIn("theorem_frontier_review", kinds)

    def test_main_cleanup_invalid_worker_cycle_restores_and_stops_before_reviewer(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase=supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP)
        state = {
            "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
            "cleanup_last_good_commit": "good-head",
        }
        restore_mock = mock.Mock()

        result, launches, _, _ = self._run_main_with_mocked_bursts(
            config,
            state,
            artifacts=[
                {
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "status": "NOT_STUCK",
                    "summary_of_changes": "cleanup edit",
                    "current_frontier": "warnings",
                    "likely_next_step": "finish cleanup",
                    "input_request": "",
                }
            ],
            validations=[
                {
                    "cycle": 1,
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "build": {"ok": False},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "bad-head"},
                }
            ],
            restore_cleanup_last_good_commit=restore_mock,
        )

        self.assertEqual(result, 0)
        self.assertEqual([entry["stage_label"] for entry in launches], ["worker burst"])
        restore_mock.assert_called_once()
        self.assertEqual(restore_mock.call_args.kwargs["cycle"], 1)
        self.assertIn("cleanup cycle ended without a fully valid proof state", restore_mock.call_args.kwargs["reason"])

    def test_main_cleanup_no_progress_stops_before_reviewer(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase=supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP)
        state = {
            "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
            "cleanup_last_good_commit": "good-head",
        }
        restore_mock = mock.Mock()

        result, launches, _, _ = self._run_main_with_mocked_bursts(
            config,
            state,
            artifacts=[
                {
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "status": "NOT_STUCK",
                    "summary_of_changes": "looked for cleanup",
                    "current_frontier": "no-op",
                    "likely_next_step": "stop",
                    "input_request": "",
                }
            ],
            validations=[
                {
                    "cycle": 1,
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "good-head"},
                }
            ],
            restore_cleanup_last_good_commit=restore_mock,
        )

        self.assertEqual(result, 0)
        self.assertEqual([entry["stage_label"] for entry in launches], ["worker burst"])
        restore_mock.assert_not_called()
        self.assertEqual(state["cleanup_last_good_commit"], "good-head")

    def test_main_cleanup_reviewer_stuck_restores_and_stops(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase=supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP)
        state = {
            "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
            "cleanup_last_good_commit": "good-head",
        }
        restore_mock = mock.Mock()

        result, launches, _, _ = self._run_main_with_mocked_bursts(
            config,
            state,
            artifacts=[
                {
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "status": "DONE",
                    "summary_of_changes": "warning cleanup commit",
                    "current_frontier": "review for more cleanup",
                    "likely_next_step": "review",
                    "input_request": "",
                },
                {
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "decision": "STUCK",
                    "confidence": 0.71,
                    "reason": "No worthwhile cleanup remains.",
                    "next_prompt": "",
                },
            ],
            validations=[
                {
                    "cycle": 1,
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "cleaner-head"},
                }
            ],
            restore_cleanup_last_good_commit=restore_mock,
        )

        self.assertEqual(result, 0)
        self.assertEqual([entry["stage_label"] for entry in launches], ["worker burst", "reviewer burst"])
        restore_mock.assert_called_once()
        self.assertIn("cleanup reviewer decided the optional cleanup phase had stalled", restore_mock.call_args.kwargs["reason"])

    def test_main_cleanup_reviewer_done_keeps_polished_commit(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase=supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP)
        state = {
            "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
            "cleanup_last_good_commit": "good-head",
        }
        restore_mock = mock.Mock()

        result, launches, _, save_state_mock = self._run_main_with_mocked_bursts(
            config,
            state,
            artifacts=[
                {
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "status": "DONE",
                    "summary_of_changes": "final warning cleanup",
                    "current_frontier": "none",
                    "likely_next_step": "stop",
                    "input_request": "",
                },
                {
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "decision": "DONE",
                    "confidence": 0.94,
                    "reason": "Cleanup complete.",
                    "next_prompt": "",
                },
            ],
            validations=[
                {
                    "cycle": 1,
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": "polished-head"},
                }
            ],
            restore_cleanup_last_good_commit=restore_mock,
        )

        self.assertEqual(result, 0)
        self.assertEqual([entry["stage_label"] for entry in launches], ["worker burst", "reviewer burst"])
        restore_mock.assert_not_called()
        self.assertEqual(state["cleanup_last_good_commit"], "polished-head")
        self.assertGreaterEqual(save_state_mock.call_count, 1)

    def test_main_cleanup_invalid_worker_cycle_real_git_restore(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase=supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP)
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True, capture_output=True, text=True)
        (repo_path / "tracked.txt").write_text("good\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo_path, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "good"], cwd=repo_path, check=True, capture_output=True, text=True)
        good_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (repo_path / "tracked.txt").write_text("bad\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "bad"], cwd=repo_path, check=True, capture_output=True, text=True)
        bad_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        state = {
            "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
            "cycle": 0,
            "roles": {},
            "review_log": [],
            "awaiting_human_input": False,
            "cleanup_last_good_commit": good_head,
        }
        launches: list[dict] = []

        def fake_make_adapter(role: str, cfg: supervisor.Config, current_state: dict) -> DummyAdapter:
            provider_cfg = cfg.worker if role == "worker" else cfg.reviewer
            return DummyAdapter(provider_cfg, role, cfg, current_state, ["bash", "-lc", "exit 0"])

        def fake_launch(*args, **kwargs):
            launches.append({"stage_label": kwargs.get("stage_label"), "phase": kwargs.get("phase")})
            return {"captured_output": "", "artifact_path": "/tmp/fake-worker.json"}

        with (
            mock.patch.object(sys, "argv", ["supervisor.py", "--config", str(config.repo_path.parent / "config.json")]),
            mock.patch.object(supervisor, "load_config", return_value=config),
            mock.patch.object(supervisor, "load_state", return_value=state),
            mock.patch.object(supervisor, "check_dependencies"),
            mock.patch.object(supervisor, "ensure_git_repository"),
            mock.patch.object(supervisor, "install_personal_provider_context_files", return_value=[]),
            mock.patch.object(supervisor, "ensure_repo_files"),
            mock.patch.object(supervisor, "ensure_chat_site"),
            mock.patch.object(supervisor, "ensure_tmux_session"),
            mock.patch.object(supervisor, "maybe_consume_human_input", return_value=True),
            mock.patch.object(supervisor, "recover_interrupted_worker_state", return_value=False),
            mock.patch.object(supervisor, "make_adapter", side_effect=fake_make_adapter),
            mock.patch.object(supervisor, "launch_tmux_burst_with_retries", side_effect=fake_launch),
            mock.patch.object(
                supervisor,
                "load_json_artifact_with_fallback",
                return_value={
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "status": "NOT_STUCK",
                    "summary_of_changes": "cleanup edit",
                    "current_frontier": "warning cleanup",
                    "likely_next_step": "finish cleanup",
                    "input_request": "",
                },
            ),
            mock.patch.object(
                supervisor,
                "run_validation",
                side_effect=[
                    {
                        "cycle": 1,
                        "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                        "build": {"ok": False},
                        "sorries": {"count": 0},
                        "axioms": {"unapproved": []},
                        "git": {"head": bad_head},
                    },
                    {
                        "cycle": 1,
                        "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                        "build": {"ok": True},
                        "sorries": {"count": 0},
                        "axioms": {"unapproved": []},
                        "git": {"head": good_head},
                    },
                ],
            ),
            mock.patch.object(supervisor, "record_chat_event"),
            mock.patch.object(supervisor, "append_jsonl"),
            mock.patch.object(supervisor, "save_state"),
            mock.patch.object(supervisor.time, "sleep"),
        ):
            result = supervisor.main()

        restored_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(result, 0)
        self.assertEqual(restored_head, good_head)
        self.assertEqual(launches, [{"stage_label": "worker burst", "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP}])

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

    def test_ensure_repo_files_writes_paper_main_results_manifest_stub_in_full_theorem_stating(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase="theorem_stating", theorem_frontier_phase="full")

        supervisor.ensure_repo_files(config, "theorem_stating")

        manifest = json.loads(supervisor.paper_main_results_manifest_path(config).read_text(encoding="utf-8"))
        self.assertEqual(manifest["phase"], "theorem_stating")
        self.assertEqual(manifest["initial_active_edge_id"], "paper.main|all_of|paper.main_aux")
        self.assertEqual(len(manifest["nodes"]), 2)
        self.assertEqual(manifest["nodes"][0]["node_id"], "paper.main")
        self.assertEqual(len(manifest["edges"]), 2)
        self.assertEqual(manifest["edges"][1]["edge_type"], "direct_proof")

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

    def test_validation_sorry_policy_allows_module_layout_paper_theorems_file(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase="theorem_stating")
        (repo_path / "PaperDefinitions.lean").write_text("def rootDef : Nat := 0\n", encoding="utf-8")
        (repo_path / "PaperTheorems.lean").write_text("import Repo.PaperTheorems\n", encoding="utf-8")
        (repo_path / "Repo").mkdir(exist_ok=True)
        (repo_path / "Repo" / "PaperTheorems.lean").write_text(
            "theorem stated : True := by\n  sorry\n",
            encoding="utf-8",
        )

        summary = supervisor.run_validation(config, "theorem_stating", 1)

        allowed = summary["sorry_policy"]["allowed_files"]
        self.assertIn("repo/PaperTheorems.lean", allowed)
        self.assertIn("repo/Repo/PaperTheorems.lean", allowed)
        self.assertEqual(summary["sorry_policy"]["disallowed_entries"], [])

    def test_run_validation_flags_theorem_stating_edits_outside_statement_file_cone(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(
            repo_path,
            start_phase="theorem_stating",
            theorem_frontier_phase="off",
            git_remote_url="git@example.com:test/repo.git",
        )
        self.git(repo_path, "init", "-b", "main")
        self.git(repo_path, "config", "user.name", "Test User")
        self.git(repo_path, "config", "user.email", "test@example.com")
        (repo_path / "PaperDefinitions.lean").write_text("def paperDef : Nat := 0\n", encoding="utf-8")
        (repo_path / "PaperTheorems.lean").write_text("theorem paperStmt : True := by\n  trivial\n", encoding="utf-8")
        (repo_path / "Support.lean").write_text("def helper : Nat := 0\n", encoding="utf-8")
        self.git(repo_path, "add", ".")
        self.git(repo_path, "commit", "-m", "init theorem stating files")
        previous_head = self.git(repo_path, "rev-parse", "HEAD").stdout.strip()

        (repo_path / "Support.lean").write_text("def helper : Nat := 1\n", encoding="utf-8")

        summary = supervisor.run_validation(
            config,
            "theorem_stating",
            2,
            previous_validation={"git": {"head": previous_head}},
        )

        self.assertIn("Support.lean", summary["theorem_stating_edit_policy"]["disallowed_changed_lean_files"])

    def test_cleanup_phase_shares_proof_sorry_policy(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, start_phase=supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP)
        (repo_path / "lakefile.toml").write_text(
            'name = "T"\nversion = "0.1.0"\ndefaultTargets = ["t"]\n\n[[lean_lib]]\nname = "T"\n',
            encoding="utf-8",
        )
        (repo_path / "lean-toolchain").write_text("leanprover/lean4:v4.28.0\n", encoding="utf-8")
        (repo_path / "T.lean").write_text("def t : Nat := 0\n", encoding="utf-8")
        (repo_path / "PaperDefinitions.lean").write_text("def foo : Nat := 0\n", encoding="utf-8")
        (repo_path / "PaperTheorems.lean").write_text("theorem stated : True := by\n  sorry\n", encoding="utf-8")

        supervisor.ensure_repo_files(config, supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP)
        summary = supervisor.run_validation(config, supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP, 1)

        self.assertFalse(summary["policy_ok"])
        self.assertEqual(summary["sorry_policy"]["allowed_files"], ["repo/PaperTheorems.lean"])

    def test_run_validation_reports_changed_lean_files_since_previous_validation(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        self.git(repo_path, "init", "-b", "main")
        self.git(repo_path, "config", "user.name", "Test User")
        self.git(repo_path, "config", "user.email", "test@example.com")
        (repo_path / "A.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")
        self.git(repo_path, "add", ".")
        self.git(repo_path, "commit", "-m", "init")
        previous_head = self.git(repo_path, "rev-parse", "HEAD").stdout.strip()

        (repo_path / "A.lean").write_text("theorem a : True := by trivial\n\ntheorem a2 : True := by trivial\n", encoding="utf-8")
        (repo_path / "B.lean").write_text("theorem b : True := by trivial\n", encoding="utf-8")
        self.git(repo_path, "add", ".")
        self.git(repo_path, "commit", "-m", "lean changes")

        summary = supervisor.run_validation(
            config,
            "proof_formalization",
            2,
            previous_validation={"git": {"head": previous_head}},
        )

        self.assertEqual(summary["git"]["previous_validation_head"], previous_head)
        self.assertEqual(summary["git"]["changed_lean_files"], ["A.lean", "B.lean"])

    def test_run_validation_writes_compatibility_summary_fields(self) -> None:
        repo_path = self.make_repo()
        remote_url = "git@example.com:test/repo.git"
        config = self.make_config(repo_path, git_remote_url=remote_url)
        self.git(repo_path, "init", "-b", "main")
        self.git(repo_path, "config", "user.name", "Test User")
        self.git(repo_path, "config", "user.email", "test@example.com")
        self.git(repo_path, "remote", "add", "origin", remote_url)
        (repo_path / "lakefile.toml").write_text(
            'name = "T"\nversion = "0.1.0"\ndefaultTargets = ["T"]\n\n[[lean_lib]]\nname = "T"\n',
            encoding="utf-8",
        )
        (repo_path / "lean-toolchain").write_text("leanprover/lean4:v4.28.0\n", encoding="utf-8")
        (repo_path / "T.lean").write_text("def t : Nat := 0\n", encoding="utf-8")
        (repo_path / "PaperDefinitions.lean").write_text("def paperDef : Nat := 0\n", encoding="utf-8")
        (repo_path / "PaperTheorems.lean").write_text("theorem paperStmt : True := by\n  trivial\n", encoding="utf-8")
        self.git(repo_path, "add", ".")
        self.git(repo_path, "commit", "-m", "init")

        summary = supervisor.run_validation(config, "proof_formalization", 1)

        self.assertEqual(summary["build_ok"], summary["build"]["ok"])
        self.assertEqual(
            summary["git_ok"],
            bool(
                summary["git"]["enabled"]
                and summary["git"]["repo_ok"]
                and summary["git"]["worktree_clean"]
                and summary["git"]["remote_matches_config"]
            ),
        )
        self.assertEqual(summary["head"], summary["git"]["head"])
        written = supervisor.JsonFile.load(supervisor.validation_summary_path(config), {})
        self.assertEqual(written["build_ok"], summary["build_ok"])
        self.assertEqual(written["git_ok"], summary["git_ok"])
        self.assertEqual(written["head"], summary["head"])

    def test_cleanup_phase_stuck_review_does_not_trigger_stuck_recovery(self) -> None:
        state = {
            "last_review": {
                "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                "decision": "STUCK",
                "cycle": 12,
            },
            "stuck_recovery_attempts": [],
            "stuck_recovery_last_trigger_cycle": None,
        }

        self.assertFalse(supervisor.has_unhandled_stuck_review(state))
        self.assertFalse(supervisor.can_attempt_stuck_recovery(state))

    def test_restore_cleanup_last_good_commit_resets_repo(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True, capture_output=True, text=True)
        (repo_path / "tracked.txt").write_text("good\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo_path, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "good"], cwd=repo_path, check=True, capture_output=True, text=True)
        good_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (repo_path / "tracked.txt").write_text("bad\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "bad"], cwd=repo_path, check=True, capture_output=True, text=True)

        state = {"cleanup_last_good_commit": good_head}
        with (
            mock.patch.object(
                supervisor,
                "run_validation",
                return_value={
                    "cycle": 2,
                    "phase": supervisor.PHASE_PROOF_COMPLETE_STYLE_CLEANUP,
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                    "git": {"head": good_head},
                },
            ),
            mock.patch.object(supervisor, "record_chat_event"),
            mock.patch.object(supervisor, "save_state"),
        ):
            supervisor.restore_cleanup_last_good_commit(
                config,
                state,
                cycle=2,
                reason="cleanup stalled",
            )

        restored_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(restored_head, good_head)

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
        self.assertTrue((chat_root / "_assets" / "viewer-version.json").exists())
        self.assertTrue((chat_root / config.chat.repo_name / "index.html").exists())
        viewer_version = json.loads((chat_root / "_assets" / "viewer-version.json").read_text(encoding="utf-8"))["version"]
        root_index = (chat_root / "index.html").read_text(encoding="utf-8")
        markdown_index = (chat_root / "_assets" / "markdown-viewer.html").read_text(encoding="utf-8")
        self.assertRegex(viewer_version, r"^[0-9a-f]{12}$")
        self.assertIn(f'data-viewer-version="{viewer_version}"', root_index)
        self.assertIn(f'data-viewer-version="{viewer_version}"', markdown_index)

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

    def test_chat_event_export_writes_events_manifest_and_chunk_files(self) -> None:
        repo_path = self.make_repo()
        chat_root = repo_path.parent / "chat site"
        config = self.make_config(repo_path, chat_root_dir=chat_root, start_phase="planning")
        state = {"phase": "planning", "cycle": 2, "awaiting_human_input": False}

        supervisor.record_chat_event(
            config,
            state,
            cycle=2,
            phase="planning",
            kind="worker_prompt",
            actor="supervisor",
            target="worker",
            content="Plan the project.",
            content_type="text",
        )
        supervisor.record_chat_event(
            config,
            state,
            cycle=31,
            phase="planning",
            kind="reviewer_decision",
            actor="reviewer",
            target="supervisor",
            content={
                "phase": "planning",
                "decision": "CONTINUE",
                "confidence": 0.7,
                "reason": "Keep going.",
                "next_prompt": "Continue.",
            },
            content_type="json",
        )

        manifest = json.loads(supervisor.chat_repo_events_manifest_path(config).read_text(encoding="utf-8"))
        self.assertEqual(manifest["chunk_size_cycles"], supervisor.CHAT_EVENT_CYCLE_CHUNK_SIZE)
        self.assertEqual(len(manifest["chunks"]), 2)
        self.assertEqual(manifest["chunks"][0]["start_cycle"], 26)
        self.assertEqual(manifest["chunks"][0]["end_cycle"], 50)
        self.assertEqual(manifest["chunks"][1]["start_cycle"], 1)
        self.assertEqual(manifest["chunks"][1]["end_cycle"], 25)

        newer_chunk = chat_root / config.chat.repo_name / manifest["chunks"][0]["file"]
        older_chunk = chat_root / config.chat.repo_name / manifest["chunks"][1]["file"]
        self.assertTrue(newer_chunk.exists())
        self.assertTrue(older_chunk.exists())
        newer_lines = [json.loads(line) for line in newer_chunk.read_text(encoding="utf-8").splitlines() if line.strip()]
        older_lines = [json.loads(line) for line in older_chunk.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(newer_lines[0]["cycle"], 31)
        self.assertEqual(older_lines[0]["cycle"], 2)

    def test_ensure_chat_event_chunks_backfills_legacy_log(self) -> None:
        repo_path = self.make_repo()
        chat_root = repo_path.parent / "chat site"
        config = self.make_config(repo_path, chat_root_dir=chat_root, start_phase="planning")
        repo_dir = supervisor.chat_repo_dir(config)
        repo_dir.mkdir(parents=True, exist_ok=True)
        legacy_events = [
            {
                "timestamp": "2026-03-27T12:00:00-04:00",
                "repo_name": config.chat.repo_name,
                "cycle": 3,
                "phase": "planning",
                "kind": "worker_prompt",
                "actor": "supervisor",
                "target": "worker",
                "content_type": "text",
                "summary": "Prompt",
                "content": "Plan.",
            },
            {
                "timestamp": "2026-03-27T12:05:00-04:00",
                "repo_name": config.chat.repo_name,
                "cycle": 29,
                "phase": "planning",
                "kind": "reviewer_decision",
                "actor": "reviewer",
                "target": "supervisor",
                "content_type": "json",
                "summary": "Decision",
                "content": {"phase": "planning", "decision": "CONTINUE"},
            },
        ]
        supervisor.chat_repo_events_path(config).write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in legacy_events),
            encoding="utf-8",
        )
        manifest_path = supervisor.chat_repo_events_manifest_path(config)
        if manifest_path.exists():
            manifest_path.unlink()

        manifest = supervisor.ensure_chat_event_chunks(config)

        self.assertEqual(len(manifest["chunks"]), 2)
        chunk_paths = [chat_root / config.chat.repo_name / entry["file"] for entry in manifest["chunks"]]
        self.assertTrue(all(path.exists() for path in chunk_paths))
        newer_lines = [json.loads(line) for line in chunk_paths[0].read_text(encoding="utf-8").splitlines() if line.strip()]
        older_lines = [json.loads(line) for line in chunk_paths[1].read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(newer_lines[0]["cycle"], 29)
        self.assertEqual(older_lines[0]["cycle"], 3)

    def test_chat_event_export_writes_codex_budget_status_file(self) -> None:
        repo_path = self.make_repo()
        chat_root = repo_path.parent / "chat site"
        config = self.make_config(repo_path, chat_root_dir=chat_root, start_phase="planning")
        state = {"phase": "planning", "cycle": 2, "awaiting_human_input": False}
        fake_status = {
            "timestamp": "2026-03-26T18:01:00Z",
            "source_path": "/tmp/session.jsonl",
            "plan_type": "pro",
            "used_percent": 44.0,
            "percent_left": 56.0,
            "window_minutes": 10080,
            "resets_at": 1775081890,
        }

        with mock.patch.object(supervisor, "latest_codex_weekly_budget_status", return_value=fake_status):
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

        payload = json.loads((chat_root / "codex-budget.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["available"])
        self.assertEqual(payload["percent_left"], 56.0)
        self.assertEqual(payload["used_percent"], 44.0)
        self.assertEqual(payload["window_minutes"], 10080)
        self.assertEqual(payload["resets_at"], 1775081890)

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

    def test_chat_markdown_refresher_also_refreshes_codex_budget_status(self) -> None:
        repo_path = self.make_repo()
        chat_root = repo_path.parent / "chat site"
        config = self.make_config(repo_path, chat_root_dir=chat_root, start_phase="planning")
        supervisor.ensure_repo_files(config, "planning")
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
        fake_status = {
            "timestamp": "2026-03-27T16:53:22.283Z",
            "source_path": "/tmp/session.jsonl",
            "plan_type": "pro",
            "used_percent": 27.0,
            "percent_left": 73.0,
            "window_minutes": 10080,
            "resets_at": 1775181392,
        }

        with mock.patch.object(supervisor, "latest_codex_weekly_budget_status", return_value=fake_status):
            refresher = supervisor.ChatMarkdownRefresher(config, interval_seconds=0.0)
            refresher.maybe_refresh(force=True)

        payload = json.loads((chat_root / "codex-budget.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["available"])
        self.assertEqual(payload["percent_left"], 73.0)
        self.assertEqual(payload["used_percent"], 27.0)


class TheoremFrontierTests(SupervisorTestCase):
    def paper_main_results_manifest(self, **overrides: object) -> dict:
        payload = {
            "phase": "theorem_stating",
            "nodes": [
                self.full_node(
                    "paper.main",
                    kind="paper",
                    natural_language_statement="The paper main theorem holds.",
                    lean_statement="def paperMainStatement : Prop := True",
                    lean_anchor="PaperTheorems.paperMainStatement",
                    paper_provenance="Paper Theorem `main`.",
                    closure_mode="all_children",
                    blocker_cluster="paper main result",
                    acceptance_evidence="Close this paper-facing result or approved descendants that prove it.",
                    notes="Primary graph theorem.",
                ),
                self.full_node(
                    "paper.main2",
                    kind="paper",
                    natural_language_statement="The paper Ramsey corollary holds.",
                    lean_statement="def paperMain2Statement : Prop := True",
                    lean_anchor="PaperTheorems.paperMain2Statement",
                    paper_provenance="Paper Theorem `main2`.",
                    closure_mode="all_children",
                    blocker_cluster="paper ramsey corollary",
                    acceptance_evidence="Close this paper-facing result or approved descendants that prove it.",
                    notes="Ramsey-number corollary.",
                ),
            ],
            "edges": [
                {
                    "parent": "paper.main2",
                    "child": "paper.main",
                    "edge_type": "reduction",
                    "justification": "The Ramsey corollary reduces to the main graph theorem.",
                    "natural_language_proof": "The paper proves the Ramsey corollary by applying the main graph theorem and then translating the witness into the Ramsey-number statement.",
                },
                {
                    "parent": "paper.main",
                    "child": "paper.main",
                    "edge_type": "direct_proof",
                    "justification": "At the current coarse paper level, the main theorem is treated as a direct proof obligation.",
                    "natural_language_proof": "The paper's remaining argument for the main theorem is deferred to later edge expansions or direct closure work on this coarse obligation.",
                }
            ],
            "initial_active_edge_id": supervisor.theorem_frontier_edge_id("paper.main", "direct_proof", "paper.main"),
        }
        payload.update(overrides)
        return payload

    def direct_edge_id(self, node_id: str) -> str:
        return supervisor.theorem_frontier_edge_id(node_id, "direct_proof", node_id)

    def dependency_edge_id(self, parent: str, child: str, edge_type: str = "all_of") -> str:
        return supervisor.theorem_frontier_edge_id(parent, edge_type, child)

    def direct_proof_edge(self, node_id: str, **overrides: object) -> dict:
        payload = {
            "parent": node_id,
            "child": node_id,
            "edge_type": "direct_proof",
            "justification": "This theorem is taken as a direct proof obligation at the current DAG granularity.",
            "natural_language_proof": "At the current DAG granularity, this theorem is attacked directly rather than by further coarse decomposition.",
        }
        payload.update(overrides)
        return payload

    def full_node(self, node_id: str = "ri.local.graph_pair", **overrides: object) -> dict:
        payload = {
            "node_id": node_id,
            "kind": "support",
            "natural_language_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
            "natural_language_proof": "By the paper's local conditioning argument, this obligation follows from the already established parent edge decomposition and the stated hypotheses.",
            "lean_statement": f"theorem {node_id.replace('.', '_')} : True := by trivial",
            "lean_anchor": f"Twobites.IndependentSets.{node_id.replace('.', '_')}",
            "paper_provenance": "Section 4 fixed-embedding RI collapse.",
            "closure_mode": "leaf",
            "blocker_cluster": "graph-pair local RI collapse",
            "acceptance_evidence": "A proved theorem closing the fixed graph-pair RI bound.",
            "notes": "The live endgame bottleneck.",
        }
        payload.update(overrides)
        return payload

    def full_edge(
        self,
        parent: str = "ri.local.graph_pair",
        child: str = "ri.local.remaining",
        **overrides: object,
    ) -> dict:
        payload = {
            "parent": parent,
            "child": child,
            "edge_type": "all_of",
            "justification": "This child obligation is required to discharge the parent theorem.",
            "natural_language_proof": "The parent theorem reduces to this child obligation by the paper-faithful decomposition stated for this frontier step.",
        }
        payload.update(overrides)
        return payload

    def create_active_worker_update(self, node_id: str = "ri.local.graph_pair", **overrides: object) -> dict:
        payload = self.full_worker_update(
            active_edge_id=self.direct_edge_id(node_id),
            active_node_id=node_id,
            active_node=self.full_node(node_id),
            proposed_edges=[self.direct_proof_edge(node_id)],
            next_candidate_edge_ids=[self.direct_edge_id(node_id)],
            structural_change_reason="Create the initial direct-proof obligation edge for this active theorem.",
        )
        payload.update(overrides)
        return payload

    def create_active_paper_verifier_review(self, node_id: str = "ri.local.graph_pair", **overrides: object) -> dict:
        payload = self.paper_verifier_review(
            parent_node_id=node_id,
            change_kind="CREATE_ACTIVE",
            approved_node_ids=[node_id],
            approved_edges=[{"parent": node_id, "child": node_id, "edge_type": "direct_proof"}],
        )
        payload.update(overrides)
        return payload

    def create_active_nl_proof_verifier_review(self, node_id: str = "ri.local.graph_pair", **overrides: object) -> dict:
        payload = self.nl_proof_verifier_review(
            parent_node_id=node_id,
            change_kind="CREATE_ACTIVE",
            approved_node_ids=[node_id],
            approved_edges=[{"parent": node_id, "child": node_id, "edge_type": "direct_proof"}],
        )
        payload.update(overrides)
        return payload

    def full_worker_update(self, **overrides: object) -> dict:
        payload = {
            "phase": "proof_formalization",
            "active_edge_id": self.direct_edge_id("ri.local.graph_pair"),
            "active_node_id": "ri.local.graph_pair",
            "active_node": self.full_node(),
            "requested_action": "CLOSE",
            "cone_scope": "Only the active graph-pair theorem and its immediate mechanical support lemmas.",
            "allowed_edit_paths": ["Twobites/IndependentSets.lean"],
            "result_summary": "Stayed on the active theorem.",
            "proposed_nodes": [],
            "proposed_edges": [],
            "next_candidate_edge_ids": [self.direct_edge_id("ri.local.graph_pair")],
            "structural_change_reason": "",
        }
        payload.update(overrides)
        if "active_node" in overrides and "active_node_id" not in overrides and isinstance(payload.get("active_node"), dict):
            payload["active_node_id"] = payload["active_node"]["node_id"]
        return payload

    def full_frontier_review(self, **overrides: object) -> dict:
        payload = {
            "phase": "proof_formalization",
            "active_edge_id": self.direct_edge_id("ri.local.graph_pair"),
            "active_theorem_id": "ri.local.graph_pair",
            "active_theorem_nl_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
            "active_theorem_lean_statement": "theorem ri_local_graph_pair : True := by trivial",
            "active_theorem_anchor": "Twobites.IndependentSets.ri_local_graph_pair",
            "assessed_action": "CLOSE",
            "blocker_cluster": "graph-pair local RI collapse",
            "outcome": "STILL_OPEN",
            "next_active_edge_id": self.direct_edge_id("ri.local.graph_pair"),
            "cone_purity": "HIGH",
            "open_hypotheses": ["close the fixed graph-pair RI theorem"],
            "justification": "The active theorem remains the bottleneck.",
        }
        payload.update(overrides)
        if "active_theorem_id" in overrides and "active_edge_id" not in overrides:
            payload["active_edge_id"] = self.direct_edge_id(str(payload["active_theorem_id"]))
        return payload

    def paper_verifier_review(self, **overrides: object) -> dict:
        payload = {
            "phase": "proof_formalization",
            "parent_node_id": "ri.local.graph_pair",
            "change_kind": "EXPAND",
            "decision": "APPROVE",
            "classification": "paper_faithful_reformulation",
            "approved_node_ids": ["ri.local.remaining", "ri.local.red_cap"],
            "approved_edges": [
                {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                {"parent": "ri.local.graph_pair", "child": "ri.local.red_cap"},
            ],
            "justification": "This is the paper-faithful local split of the graph-pair RI obstruction.",
            "caveat": "",
        }
        payload.update(overrides)
        return payload

    def nl_proof_verifier_review(self, **overrides: object) -> dict:
        payload = {
            "phase": "proof_formalization",
            "parent_node_id": "ri.local.graph_pair",
            "change_kind": "EXPAND",
            "decision": "APPROVE",
            "approved_node_ids": ["ri.local.remaining", "ri.local.red_cap"],
            "approved_edges": [
                {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                {"parent": "ri.local.graph_pair", "child": "ri.local.red_cap"},
            ],
            "justification": "The newly introduced edge and leaf proofs are complete and rigorous.",
            "caveat": "",
        }
        payload.update(overrides)
        return payload

    def worker_update(self, **overrides: object) -> dict:
        payload = {
            "phase": "proof_formalization",
            "active_theorem_id": "ri.local.graph_pair",
            "active_theorem_nl_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
            "active_theorem_lean_statement": "theorem graph_pair_bad_event_bound : True := by trivial",
            "active_theorem_anchor": "Twobites.IndependentSets.graph_pair_bad_event_bound",
            "requested_action": "CLOSE",
            "blocker_cluster": "graph-pair local RI collapse",
            "cone_scope": "Only local graph-pair support lemmas needed for the active theorem.",
            "result_summary": "Reduced the remaining gap to one graph-pair lemma.",
        }
        payload.update(overrides)
        return payload

    def frontier_review(self, **overrides: object) -> dict:
        payload = {
            "phase": "proof_formalization",
            "active_theorem_id": "ri.local.graph_pair",
            "active_theorem_nl_statement": "For one good graph pair, the bad-event mass is bounded by the RI target.",
            "active_theorem_lean_statement": "theorem graph_pair_bad_event_bound : True := by trivial",
            "active_theorem_anchor": "Twobites.IndependentSets.graph_pair_bad_event_bound",
            "assessed_action": "CLOSE",
            "blocker_cluster": "graph-pair local RI collapse",
            "outcome": "STILL_OPEN",
            "justification": "The active theorem is still the bottleneck.",
        }
        payload.update(overrides)
        return payload

    def test_validate_theorem_frontier_edge_assigns_canonical_id_and_open_status(self) -> None:
        edge = supervisor.validate_theorem_frontier_edge(
            {
                "parent": "paper.main",
                "child": "support.lemma",
                "edge_type": "reduction",
                "justification": "Main theorem reduces to the support lemma.",
            },
            require_paper_status=False,
        )
        self.assertEqual(edge["edge_id"], "paper.main|reduction|support.lemma")
        self.assertEqual(edge["status"], "open")

    def test_sync_theorem_frontier_metrics_counts_effectively_closed_parent(self) -> None:
        payload = supervisor.default_theorem_frontier_payload("full")
        payload["nodes"] = {
            "paper.main": supervisor.theorem_frontier_node_record(
                self.full_node("paper.main", closure_mode="all_children", kind="paper"),
                status="open",
                parent_ids=[],
                child_ids=["support.lemma"],
            ),
            "support.lemma": supervisor.theorem_frontier_node_record(
                self.full_node("support.lemma"),
                status="closed",
                parent_ids=["paper.main"],
                child_ids=[],
            ),
        }
        payload["edges"] = [
            supervisor.validate_theorem_frontier_edge(
                {
                    "parent": "paper.main",
                    "child": "support.lemma",
                    "edge_type": "reduction",
                    "status": "closed",
                    "justification": "Main theorem reduces to the support lemma.",
                    "paper_verifier_status": "APPROVE",
                },
                require_paper_status=True,
            )
        ]
        supervisor.sync_theorem_frontier_metrics(payload)
        self.assertEqual(payload["metrics"]["closed_nodes_count"], 2)
        self.assertEqual(payload["metrics"]["paper_nodes_closed"], 1)
        self.assertEqual(payload["metrics"]["closed_edges_count"], 1)

    def test_ensure_repo_files_initializes_phase0_theorem_frontier_state(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="phase0")

        supervisor.ensure_repo_files(config, "proof_formalization")

        payload = json.loads(supervisor.theorem_frontier_state_path(config).read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], "phase0")
        self.assertIsNone(payload["current"])
        self.assertEqual(payload["metrics"]["active_theorem_age"], 0)
        self.assertEqual(payload["metrics"]["blocker_cluster_age"], 0)

    def test_ensure_repo_files_initializes_full_theorem_frontier_state(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")

        supervisor.ensure_repo_files(config, "proof_formalization")

        payload = json.loads(supervisor.theorem_frontier_state_path(config).read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], "full")
        self.assertIsNone(payload["active_edge_id"])
        self.assertEqual(payload["active_leaf_id"], None)
        self.assertEqual(payload["nodes"], {})
        self.assertEqual(payload["metrics"]["active_edge_age"], 0)
        self.assertEqual(payload["metrics"]["active_leaf_age"], 0)
        self.assertEqual(payload["metrics"]["failed_close_attempts"], 0)

    def test_validate_paper_main_results_manifest_accepts_main_results_and_edges(self) -> None:
        manifest = supervisor.validate_paper_main_results_manifest(
            "theorem_stating",
            self.paper_main_results_manifest(),
        )

        self.assertEqual(manifest["initial_active_edge_id"], self.direct_edge_id("paper.main"))
        self.assertEqual(len(manifest["nodes"]), 2)
        self.assertEqual(manifest["edges"][0]["parent"], "paper.main2")

    def test_validate_paper_main_results_manifest_rejects_support_node(self) -> None:
        manifest = self.paper_main_results_manifest()
        manifest["nodes"][1]["kind"] = "support"

        with self.assertRaises(supervisor.SupervisorError):
            supervisor.validate_paper_main_results_manifest("theorem_stating", manifest)

    def test_seed_theorem_frontier_from_main_results_manifest_populates_initial_dag(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        config.state_dir.mkdir(parents=True, exist_ok=True)
        state = {"phase": "theorem_stating"}

        payload = supervisor.seed_theorem_frontier_from_main_results_manifest(
            config,
            state,
            supervisor.validate_paper_main_results_manifest("theorem_stating", self.paper_main_results_manifest()),
            cycle=7,
        )

        self.assertEqual(payload["active_edge_id"], self.direct_edge_id("paper.main"))
        self.assertEqual(payload["active_leaf_id"], "paper.main")
        self.assertEqual(sorted(payload["nodes"].keys()), ["paper.main", "paper.main2"])
        self.assertEqual(payload["nodes"]["paper.main"]["status"], "active")
        self.assertEqual(payload["nodes"]["paper.main2"]["status"], "open")
        self.assertCountEqual(
            [edge["edge_id"] for edge in payload["edges"]],
            [
                self.dependency_edge_id("paper.main2", "paper.main", "reduction"),
                self.direct_edge_id("paper.main"),
            ],
        )
        saved_payload = json.loads(supervisor.theorem_frontier_state_path(config).read_text(encoding="utf-8"))
        self.assertEqual(saved_payload["active_edge_id"], self.direct_edge_id("paper.main"))
        self.assertEqual(saved_payload["active_leaf_id"], "paper.main")

    def test_phase0_worker_prompt_includes_theorem_frontier_requirements(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="phase0")

        prompt = supervisor.build_worker_prompt(
            config,
            {"phase": "proof_formalization", "review_log": []},
            "proof_formalization",
            False,
        )

        self.assertIn("Theorem-frontier artifact requirements (Phase 0)", prompt)
        self.assertIn("theorem_frontier_update.json", prompt)
        self.assertIn("exact Lean statement", prompt)
        self.assertIn("`CLOSE`, `EXPAND`, and `REFUTE_REPLACE`", prompt)

    def test_phase0_reviewer_prompt_includes_worker_frontier_json_and_review_requirements(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="phase0")
        state = {
            "phase": "proof_formalization",
            "review_log": [],
            "last_theorem_frontier_worker_update": self.worker_update(),
        }

        prompt = supervisor.build_reviewer_prompt(
            config,
            state,
            "proof_formalization",
            "worker terminal output",
            json.dumps({"status": "NOT_STUCK"}),
            {"build": {"ok": True}, "sorries": {"count": 0}, "axioms": {"unapproved": []}},
            False,
        )

        self.assertIn("Theorem-frontier review requirements (Phase 0)", prompt)
        self.assertIn("Worker theorem-frontier JSON", prompt)
        self.assertIn("theorem_frontier_review.json", prompt)
        self.assertIn('"active_theorem_id": "ri.local.graph_pair"', prompt)
        self.assertIn("Judge the cycle by theorem-frontier standards", prompt)

    def test_full_worker_prompt_includes_authoritative_dag_requirements(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")

        prompt = supervisor.build_worker_prompt(
            config,
            {"phase": "proof_formalization", "review_log": []},
            "proof_formalization",
            False,
        )

        self.assertIn("authoritative theorem-frontier DAG", prompt)
        self.assertIn('"active_edge_id": "stable obligation edge id"', prompt)
        self.assertIn('"active_node_id": "stable theorem node id"', prompt)
        self.assertIn('exact node object only if `active_node_id` is not already authoritative', prompt)
        self.assertIn('"allowed_edit_paths": ["repo-relative .lean files allowed inside that cone for this burst"]', prompt)
        self.assertIn('"proposed_nodes": [{ exact node objects, including `natural_language_proof` for any new leaf node }]', prompt)
        self.assertIn("active cone", prompt)
        self.assertIn("Any Lean file changed outside `allowed_edit_paths` will fail theorem-frontier cone validation", prompt)

    def test_full_reviewer_prompt_includes_paper_verifier_review(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        state = {
            "phase": "proof_formalization",
            "review_log": [],
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
            "last_theorem_frontier_worker_update": self.full_worker_update(),
            "last_theorem_frontier_paper_review": self.paper_verifier_review(),
            "last_theorem_frontier_nl_proof_review": self.nl_proof_verifier_review(),
        }

        prompt = supervisor.build_reviewer_prompt(
            config,
            state,
            "proof_formalization",
            "worker terminal output",
            json.dumps({"status": "NOT_STUCK"}),
            {"build": {"ok": True}, "sorries": {"count": 0}, "axioms": {"unapproved": []}},
            False,
        )

        self.assertIn("Paper-verifier structural review", prompt)
        self.assertIn("NL-proof verifier review", prompt)
        self.assertIn('"classification": "paper_faithful_reformulation"', prompt)
        self.assertIn('"cone_purity": "HIGH" | "MEDIUM" | "LOW"', prompt)

    def test_full_paper_verifier_prompt_mentions_structural_gate(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        state = {
            "phase": "proof_formalization",
            "review_log": [],
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        prompt = supervisor.build_theorem_frontier_paper_verifier_prompt(
            config,
            state,
            "proof_formalization",
            "worker terminal output",
            json.dumps({"status": "NOT_STUCK"}),
            self.full_worker_update(
                requested_action="EXPAND",
                proposed_nodes=[
                    self.full_node("ri.local.remaining"),
                    self.full_node("ri.local.red_cap"),
                ],
                proposed_edges=[
                    self.full_edge(
                        "ri.local.graph_pair",
                        "ri.local.remaining",
                        justification="Both local bounds are needed.",
                    )
                ],
                next_candidate_edge_ids=[self.direct_edge_id("ri.local.remaining")],
                structural_change_reason="Split the live blocker into local siblings.",
            ),
            False,
        )

        self.assertIn("paper-verifier for theorem-frontier structural edits", prompt)
        self.assertIn("paper-faithful reformulation", prompt)
        self.assertIn("theorem_frontier_paper_verifier.json", prompt)

    def test_full_nl_proof_verifier_prompt_mentions_rigor_gate(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        state = {
            "phase": "proof_formalization",
            "review_log": [],
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        prompt = supervisor.build_theorem_frontier_nl_proof_verifier_prompt(
            config,
            state,
            "proof_formalization",
            "worker terminal output",
            json.dumps({"status": "NOT_STUCK"}),
            self.full_worker_update(
                requested_action="EXPAND",
                proposed_nodes=[self.full_node("ri.local.remaining")],
                proposed_edges=[self.full_edge("ri.local.graph_pair", "ri.local.remaining")],
                next_candidate_edge_ids=[self.direct_edge_id("ri.local.remaining")],
                structural_change_reason="Split the blocker.",
            ),
            self.paper_verifier_review(
                approved_node_ids=["ri.local.remaining"],
                approved_edges=[{"parent": "ri.local.graph_pair", "child": "ri.local.remaining"}],
            ),
            False,
        )

        self.assertIn("NL-proof verifier", prompt)
        self.assertIn("paper-approved newly admitted edges and newly admitted leaf nodes", prompt)
        self.assertIn("theorem_frontier_nl_proof_verifier.json", prompt)

    def test_validate_theorem_frontier_worker_update_requires_exact_statements(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "active_theorem_lean_statement"):
            supervisor.validate_theorem_frontier_worker_update(
                "proof_formalization",
                self.worker_update(active_theorem_lean_statement=""),
            )

    def test_validate_theorem_frontier_worker_update_rejects_invalid_action(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "Invalid theorem frontier action"):
            supervisor.validate_theorem_frontier_worker_update(
                "proof_formalization",
                self.worker_update(requested_action="WRAP"),
            )

    def test_validate_theorem_frontier_review_rejects_invalid_outcome(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "Invalid theorem frontier outcome"):
            supervisor.validate_theorem_frontier_review(
                "proof_formalization",
                self.frontier_review(outcome="WRAPPER_PROGRESS"),
            )

    def test_validate_full_worker_update_requires_exact_active_node_statement(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "lean_statement"):
            supervisor.validate_theorem_frontier_worker_update_full(
                "proof_formalization",
                self.full_worker_update(
                    active_edge_id=self.direct_edge_id("ri.local.graph_pair"),
                    active_node_id="ri.local.graph_pair",
                    active_node=self.full_node(lean_statement=""),
                ),
            )

    def test_validate_full_worker_update_accepts_known_node_by_id_only(self) -> None:
        result = supervisor.validate_theorem_frontier_worker_update_full(
            "proof_formalization",
            self.full_worker_update(active_node=None),
        )
        self.assertEqual(result["active_node_id"], "ri.local.graph_pair")
        self.assertIsNone(result["active_node"])

    def test_validate_full_worker_update_rejects_missing_active_edge_id(self) -> None:
        legacy = self.full_worker_update()
        legacy.pop("active_edge_id")
        with self.assertRaisesRegex(supervisor.SupervisorError, "active_edge_id"):
            supervisor.validate_theorem_frontier_worker_update_full("proof_formalization", legacy)

    def test_validate_full_worker_update_rejects_phase_mismatch(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "phase mismatch"):
            supervisor.validate_theorem_frontier_worker_update_full(
                "proof_formalization",
                self.full_worker_update(phase="planning"),
            )

    def test_validate_full_worker_update_requires_allowed_edit_paths(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "allowed_edit_paths"):
            supervisor.validate_theorem_frontier_worker_update_full(
                "proof_formalization",
                self.full_worker_update(allowed_edit_paths=[]),
            )

    def test_validate_full_worker_update_rejects_non_relative_allowed_edit_paths(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "repo-relative path"):
            supervisor.validate_theorem_frontier_worker_update_full(
                "proof_formalization",
                self.full_worker_update(allowed_edit_paths=["/tmp/Bad.lean"]),
            )

    def test_validate_full_paper_verifier_review_rejects_invalid_classification(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "paper verifier classification"):
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.paper_verifier_review(classification="wrapper_progress"),
            )

    def test_validate_full_nl_proof_verifier_review_rejects_phase_mismatch(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "phase mismatch"):
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.nl_proof_verifier_review(phase="planning"),
            )

    def test_validate_full_frontier_review_rejects_phase_mismatch(self) -> None:
        with self.assertRaisesRegex(supervisor.SupervisorError, "phase mismatch"):
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(phase="planning"),
            )

    def test_validate_full_frontier_review_accepts_id_only_shape(self) -> None:
        review = self.full_frontier_review()
        review.pop("active_theorem_nl_statement")
        review.pop("active_theorem_lean_statement")
        review.pop("active_theorem_anchor")
        result = supervisor.validate_theorem_frontier_review_full("proof_formalization", review)
        self.assertEqual(result["active_theorem_id"], "ri.local.graph_pair")
        self.assertEqual(result["active_theorem_nl_statement"], "")

    def test_validate_full_worker_update_accepts_proposed_edges_referencing_existing_nodes(self) -> None:
        # Proposed edges may reference nodes already in the DAG (not just active/proposed).
        # The authoritative DAG check happens later in update_theorem_frontier_full_state.
        result = supervisor.validate_theorem_frontier_worker_update_full(
            "proof_formalization",
            self.full_worker_update(
                requested_action="EXPAND",
                proposed_nodes=[self.full_node("ri.local.remaining")],
                proposed_edges=[
                    self.full_edge(
                        "paper.main",
                        "ri.local.remaining",
                        justification="Attach to existing DAG node.",
                    )
                ],
                next_candidate_edge_ids=[self.direct_edge_id("ri.local.remaining")],
                structural_change_reason="Expand with edge to existing node.",
            ),
        )
        self.assertEqual(len(result["proposed_edges"]), 1)

    def test_apply_theorem_frontier_cone_file_guard_rejects_off_cone_lean_edit(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        validation_summary = {
            "git": {
                "changed_lean_files": ["Twobites/IndependentSets.lean", "Twobites/ParameterBounds.lean"],
            }
        }

        with self.assertRaisesRegex(supervisor.SupervisorError, "outside allowed_edit_paths"):
            supervisor.apply_theorem_frontier_cone_file_guard(
                config,
                "proof_formalization",
                validation_summary,
                self.full_worker_update(allowed_edit_paths=["Twobites/IndependentSets.lean"]),
            )

    def test_apply_theorem_frontier_cone_file_guard_accepts_declared_lean_edits(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        validation_summary = {
            "git": {
                "changed_lean_files": ["Twobites/IndependentSets.lean"],
            }
        }

        result = supervisor.apply_theorem_frontier_cone_file_guard(
            config,
            "proof_formalization",
            validation_summary,
            self.full_worker_update(allowed_edit_paths=["Twobites/IndependentSets.lean"]),
        )

        self.assertTrue(result["enforced"])
        self.assertEqual(result["disallowed_changed_lean_files"], [])

    def test_update_theorem_frontier_state_tracks_ages_and_counts(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="phase0")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {"phase": "proof_formalization", "theorem_frontier": None}

        first = supervisor.update_theorem_frontier_state(
            config,
            state,
            supervisor.validate_theorem_frontier_review("proof_formalization", self.frontier_review()),
            cycle=11,
        )
        self.assertEqual(first["active_theorem_age"], 1)
        self.assertEqual(first["blocker_cluster_age"], 1)
        self.assertEqual(first["closed_nodes_count"], 0)
        self.assertEqual(first["refuted_nodes_count"], 0)

        second = supervisor.update_theorem_frontier_state(
            config,
            state,
            supervisor.validate_theorem_frontier_review(
                "proof_formalization",
                self.frontier_review(outcome="NO_FRONTIER_PROGRESS"),
            ),
            cycle=12,
        )
        self.assertEqual(second["active_theorem_age"], 2)
        self.assertEqual(second["blocker_cluster_age"], 2)

        third = supervisor.update_theorem_frontier_state(
            config,
            state,
            supervisor.validate_theorem_frontier_review(
                "proof_formalization",
                self.frontier_review(
                    active_theorem_id="ri.local.embedding_sum",
                    active_theorem_anchor="Twobites.IndependentSets.embedding_sum_bound",
                    active_theorem_lean_statement="theorem embedding_sum_bound : True := by trivial",
                    assessed_action="EXPAND",
                    outcome="EXPANDED",
                ),
            ),
            cycle=13,
        )
        self.assertEqual(third["active_theorem_age"], 1)
        self.assertEqual(third["blocker_cluster_age"], 3)

        fourth = supervisor.update_theorem_frontier_state(
            config,
            state,
            supervisor.validate_theorem_frontier_review(
                "proof_formalization",
                self.frontier_review(
                    active_theorem_id="ri.local.replacement_route",
                    active_theorem_anchor="Twobites.IndependentSets.replacement_route",
                    active_theorem_lean_statement="theorem replacement_route : True := by trivial",
                    blocker_cluster="replacement route for RI collapse",
                    assessed_action="REFUTE_REPLACE",
                    outcome="REFUTED_REPLACED",
                ),
            ),
            cycle=14,
        )
        self.assertEqual(fourth["active_theorem_age"], 1)
        self.assertEqual(fourth["blocker_cluster_age"], 1)
        self.assertEqual(fourth["refuted_nodes_count"], 1)

        fifth = supervisor.update_theorem_frontier_state(
            config,
            state,
            supervisor.validate_theorem_frontier_review(
                "proof_formalization",
                self.frontier_review(
                    active_theorem_id="ri.local.replacement_route",
                    active_theorem_anchor="Twobites.IndependentSets.replacement_route",
                    active_theorem_lean_statement="theorem replacement_route : True := by trivial",
                    blocker_cluster="replacement route for RI collapse",
                    outcome="CLOSED",
                ),
            ),
            cycle=15,
        )
        self.assertEqual(fifth["active_theorem_age"], 2)
        self.assertEqual(fifth["blocker_cluster_age"], 2)
        self.assertEqual(fifth["closed_nodes_count"], 1)

        history_lines = supervisor.theorem_frontier_history_path(config).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(history_lines), 5)

    def test_update_theorem_frontier_full_state_records_expansion_and_escalation(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        active_payload = supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.create_active_worker_update(),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.create_active_paper_verifier_review(),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.create_active_nl_proof_verifier_review(),
            ),
            cycle=11,
        )
        self.assertEqual(active_payload["active_edge_id"], self.direct_edge_id("ri.local.graph_pair"))
        self.assertEqual(active_payload["active_leaf_id"], "ri.local.graph_pair")
        self.assertEqual(active_payload["metrics"]["active_edge_age"], 1)
        self.assertEqual(active_payload["metrics"]["active_leaf_age"], 1)
        self.assertEqual(active_payload["metrics"]["failed_close_attempts"], 1)

        expanded_payload = supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.full_worker_update(
                requested_action="EXPAND",
                proposed_nodes=[
                    self.full_node("ri.local.remaining"),
                    self.full_node("ri.local.red_cap"),
                ],
                proposed_edges=[
                    self.full_edge(
                        "ri.local.graph_pair",
                        "ri.local.remaining",
                        justification="The remaining-pairs lower bound is needed.",
                    ),
                    self.direct_proof_edge("ri.local.remaining"),
                    self.full_edge(
                        "ri.local.graph_pair",
                        "ri.local.red_cap",
                        justification="The red cap is also needed.",
                    ),
                    self.direct_proof_edge("ri.local.red_cap"),
                ],
                next_candidate_edge_ids=[
                    self.direct_edge_id("ri.local.remaining"),
                    self.direct_edge_id("ri.local.red_cap"),
                ],
                structural_change_reason="Split the live blocker into local siblings.",
            ),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(
                    assessed_action="EXPAND",
                    outcome="EXPANDED",
                    next_active_edge_id=self.direct_edge_id("ri.local.remaining"),
                    open_hypotheses=["close the remaining-pairs lower bound child"],
                ),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.paper_verifier_review(
                    approved_edges=[
                        {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                        {"parent": "ri.local.remaining", "child": "ri.local.remaining", "edge_type": "direct_proof"},
                        {"parent": "ri.local.graph_pair", "child": "ri.local.red_cap"},
                        {"parent": "ri.local.red_cap", "child": "ri.local.red_cap", "edge_type": "direct_proof"},
                    ],
                ),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.nl_proof_verifier_review(
                    approved_edges=[
                        {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                        {"parent": "ri.local.remaining", "child": "ri.local.remaining", "edge_type": "direct_proof"},
                        {"parent": "ri.local.graph_pair", "child": "ri.local.red_cap"},
                        {"parent": "ri.local.red_cap", "child": "ri.local.red_cap", "edge_type": "direct_proof"},
                    ],
                ),
            ),
            cycle=12,
        )
        self.assertEqual(expanded_payload["active_edge_id"], self.direct_edge_id("ri.local.remaining"))
        self.assertEqual(expanded_payload["active_leaf_id"], "ri.local.remaining")
        self.assertIn("ri.local.red_cap", expanded_payload["nodes"])
        self.assertEqual(expanded_payload["nodes"]["ri.local.graph_pair"]["status"], "open")
        self.assertEqual(expanded_payload["nodes"]["ri.local.remaining"]["status"], "active")
        dependency_edges = [edge for edge in expanded_payload["edges"] if edge["edge_type"] != "direct_proof"]
        self.assertEqual(len(dependency_edges), 2)
        self.assertEqual(
            sorted((edge["parent"], edge["child"]) for edge in dependency_edges),
            [
                ("ri.local.graph_pair", "ri.local.red_cap"),
                ("ri.local.graph_pair", "ri.local.remaining"),
            ],
        )

        low_purity_payload = supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.full_worker_update(
                active_edge_id=self.direct_edge_id("ri.local.remaining"),
                active_node=self.full_node("ri.local.remaining"),
                next_candidate_edge_ids=[self.direct_edge_id("ri.local.remaining")],
            ),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(
                    active_theorem_id="ri.local.remaining",
                    active_theorem_lean_statement="theorem ri_local_remaining : True := by trivial",
                    active_theorem_anchor="Twobites.IndependentSets.ri_local_remaining",
                    outcome="NO_FRONTIER_PROGRESS",
                    next_active_edge_id=self.direct_edge_id("ri.local.remaining"),
                    cone_purity="LOW",
                    open_hypotheses=["still missing the remaining-pairs lower bound"],
                ),
            ),
            None,
            cycle=13,
        )
        low_purity_payload = supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.full_worker_update(
                active_edge_id=self.direct_edge_id("ri.local.remaining"),
                active_node=self.full_node("ri.local.remaining"),
                next_candidate_edge_ids=[self.direct_edge_id("ri.local.remaining")],
            ),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(
                    active_theorem_id="ri.local.remaining",
                    active_theorem_lean_statement="theorem ri_local_remaining : True := by trivial",
                    active_theorem_anchor="Twobites.IndependentSets.ri_local_remaining",
                    outcome="NO_FRONTIER_PROGRESS",
                    next_active_edge_id=self.direct_edge_id("ri.local.remaining"),
                    cone_purity="LOW",
                    open_hypotheses=["still missing the remaining-pairs lower bound"],
                ),
            ),
            None,
            cycle=14,
        )
        self.assertTrue(low_purity_payload["escalation"]["required"])
        self.assertIn("low cone purity", " ".join(low_purity_payload["escalation"]["reasons"]))

    def test_update_theorem_frontier_full_state_rejects_unapproved_create_active(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        with self.assertRaisesRegex(supervisor.SupervisorError, "Cannot create or activate"):
            supervisor.update_theorem_frontier_full_state(
                config,
                state,
                self.create_active_worker_update(),
                supervisor.validate_theorem_frontier_review_full(
                    "proof_formalization",
                    self.full_frontier_review(),
                ),
                supervisor.validate_theorem_frontier_paper_verifier_review(
                    "proof_formalization",
                    self.create_active_paper_verifier_review(
                        decision="REJECT",
                        approved_node_ids=[],
                        approved_edges=[],
                    ),
                ),
                supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                    "proof_formalization",
                    self.create_active_nl_proof_verifier_review(
                        approved_node_ids=[],
                        approved_edges=[],
                    ),
                ),
                cycle=11,
            )

    def test_update_theorem_frontier_full_state_ignores_echo_drift_for_existing_node(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.create_active_worker_update(),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.create_active_paper_verifier_review(),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.create_active_nl_proof_verifier_review(),
            ),
            cycle=11,
        )

        payload = supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.full_worker_update(
                active_edge_id=self.direct_edge_id("ri.local.graph_pair"),
                active_node=self.full_node(
                    "ri.local.graph_pair",
                    lean_statement="theorem altered_statement : False := by trivial",
                    natural_language_statement="Altered statement",
                ),
            ),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(
                    active_theorem_lean_statement="theorem altered_statement : False := by trivial",
                    active_theorem_nl_statement="Altered statement",
                ),
            ),
            None,
            cycle=12,
        )
        self.assertEqual(
            payload["nodes"]["ri.local.graph_pair"]["lean_statement"],
            "theorem ri_local_graph_pair : True := by trivial",
        )
        warnings_text = (config.state_dir / "logs" / "supervisor_warnings.jsonl").read_text(encoding="utf-8")
        self.assertIn("frontier_active_node_echo_drift", warnings_text)

    def test_update_theorem_frontier_full_state_rejects_closed_node_as_next_active(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        with self.assertRaisesRegex(supervisor.SupervisorError, "cannot be both CLOSED and the next active"):
            supervisor.update_theorem_frontier_full_state(
                config,
                state,
                self.create_active_worker_update(),
                supervisor.validate_theorem_frontier_review_full(
                    "proof_formalization",
                    self.full_frontier_review(
                        outcome="CLOSED",
                        next_active_edge_id=self.direct_edge_id("ri.local.graph_pair"),
                        open_hypotheses=[],
                    ),
                ),
                supervisor.validate_theorem_frontier_paper_verifier_review(
                    "proof_formalization",
                    self.create_active_paper_verifier_review(),
                ),
                supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                    "proof_formalization",
                    self.create_active_nl_proof_verifier_review(),
                ),
                cycle=11,
            )

    def test_update_theorem_frontier_full_state_rejects_closing_leaf_node_with_dependency_children(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.create_active_worker_update(),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.create_active_paper_verifier_review(),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.create_active_nl_proof_verifier_review(),
            ),
            cycle=11,
        )

        supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.full_worker_update(
                requested_action="EXPAND",
                proposed_nodes=[self.full_node("ri.local.remaining")],
                proposed_edges=[
                    self.full_edge(
                        "ri.local.graph_pair",
                        "ri.local.remaining",
                        justification="The remaining-pairs child is needed.",
                    ),
                    self.direct_proof_edge("ri.local.remaining"),
                ],
                next_candidate_edge_ids=[self.direct_edge_id("ri.local.graph_pair")],
                structural_change_reason="Split the blocker but keep the parent active.",
            ),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(
                    assessed_action="EXPAND",
                    outcome="EXPANDED",
                    next_active_edge_id=self.direct_edge_id("ri.local.graph_pair"),
                    open_hypotheses=["close the remaining-pairs child"],
                ),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.paper_verifier_review(
                    approved_node_ids=["ri.local.remaining"],
                    approved_edges=[
                        {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                        {"parent": "ri.local.remaining", "child": "ri.local.remaining", "edge_type": "direct_proof"},
                    ],
                ),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.nl_proof_verifier_review(
                    approved_node_ids=["ri.local.remaining"],
                    approved_edges=[
                        {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                        {"parent": "ri.local.remaining", "child": "ri.local.remaining", "edge_type": "direct_proof"},
                    ],
                ),
            ),
            cycle=12,
        )

        payload = supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.full_worker_update(),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(
                    outcome="CLOSED",
                    next_active_edge_id="",
                    open_hypotheses=[],
                ),
            ),
            None,
            cycle=13,
        )
        self.assertEqual(payload["nodes"]["ri.local.graph_pair"]["status"], "open")
        self.assertEqual(payload["edges"][0]["edge_type"], "direct_proof")
        self.assertEqual(payload["edges"][0]["status"], "closed")

    def test_update_theorem_frontier_full_state_only_admits_paper_approved_subset(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.create_active_worker_update(),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.create_active_paper_verifier_review(),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.create_active_nl_proof_verifier_review(),
            ),
            cycle=11,
        )

        payload = supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.full_worker_update(
                requested_action="EXPAND",
                proposed_nodes=[
                    self.full_node("ri.local.remaining"),
                    self.full_node("ri.local.red_cap"),
                ],
                proposed_edges=[
                    self.full_edge(
                        "ri.local.graph_pair",
                        "ri.local.remaining",
                        justification="The remaining-pairs child is needed.",
                    ),
                    self.direct_proof_edge("ri.local.remaining"),
                    self.full_edge(
                        "ri.local.graph_pair",
                        "ri.local.red_cap",
                        justification="The red-cap child is proposed too.",
                    ),
                ],
                next_candidate_edge_ids=[self.direct_edge_id("ri.local.remaining")],
                structural_change_reason="Split the blocker in two.",
            ),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(
                    assessed_action="EXPAND",
                    outcome="EXPANDED",
                    next_active_edge_id=self.direct_edge_id("ri.local.remaining"),
                    open_hypotheses=["close the remaining-pairs child"],
                ),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.paper_verifier_review(
                    approved_node_ids=["ri.local.remaining"],
                    approved_edges=[
                        {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                        {"parent": "ri.local.remaining", "child": "ri.local.remaining", "edge_type": "direct_proof"},
                    ],
                ),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.nl_proof_verifier_review(
                    approved_node_ids=["ri.local.remaining"],
                    approved_edges=[
                        {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                        {"parent": "ri.local.remaining", "child": "ri.local.remaining", "edge_type": "direct_proof"},
                    ],
                ),
            ),
            cycle=12,
        )
        self.assertIn("ri.local.remaining", payload["nodes"])
        self.assertNotIn("ri.local.red_cap", payload["nodes"])
        dependency_edges = [edge for edge in payload["edges"] if edge["edge_type"] != "direct_proof"]
        self.assertEqual(
            [(edge["parent"], edge["child"]) for edge in dependency_edges],
            [("ri.local.graph_pair", "ri.local.remaining")],
        )

    def test_update_theorem_frontier_full_state_rejects_next_active_not_paper_approved(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.create_active_worker_update(),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.create_active_paper_verifier_review(),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.create_active_nl_proof_verifier_review(),
            ),
            cycle=11,
        )

        with self.assertRaisesRegex(supervisor.SupervisorError, "Next active edge id"):
            supervisor.update_theorem_frontier_full_state(
                config,
                state,
                self.full_worker_update(
                    requested_action="EXPAND",
                    proposed_nodes=[self.full_node("ri.local.remaining")],
                proposed_edges=[
                    self.full_edge(
                        "ri.local.graph_pair",
                        "ri.local.remaining",
                        justification="The remaining-pairs child is proposed.",
                    ),
                    self.direct_proof_edge("ri.local.remaining"),
                ],
                    next_candidate_edge_ids=[self.direct_edge_id("ri.local.remaining")],
                    structural_change_reason="Split the blocker.",
                ),
                supervisor.validate_theorem_frontier_review_full(
                    "proof_formalization",
                    self.full_frontier_review(
                        assessed_action="EXPAND",
                        outcome="EXPANDED",
                        next_active_edge_id=self.direct_edge_id("ri.local.remaining"),
                        open_hypotheses=["close the remaining-pairs child"],
                    ),
                ),
                supervisor.validate_theorem_frontier_paper_verifier_review(
                    "proof_formalization",
                    self.paper_verifier_review(
                        approved_node_ids=[],
                        approved_edges=[],
                    ),
                ),
                supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                    "proof_formalization",
                    self.nl_proof_verifier_review(
                        approved_node_ids=[],
                        approved_edges=[],
                    ),
                ),
                cycle=12,
            )

    def test_update_theorem_frontier_full_state_rejects_approved_edge_to_unapproved_node(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.create_active_worker_update(),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.create_active_paper_verifier_review(),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.create_active_nl_proof_verifier_review(),
            ),
            cycle=11,
        )

        with self.assertRaisesRegex(supervisor.SupervisorError, "approved_edges may only reference nodes"):
            supervisor.update_theorem_frontier_full_state(
                config,
                state,
                self.full_worker_update(
                    requested_action="EXPAND",
                    proposed_nodes=[self.full_node("ri.local.remaining")],
                    proposed_edges=[
                        self.full_edge(
                            "ri.local.graph_pair",
                            "ri.local.remaining",
                            justification="The remaining-pairs child is proposed.",
                        )
                    ],
                    next_candidate_edge_ids=[self.direct_edge_id("ri.local.remaining")],
                    structural_change_reason="Split the blocker.",
                ),
                supervisor.validate_theorem_frontier_review_full(
                    "proof_formalization",
                    self.full_frontier_review(
                        assessed_action="EXPAND",
                        outcome="EXPANDED",
                        next_active_edge_id=self.direct_edge_id("ri.local.graph_pair"),
                        open_hypotheses=["close the remaining-pairs child"],
                    ),
                ),
                supervisor.validate_theorem_frontier_paper_verifier_review(
                    "proof_formalization",
                    self.paper_verifier_review(
                        approved_node_ids=[],
                        approved_edges=[{"parent": "ri.local.graph_pair", "child": "ri.local.remaining"}],
                    ),
                ),
                cycle=12,
            )

    def test_update_theorem_frontier_full_state_rejects_new_leaf_without_natural_language_proof(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.create_active_worker_update(),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.create_active_paper_verifier_review(),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.create_active_nl_proof_verifier_review(),
            ),
            cycle=11,
        )

        with self.assertRaisesRegex(supervisor.SupervisorError, "newly admitted leaf nodes"):
            supervisor.update_theorem_frontier_full_state(
                config,
                state,
                self.full_worker_update(
                    requested_action="EXPAND",
                    proposed_nodes=[self.full_node("ri.local.remaining", natural_language_proof="")],
                    proposed_edges=[
                        self.full_edge(
                            "ri.local.graph_pair",
                            "ri.local.remaining",
                            justification="The remaining-pairs child is needed.",
                        )
                    ],
                    next_candidate_edge_ids=[self.direct_edge_id("ri.local.graph_pair")],
                    structural_change_reason="Introduce the remaining-pairs child but keep the parent obligation active.",
                ),
                supervisor.validate_theorem_frontier_review_full(
                    "proof_formalization",
                    self.full_frontier_review(
                        assessed_action="EXPAND",
                        outcome="EXPANDED",
                        next_active_edge_id=self.direct_edge_id("ri.local.graph_pair"),
                        open_hypotheses=["close the remaining-pairs child"],
                    ),
                ),
                supervisor.validate_theorem_frontier_paper_verifier_review(
                    "proof_formalization",
                    self.paper_verifier_review(
                        approved_node_ids=["ri.local.remaining"],
                        approved_edges=[{"parent": "ri.local.graph_pair", "child": "ri.local.remaining"}],
                    ),
                ),
                supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                    "proof_formalization",
                    self.nl_proof_verifier_review(
                        approved_node_ids=["ri.local.remaining"],
                        approved_edges=[{"parent": "ri.local.graph_pair", "child": "ri.local.remaining"}],
                    ),
                ),
                cycle=12,
            )

    def test_update_theorem_frontier_full_state_rejects_new_edge_without_natural_language_proof(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.create_active_worker_update(),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.create_active_paper_verifier_review(),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.create_active_nl_proof_verifier_review(),
            ),
            cycle=11,
        )

        with self.assertRaisesRegex(supervisor.SupervisorError, "newly admitted edges"):
            supervisor.update_theorem_frontier_full_state(
                config,
                state,
                self.full_worker_update(
                    requested_action="EXPAND",
                    proposed_nodes=[self.full_node("ri.local.remaining")],
                    proposed_edges=[
                        self.full_edge(
                            "ri.local.graph_pair",
                            "ri.local.remaining",
                            natural_language_proof="",
                            justification="The remaining-pairs child is needed.",
                        ),
                        self.direct_proof_edge("ri.local.remaining"),
                    ],
                    next_candidate_edge_ids=[self.direct_edge_id("ri.local.remaining")],
                    structural_change_reason="Split to the remaining-pairs child.",
                ),
                supervisor.validate_theorem_frontier_review_full(
                    "proof_formalization",
                    self.full_frontier_review(
                        assessed_action="EXPAND",
                        outcome="EXPANDED",
                        next_active_edge_id=self.direct_edge_id("ri.local.remaining"),
                        open_hypotheses=["close the remaining-pairs child"],
                    ),
                ),
                supervisor.validate_theorem_frontier_paper_verifier_review(
                    "proof_formalization",
                    self.paper_verifier_review(
                        approved_node_ids=["ri.local.remaining"],
                        approved_edges=[
                            {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                            {"parent": "ri.local.remaining", "child": "ri.local.remaining", "edge_type": "direct_proof"},
                        ],
                    ),
                ),
                supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                    "proof_formalization",
                    self.nl_proof_verifier_review(
                        approved_node_ids=["ri.local.remaining"],
                        approved_edges=[
                            {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                            {"parent": "ri.local.remaining", "child": "ri.local.remaining", "edge_type": "direct_proof"},
                        ],
                    ),
                ),
                cycle=12,
            )

    def test_update_theorem_frontier_full_state_failed_close_attempts_track_leaf_not_blocker(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        supervisor.ensure_repo_files(config, "proof_formalization")
        state: dict = {
            "phase": "proof_formalization",
            "theorem_frontier": supervisor.default_theorem_frontier_payload("full"),
        }

        supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.create_active_worker_update(),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(),
            ),
            supervisor.validate_theorem_frontier_paper_verifier_review(
                "proof_formalization",
                self.create_active_paper_verifier_review(),
            ),
            supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                "proof_formalization",
                self.create_active_nl_proof_verifier_review(),
            ),
            cycle=11,
        )
        expanded = supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.full_worker_update(
                requested_action="EXPAND",
                proposed_nodes=[self.full_node("ri.local.remaining")],
                proposed_edges=[
                    self.full_edge(
                        "ri.local.graph_pair",
                        "ri.local.remaining",
                        justification="The remaining-pairs child is needed.",
                    ),
                    self.direct_proof_edge("ri.local.remaining"),
                ],
                next_candidate_edge_ids=[self.direct_edge_id("ri.local.remaining")],
                structural_change_reason="Split to the remaining-pairs child.",
            ),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(
                    assessed_action="EXPAND",
                    outcome="EXPANDED",
                    next_active_edge_id=self.direct_edge_id("ri.local.remaining"),
                    open_hypotheses=["close the remaining-pairs child"],
                ),
            ),
                supervisor.validate_theorem_frontier_paper_verifier_review(
                    "proof_formalization",
                    self.paper_verifier_review(
                        approved_node_ids=["ri.local.remaining"],
                        approved_edges=[
                            {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                            {"parent": "ri.local.remaining", "child": "ri.local.remaining", "edge_type": "direct_proof"},
                        ],
                    ),
                ),
                supervisor.validate_theorem_frontier_nl_proof_verifier_review(
                    "proof_formalization",
                    self.nl_proof_verifier_review(
                        approved_node_ids=["ri.local.remaining"],
                        approved_edges=[
                            {"parent": "ri.local.graph_pair", "child": "ri.local.remaining"},
                            {"parent": "ri.local.remaining", "child": "ri.local.remaining", "edge_type": "direct_proof"},
                        ],
                    ),
                ),
                cycle=12,
            )
        self.assertEqual(expanded["metrics"]["failed_close_attempts"], 0)

        next_payload = supervisor.update_theorem_frontier_full_state(
            config,
            state,
            self.full_worker_update(
                active_edge_id=self.direct_edge_id("ri.local.remaining"),
                active_node=self.full_node("ri.local.remaining"),
            ),
            supervisor.validate_theorem_frontier_review_full(
                "proof_formalization",
                self.full_frontier_review(
                    active_theorem_id="ri.local.remaining",
                    active_theorem_nl_statement="For one good graph pair, the bad-event mass is bounded by the RI target.",
                    active_theorem_lean_statement="theorem ri_local_remaining : True := by trivial",
                    active_theorem_anchor="Twobites.IndependentSets.ri_local_remaining",
                    blocker_cluster="graph-pair local RI collapse",
                    assessed_action="CLOSE",
                    outcome="NO_FRONTIER_PROGRESS",
                    next_active_edge_id=self.direct_edge_id("ri.local.remaining"),
                ),
            ),
            None,
            cycle=13,
        )
        self.assertEqual(next_payload["metrics"]["failed_close_attempts"], 1)
        self.assertEqual(next_payload["current_action"], "CLOSE")

    def test_load_state_rejects_invalid_theorem_frontier_payload(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        config.state_dir.mkdir(parents=True, exist_ok=True)
        supervisor.JsonFile.dump(
            config.state_dir / "state.json",
            {
                "phase": "proof_formalization",
                "theorem_frontier": {
                    "mode": "full",
                    "active_leaf_id": "missing.node",
                    "nodes": {},
                    "edges": [],
                    "metrics": {},
                    "escalation": {"required": False, "reasons": []},
                    "paper_verifier_history": [],
                    "current": None,
                },
            },
        )

        with self.assertRaisesRegex(supervisor.SupervisorError, "active_leaf_id"):
            supervisor.load_state(config)

    def test_load_state_rejects_multiple_active_theorem_frontier_nodes(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        config.state_dir.mkdir(parents=True, exist_ok=True)
        payload = supervisor.default_theorem_frontier_payload("full")
        payload["active_edge_id"] = self.direct_edge_id("ri.local.graph_pair")
        payload["active_leaf_id"] = "ri.local.graph_pair"
        payload["edges"] = [
            supervisor.validate_theorem_frontier_edge(
                {
                    "parent": "ri.local.graph_pair",
                    "child": "ri.local.graph_pair",
                    "edge_type": "direct_proof",
                    "status": "active",
                    "justification": "Direct proof obligation.",
                    "paper_verifier_status": "APPROVE",
                },
                require_paper_status=True,
            )
        ]
        payload["nodes"] = {
            "ri.local.graph_pair": supervisor.theorem_frontier_node_record(
                self.full_node("ri.local.graph_pair"),
                status="active",
                parent_ids=[],
                child_ids=[],
            ),
            "ri.local.remaining": supervisor.theorem_frontier_node_record(
                self.full_node("ri.local.remaining"),
                status="active",
                parent_ids=[],
                child_ids=[],
            ),
        }
        supervisor.JsonFile.dump(
            config.state_dir / "state.json",
            {
                "phase": "proof_formalization",
                "theorem_frontier": payload,
            },
        )

        with self.assertRaisesRegex(supervisor.SupervisorError, "exactly one active node"):
            supervisor.load_state(config)

    def test_load_state_repairs_closed_leaf_node_with_open_dependency_child(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        config.state_dir.mkdir(parents=True, exist_ok=True)
        payload = supervisor.default_theorem_frontier_payload("full")
        payload["nodes"] = {
            "ri.local.graph_pair": supervisor.theorem_frontier_node_record(
                self.full_node("ri.local.graph_pair"),
                status="closed",
                parent_ids=[],
                child_ids=["ri.local.remaining"],
            ),
            "ri.local.remaining": supervisor.theorem_frontier_node_record(
                self.full_node("ri.local.remaining"),
                status="open",
                parent_ids=["ri.local.graph_pair"],
                child_ids=[],
            ),
        }
        payload["edges"] = [
            supervisor.validate_theorem_frontier_edge(
                {
                    "parent": "ri.local.graph_pair",
                    "child": "ri.local.remaining",
                    "edge_type": "all_of",
                    "justification": "The child is required.",
                    "paper_verifier_status": "APPROVE",
                },
                require_paper_status=True,
            )
        ]
        supervisor.JsonFile.dump(
            config.state_dir / "state.json",
            {
                "phase": "proof_formalization",
                "theorem_frontier": payload,
            },
        )

        loaded = supervisor.load_state(config)
        self.assertEqual(loaded["theorem_frontier"]["nodes"]["ri.local.graph_pair"]["status"], "open")

    def test_load_state_keeps_any_child_node_closed_when_one_dependency_child_is_closed(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="full")
        config.state_dir.mkdir(parents=True, exist_ok=True)
        payload = supervisor.default_theorem_frontier_payload("full")
        payload["nodes"] = {
            "ri.local.graph_pair": supervisor.theorem_frontier_node_record(
                self.full_node("ri.local.graph_pair", closure_mode="any_child"),
                status="closed",
                parent_ids=[],
                child_ids=["ri.local.remaining", "ri.local.red_cap"],
            ),
            "ri.local.remaining": supervisor.theorem_frontier_node_record(
                self.full_node("ri.local.remaining"),
                status="closed",
                parent_ids=["ri.local.graph_pair"],
                child_ids=[],
            ),
            "ri.local.red_cap": supervisor.theorem_frontier_node_record(
                self.full_node("ri.local.red_cap"),
                status="open",
                parent_ids=["ri.local.graph_pair"],
                child_ids=[],
            ),
        }
        payload["edges"] = [
            supervisor.validate_theorem_frontier_edge(
                {
                    "parent": "ri.local.graph_pair",
                    "child": "ri.local.remaining",
                    "edge_type": "any_of",
                    "status": "closed",
                    "justification": "Either child route suffices.",
                    "paper_verifier_status": "APPROVE",
                },
                require_paper_status=True,
            ),
            supervisor.validate_theorem_frontier_edge(
                {
                    "parent": "ri.local.graph_pair",
                    "child": "ri.local.red_cap",
                    "edge_type": "any_of",
                    "justification": "Either child route suffices.",
                    "paper_verifier_status": "APPROVE",
                },
                require_paper_status=True,
            ),
        ]
        supervisor.JsonFile.dump(
            config.state_dir / "state.json",
            {
                "phase": "proof_formalization",
                "theorem_frontier": payload,
            },
        )

        loaded = supervisor.load_state(config)
        self.assertEqual(loaded["theorem_frontier"]["nodes"]["ri.local.graph_pair"]["status"], "closed")

    def test_recover_interrupted_worker_state_restores_phase0_frontier_artifact(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, theorem_frontier_phase="phase0")
        config.state_dir.mkdir(parents=True, exist_ok=True)
        (config.state_dir / "worker_handoff.json").write_text(
            json.dumps(
                {
                    "phase": "proof_formalization",
                    "status": "NOT_STUCK",
                    "summary_of_changes": "Made local progress.",
                    "current_frontier": "graph-pair bound",
                    "likely_next_step": "finish the active theorem",
                    "input_request": "",
                }
            ),
            encoding="utf-8",
        )
        supervisor.theorem_frontier_worker_update_path(config).write_text(
            json.dumps(self.worker_update()),
            encoding="utf-8",
        )
        state = {
            "phase": "proof_formalization",
            "cycle": 3,
            "review_log": [],
            "awaiting_human_input": False,
            "roles": {},
        }

        with (
            mock.patch.object(
                supervisor,
                "run_validation",
                return_value={
                    "cycle": 3,
                    "phase": "proof_formalization",
                    "build": {"ok": True},
                    "sorries": {"count": 0},
                    "axioms": {"unapproved": []},
                },
            ),
            mock.patch.object(supervisor, "record_chat_event") as record_chat_event_mock,
        ):
            recovered = supervisor.recover_interrupted_worker_state(config, state, "proof_formalization")

        self.assertTrue(recovered)
        self.assertEqual(state["last_theorem_frontier_worker_update"]["active_theorem_id"], "ri.local.graph_pair")
        kinds = [call.kwargs.get("kind") for call in record_chat_event_mock.mock_calls]
        self.assertIn("worker_handoff", kinds)
        self.assertIn("theorem_frontier_update", kinds)
        self.assertIn("validation_summary", kinds)


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

    def test_launch_tmux_burst_reuses_existing_live_window(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path, kill_windows_after_capture=False)
        supervisor.ensure_repo_files(config, "proof_formalization")

        adapter = DummyAdapter(
            supervisor.ProviderConfig(provider="claude", model=None, extra_args=[]),
            "worker",
            config,
            {},
            ["bash", "-lc", "echo should-not-run"],
        )

        prompt_stem = "worker-cycle-0007"
        runtime_dir = config.state_dir / "runtime"
        logs_dir = config.state_dir / "logs"
        start_file = runtime_dir / f"{prompt_stem}.started"
        exit_file = runtime_dir / f"{prompt_stem}.exit"
        per_cycle_log = logs_dir / f"{prompt_stem}.ansi.log"
        start_file.write_text("started\n", encoding="utf-8")
        exit_file.write_text("0\n", encoding="utf-8")
        per_cycle_log.write_text("existing-output\n", encoding="utf-8")

        def fake_tmux_cmd(*args: str, **kwargs):
            if args[:3] == ("capture-pane", "-p", "-t"):
                return subprocess.CompletedProcess(args, 0, stdout="pane-capture\n", stderr="")
            raise AssertionError(f"unexpected tmux call: {args}")

        with mock.patch.object(
            supervisor,
            "find_live_tmux_burst_pane",
            return_value={"window_id": "@1", "pane_id": "%9"},
        ), mock.patch.object(supervisor, "tmux_cmd", side_effect=fake_tmux_cmd):
            run = supervisor.launch_tmux_burst(adapter, 7, "ignored prompt", reuse_existing_window=True)

        self.assertEqual(run["window_id"], "@1")
        self.assertEqual(run["pane_id"], "%9")
        self.assertEqual(run["exit_code"], 0)
        self.assertIn("existing-output", run["captured_output"])
        latest_log = config.state_dir / "logs" / "worker.latest.ansi.log"
        self.assertTrue(latest_log.exists())
        self.assertIn("existing-output", latest_log.read_text(encoding="utf-8"))


class DagExportTests(SupervisorTestCase):
    def _make_frontier_state(self) -> dict:
        return {
            "theorem_frontier": {
                "mode": "full",
                "active_edge_id": "main_thm|reduction|lemma_a",
                "active_leaf_id": "main_thm",
                "current_action": "CLOSE",
                "nodes": {
                    "main_thm": {
                        "node_id": "main_thm",
                        "kind": "paper",
                        "status": "active",
                        "natural_language_statement": "Main theorem",
                        "lean_statement": "theorem main : True := trivial",
                        "lean_anchor": "main",
                        "paper_provenance": "Theorem 1",
                        "closure_mode": "leaf",
                        "blocker_cluster": "none",
                        "acceptance_evidence": "Lean build passes",
                        "notes": "root node",
                        "parent_ids": [],
                        "child_ids": ["lemma_a"],
                    },
                    "lemma_a": {
                        "node_id": "lemma_a",
                        "kind": "support",
                        "status": "open",
                        "natural_language_statement": "Lemma A",
                        "lean_statement": "lemma a : True := trivial",
                        "lean_anchor": "lemma_a",
                        "paper_provenance": "Section 3",
                        "closure_mode": "leaf",
                        "blocker_cluster": "graph bound",
                        "acceptance_evidence": "Lean build",
                        "notes": "",
                        "parent_ids": ["main_thm"],
                        "child_ids": [],
                    },
                },
                "edges": [
                    {
                        "edge_id": "main_thm|reduction|lemma_a",
                        "parent": "main_thm",
                        "child": "lemma_a",
                        "edge_type": "reduction",
                        "status": "active",
                        "justification": "main reduces to lemma",
                    },
                ],
                "metrics": {
                    "active_edge_age": 3,
                    "active_leaf_age": 3,
                    "blocker_cluster_age": 2,
                    "closed_nodes_count": 0,
                    "closed_edges_count": 0,
                    "refuted_nodes_count": 0,
                    "paper_nodes_closed": 0,
                    "failed_close_attempts": 0,
                    "low_cone_purity_streak": 0,
                    "cone_purity": "HIGH",
                    "structural_churn": 0,
                },
                "escalation": {"required": False, "reasons": []},
                "paper_verifier_history": [],
                "current": None,
            },
            "cycle": 10,
            "phase": "proof_formalization",
        }

    def test_frontier_summary_for_meta_returns_summary(self) -> None:
        state = self._make_frontier_state()
        summary = supervisor.frontier_summary_for_meta(state)
        self.assertIsNotNone(summary)
        self.assertTrue(summary["has_frontier"])
        self.assertEqual(summary["total_nodes"], 2)
        self.assertEqual(summary["active_leaf_id"], "main_thm")
        self.assertEqual(summary["status_counts"]["active"], 1)
        self.assertEqual(summary["status_counts"]["open"], 1)

    def test_frontier_summary_for_meta_returns_none_without_frontier(self) -> None:
        self.assertIsNone(supervisor.frontier_summary_for_meta({}))
        self.assertIsNone(supervisor.frontier_summary_for_meta({"theorem_frontier": {"mode": "phase0"}}))

    def test_export_dag_frontier_snapshot_writes_file(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = self._make_frontier_state()
        supervisor.dag_repo_dir(config).mkdir(parents=True, exist_ok=True)
        supervisor.export_dag_frontier_snapshot(config, state)
        path = supervisor.dag_frontier_path(config)
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "full")
        self.assertEqual(data["active_leaf_id"], "main_thm")
        self.assertIn("exported_at", data)
        self.assertIn("main_thm", data["nodes"])
        self.assertIn("lemma_a", data["nodes"])

    def test_export_dag_frontier_seed_writes_history(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = self._make_frontier_state()
        supervisor.dag_repo_dir(config).mkdir(parents=True, exist_ok=True)
        payload = state["theorem_frontier"]
        supervisor.export_dag_frontier_seed(config, payload, cycle=5)
        path = supervisor.dag_frontier_history_path(config)
        self.assertTrue(path.exists())
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["type"], "seed")
        self.assertEqual(entry["cycle"], 5)
        self.assertIn("main_thm", entry["nodes"])
        self.assertIn("lemma_a", entry["nodes"])

    def test_export_dag_frontier_cycle_writes_delta(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = self._make_frontier_state()
        supervisor.dag_repo_dir(config).mkdir(parents=True, exist_ok=True)
        before_ids = {"main_thm", "lemma_a"}
        # Add a new node
        state["theorem_frontier"]["nodes"]["lemma_b"] = {
            "node_id": "lemma_b",
            "kind": "support",
            "status": "open",
            "natural_language_statement": "Lemma B",
            "lean_statement": "lemma b : True := trivial",
            "lean_anchor": "lemma_b",
            "paper_provenance": "Section 4",
            "closure_mode": "leaf",
            "blocker_cluster": "graph bound",
            "acceptance_evidence": "Lean build",
            "notes": "",
            "parent_ids": ["main_thm"],
            "child_ids": [],
        }
        state["theorem_frontier"]["edges"].append(
            {
                "edge_id": "main_thm|reduction|lemma_b",
                "parent": "main_thm",
                "child": "lemma_b",
                "edge_type": "reduction",
                "status": "open",
                "justification": "expand",
            }
        )
        payload = state["theorem_frontier"]
        supervisor.export_dag_frontier_cycle(
            config, state, before_ids, {"main_thm|reduction|lemma_a"}, payload,
            cycle=11, outcome="EXPANDED", reviewed_node_id="main_thm",
            worker_directive="Expand main_thm",
        )
        path = supervisor.dag_frontier_history_path(config)
        self.assertTrue(path.exists())
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["type"], "review")
        self.assertEqual(entry["outcome"], "EXPANDED")
        self.assertEqual(entry["cycle"], 11)
        self.assertIn("lemma_b", entry["nodes_added"])
        self.assertNotIn("main_thm", entry["nodes_added"])
        self.assertEqual(entry["node_statuses"]["main_thm"], "active")
        self.assertEqual(entry["worker_directive"], "Expand main_thm")
        self.assertIn("edges_added", entry)
        self.assertEqual(entry["edges_added"][0]["edge_id"], "main_thm|reduction|lemma_b")
        self.assertEqual(entry["edge_statuses"]["main_thm|reduction|lemma_a"], "active")

    def test_worker_directive_summary_extracts_review_prompt(self) -> None:
        state = {
            "last_review": {"next_prompt": "Close the bound lemma."},
        }
        self.assertIn("Close the bound lemma", supervisor.worker_directive_summary(state))

    def test_worker_directive_summary_empty_without_review(self) -> None:
        self.assertEqual(supervisor.worker_directive_summary({}), "")

    def test_dag_site_paths_use_lagent_dags(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        dag_root = supervisor.dag_root_dir(config)
        self.assertTrue(str(dag_root).endswith("lagent-dags"))
        self.assertNotIn("lagent-chats", str(dag_root))

    def test_install_dag_viewer_assets_creates_files(self) -> None:
        repo_path = self.make_repo()
        root = repo_path.parent / "dag-site"
        supervisor.install_dag_viewer_assets(root)
        self.assertTrue((root / "index.html").exists())
        self.assertTrue((root / "_assets" / "dag-browser.js").exists())
        self.assertTrue((root / "_assets" / "dag-browser.css").exists())
        self.assertTrue((root / "_assets" / "dag-layout-worker.js").exists())
        self.assertTrue((root / "_assets" / "viewer-version.json").exists())
        self.assertTrue((root / "repos.json").exists())

    def test_export_dag_meta_updates_manifest(self) -> None:
        repo_path = self.make_repo()
        config = self.make_config(repo_path)
        state = self._make_frontier_state()
        supervisor.ensure_dag_site(config)
        supervisor.export_dag_meta(config, state)
        meta_path = supervisor.dag_repo_meta_path(config)
        self.assertTrue(meta_path.exists())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(meta["frontier_summary"])
        self.assertEqual(meta["frontier_summary"]["total_nodes"], 2)
        manifest = json.loads(supervisor.dag_manifest_path(config).read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["repos"]), 1)
        self.assertEqual(manifest["repos"][0]["repo_name"], config.chat.repo_name)


if __name__ == "__main__":
    unittest.main()
