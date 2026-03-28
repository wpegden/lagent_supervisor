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


if __name__ == "__main__":
    unittest.main()
