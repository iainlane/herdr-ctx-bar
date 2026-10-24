import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "bin" / "ctx_bar.py"
SPEC = importlib.util.spec_from_file_location("ctx_bar", MODULE_PATH)
assert SPEC and SPEC.loader
ctx_bar = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ctx_bar
with mock.patch.object(sys, "path", [str(MODULE_PATH.parent), *sys.path]):
    SPEC.loader.exec_module(ctx_bar)


class SocketTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.socket_path = str(Path(directory.name) / "herdr.sock")


class RenderingTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(ctx_bar.color_for_percent(49.99), "ctx_green")
        self.assertEqual(ctx_bar.color_for_percent(50), "ctx_yellow")
        self.assertEqual(ctx_bar.color_for_percent(79.99), "ctx_yellow")
        self.assertEqual(ctx_bar.color_for_percent(80), "ctx_red")

    def test_bar_fills_requested_width(self):
        bar = ctx_bar.render_bar(50, 20)
        self.assertEqual(len(bar), 20)
        self.assertEqual(bar, "━" * 10 + "─" * 10)

    def test_percent_clamps(self):
        self.assertEqual(ctx_bar.usage_percent(250, 200), 100)
        self.assertIsNone(ctx_bar.usage_percent(10, 0))


class ConfigTests(unittest.TestCase):
    def test_installs_section_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = '[ui]\nagent_panel_sort = "spaces"\n'
            path.write_text(original)
            status, backup = ctx_bar.install_sidebar_config(path)
            self.assertEqual(status, "installed")
            self.assertIsNotNone(backup)
            self.assertEqual(backup.read_text(), original)
            self.assertIn("[ui.sidebar.agents]", path.read_text())
            self.assertIn('token = "$ctx_red"', path.read_text())

    def test_refuses_to_overwrite_custom_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = '[ui.sidebar.agents]\nrows = [["agent"]]\n'
            path.write_text(original)
            status, backup = ctx_bar.install_sidebar_config(path)
            self.assertEqual(status, "custom-layout")
            self.assertIsNone(backup)
            self.assertEqual(path.read_text(), original)

    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            first, _ = ctx_bar.install_sidebar_config(path)
            content = path.read_text()
            second, backup = ctx_bar.install_sidebar_config(path)
            self.assertEqual((first, second), ("installed", "already-installed"))
            self.assertIsNone(backup)
            self.assertEqual(path.read_text(), content)


class DemoOverrideTests(SocketTestCase):
    def test_demo_override_round_trip_and_clear(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(ctx_bar, "state_dir", return_value=Path(directory)),
        ):
            ctx_bar.update_demo_override("w1:p1", 95, now=1000)
            self.assertEqual(ctx_bar.read_demo_overrides(now=1001), {"w1:p1": 95})
            ctx_bar.update_demo_override("w1:p1", None, now=1002)
            self.assertEqual(ctx_bar.read_demo_overrides(now=1003), {})

    def test_demo_override_expires(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(ctx_bar, "state_dir", return_value=Path(directory)),
        ):
            ctx_bar.update_demo_override("w1:p1", 25, now=1000)
            self.assertEqual(
                ctx_bar.read_demo_overrides(now=1000 + ctx_bar.DEMO_SECONDS + 1),
                {},
            )

    def test_watcher_prefers_demo_over_collected_usage(self):
        watcher = ctx_bar.Watcher(self.socket_path)
        pane = {"pane_id": "w1:p1", "agent": "codex"}
        with (
            mock.patch.object(
                watcher,
                "refresh_panes",
                side_effect=lambda: setattr(watcher, "panes", {"w1:p1": pane}),
            ),
            mock.patch.object(watcher, "refresh_width"),
            mock.patch.object(
                ctx_bar, "read_demo_overrides", return_value={"w1:p1": 95}
            ),
            mock.patch.object(ctx_bar, "collect_percent") as collect,
            mock.patch.object(watcher, "publish") as publish,
        ):
            watcher.refresh_usage()
        collect.assert_not_called()
        publish.assert_called_once_with("w1:p1", 95)


class CollectorTests(unittest.TestCase):
    def test_codex_uses_latest_turn_not_cumulative(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 35,
                                    "total_tokens": 40,
                                },
                                "total_token_usage": {"total_tokens": 9999},
                                "model_context_window": 200,
                            },
                        },
                    }
                )
                + "\n"
            )
            with mock.patch.object(
                ctx_bar, "resolve_codex_path", return_value=str(path)
            ):
                self.assertEqual(ctx_bar.collect_codex({"agent": "codex"}), (40, 200))

    def test_codex_skips_malformed_session_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            valid = {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 40},
                        "model_context_window": 200,
                    },
                },
            }
            path.write_text(json.dumps(valid) + "\n{not json}\n")
            with mock.patch.object(
                ctx_bar, "resolve_codex_path", return_value=str(path)
            ):
                self.assertEqual(ctx_bar.collect_codex({"agent": "codex"}), (40, 200))

    def test_claude_compaction_supersedes_old_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            rows = [
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-4-5",
                        "usage": {
                            "input_tokens": 100,
                            "cache_read_input_tokens": 700000,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                },
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compactMetadata": {"postTokens": 12000},
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            with mock.patch.object(
                ctx_bar, "resolve_claude_path", return_value=str(path)
            ):
                pane = {
                    "agent": "claude",
                    "agent_session": {"kind": "id", "value": "x"},
                }
                self.assertEqual(ctx_bar.collect_claude(pane), (12000, 1_000_000))

    def test_pi_reads_context_snapshot_and_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_dir = root / "sessions" / "project"
            session_dir.mkdir(parents=True)
            session = session_dir / "session.jsonl"
            session.write_text(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "provider": "openai",
                            "model": "gpt-test",
                            "stopReason": "stop",
                            "contextSnapshot": {"promptTokens": 800},
                            "usage": {"totalTokens": 50},
                        },
                    }
                )
                + "\n"
            )
            (root / "models-store.json").write_text(
                json.dumps(
                    {"openai": {"models": [{"id": "gpt-test", "contextWindow": 1000}]}}
                )
            )
            pane = {
                "agent": "pi",
                "agent_session": {"kind": "path", "value": str(session)},
            }
            self.assertEqual(ctx_bar.collect_pi(pane), (800, 1000))

    def test_hermes_uses_active_message_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = sqlite3.connect(root / "state.db")
            db.executescript("""
                CREATE TABLE system_prompts(hash TEXT PRIMARY KEY, prompt TEXT);
                CREATE TABLE sessions(id TEXT PRIMARY KEY, model TEXT, model_config TEXT,
                  system_prompt TEXT, system_prompt_hash TEXT);
                CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, token_count INTEGER,
                  content TEXT, tool_calls TEXT, reasoning TEXT, reasoning_content TEXT, active INTEGER);
            """)
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                ("s1", "model", '{"context_length": 1000}', "abcd", None),
            )
            db.execute("INSERT INTO messages VALUES (1, 's1', 499, '', '', '', '', 1)")
            db.execute("INSERT INTO messages VALUES (2, 's1', 900, '', '', '', '', 0)")
            db.commit()
            db.close()
            with mock.patch.dict("os.environ", {"HERMES_HOME": directory}):
                pane = {
                    "agent": "hermes",
                    "agent_session": {"kind": "id", "value": "s1"},
                }
                self.assertEqual(ctx_bar.collect_hermes(pane), (500, 1000))

    def test_hermes_window_matches_hermes_fallback(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict("os.environ", {"HERMES_HOME": directory}),
        ):
            self.assertEqual(
                ctx_bar.hermes_context_window("unknown-model", {}), 256_000
            )


class DaemonLockTests(SocketTestCase):
    def test_losing_contender_does_not_truncate_active_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch.object(ctx_bar, "state_dir", return_value=state_dir):
                owner = ctx_bar.acquire_daemon_lock(self.socket_path)
                self.assertIsNotNone(owner)
                self.addCleanup(owner.close)

                lock_path = ctx_bar.lock_path(self.socket_path)
                expected_pid = str(ctx_bar.os.getpid())
                self.assertEqual(lock_path.read_text(), expected_pid)
                self.assertEqual(
                    ctx_bar.running_pid(self.socket_path), int(expected_pid)
                )

                contender = ctx_bar.acquire_daemon_lock(self.socket_path)
                self.assertIsNone(contender)
                self.assertEqual(lock_path.read_text(), expected_pid)

    def test_ensure_does_not_spawn_when_lock_is_held(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(ctx_bar, "state_dir", return_value=Path(directory)),
        ):
            owner = ctx_bar.acquire_daemon_lock(self.socket_path)
            self.assertIsNotNone(owner)
            self.addCleanup(owner.close)
            with mock.patch.object(ctx_bar.subprocess, "Popen") as popen:
                self.assertEqual(ctx_bar.ensure(self.socket_path, quiet=True), 0)
            popen.assert_not_called()


class SocketClientTests(SocketTestCase):
    def test_request_reassembles_fragmented_response(self):
        transport = mock.Mock()
        transport.recv.side_effect = [
            b'{"id":"response","result":',
            b'{"panes":[]}}\n',
        ]
        with mock.patch.object(ctx_bar.socket, "socket", return_value=transport):
            response = ctx_bar.HerdrClient(self.socket_path).request("pane.list", {})

        self.assertEqual(response, {"id": "response", "result": {"panes": []}})
        sent = json.loads(transport.sendall.call_args.args[0])
        self.assertEqual(sent["method"], "pane.list")
        self.assertEqual(sent["params"], {})
        transport.close.assert_called_once_with()

    def test_request_returns_none_for_malformed_response(self):
        transport = mock.Mock()
        transport.recv.return_value = b"not-json\n"
        with mock.patch.object(ctx_bar.socket, "socket", return_value=transport):
            response = ctx_bar.HerdrClient(self.socket_path).request("pane.list", {})

        self.assertIsNone(response)
        transport.close.assert_called_once_with()


class OpenCodeWatcherTests(SocketTestCase):
    def setUp(self):
        super().setUp()
        self.opencode = ctx_bar.opencode
        self.registration = self.opencode.Registration(
            Path("/test/service.json"),
            "http://127.0.0.1:1234",
            123,
            self.opencode.Endpoint.parse("http://127.0.0.1:1234"),
            "one",
            "2.0.32",
        )
        self.pane = {
            "pane_id": "w1:p1",
            "agent": "opencode",
            "agent_session": {"kind": "id", "value": "s1"},
        }
        self.panes = [self.pane]
        self.futures = []

        def submit(*args, **kwargs):
            future = Future()
            future.set_running_or_notify_cancel()
            self.futures.append(future)
            return future

        self.executor = mock.Mock()
        self.executor.submit.side_effect = submit
        patches = [
            mock.patch.object(
                ctx_bar, "ThreadPoolExecutor", return_value=self.executor
            ),
            mock.patch.object(ctx_bar, "read_demo_overrides", return_value={}),
            mock.patch.object(ctx_bar, "collect_percent", return_value=25),
            mock.patch.object(
                self.opencode, "service_file", return_value=self.registration.file
            ),
            mock.patch.object(
                self.opencode.Registration, "read", return_value=self.registration
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.watcher = ctx_bar.Watcher(self.socket_path)
        self.addCleanup(self.watcher.close_opencode)
        self.watcher.pane_list = mock.Mock(side_effect=lambda: list(self.panes))
        self.watcher.refresh_width = mock.Mock(return_value=False)
        self.watcher.publish = mock.Mock()

    def test_coalesces_requests_and_leaves_other_agents_synchronous(self):
        self.panes.append({"pane_id": "w1:p2", "agent": "codex"})
        with mock.patch.object(ctx_bar, "collect_percent", return_value=25) as collect:
            self.watcher.refresh_usage()
            self.watcher.refresh_usage()
        self.assertEqual(len(self.futures), 1)
        self.assertEqual(collect.call_args_list, [mock.call(self.panes[1])] * 2)
        self.futures[0].set_result(
            self.opencode.Result((50, 100), self.opencode.Reason.AVAILABLE)
        )
        self.watcher.refresh_usage()
        self.watcher.publish.assert_any_call("w1:p1", 50, ttl_ms=mock.ANY)

    def test_rejects_old_result_after_session_switch_and_return(self):
        self.watcher.refresh_usage()
        self.pane["agent_session"]["value"] = "s2"
        self.watcher.refresh_usage()
        self.pane["agent_session"]["value"] = "s1"
        self.watcher.refresh_usage()
        self.watcher.publish.reset_mock()
        self.futures[0].set_result(
            self.opencode.Result((95, 100), self.opencode.Reason.AVAILABLE)
        )
        self.watcher.refresh_usage()
        self.assertFalse(
            any(
                call.args == ("w1:p1", 95)
                for call in self.watcher.publish.call_args_list
            )
        )
        self.assertEqual(len(self.futures), 3)

    def test_rejects_replaced_service_and_worker_failure(self):
        self.watcher.refresh_usage()
        replacement = self.opencode.Registration(
            self.registration.file,
            self.registration.url,
            self.registration.pid,
            self.registration.endpoint,
            "two",
            self.registration.version,
        )
        with mock.patch.object(
            self.opencode.Registration, "read", return_value=replacement
        ):
            self.futures[0].set_result(
                self.opencode.Result((95, 100), self.opencode.Reason.AVAILABLE)
            )
            self.watcher.publish.reset_mock()
            self.watcher.refresh_usage()
            self.assertNotIn(
                mock.call("w1:p1", 95), self.watcher.publish.call_args_list
            )
            self.futures[-1].set_exception(OSError("private transport details"))
            self.watcher.refresh_usage()
        self.assertNotIn("w1:p1", self.watcher.percentages)

    def test_preview_prevents_collection_and_late_publication(self):
        with mock.patch.object(
            ctx_bar, "read_demo_overrides", return_value={"w1:p1": 95}
        ):
            self.watcher.refresh_usage()
        self.assertEqual(self.futures, [])
        self.watcher.publish.assert_called_with("w1:p1", 95)
        self.watcher.refresh_usage()
        self.futures[0].set_result(
            self.opencode.Result((50, 100), self.opencode.Reason.AVAILABLE)
        )
        with mock.patch.object(
            ctx_bar, "read_demo_overrides", return_value={"w1:p1": 95}
        ):
            self.watcher.refresh_usage()
        self.watcher.publish.assert_called_with("w1:p1", 95)

    def test_samples_expire_and_one_shot_aborts_unfinished_io(self):
        self.watcher.refresh_usage()
        self.futures[0].set_result(
            self.opencode.Result((50, 100), self.opencode.Reason.AVAILABLE)
        )
        self.watcher.refresh_usage()
        with mock.patch.object(
            ctx_bar.time, "monotonic", return_value=ctx_bar.time.monotonic() + 21
        ):
            self.watcher.refresh_usage()
        self.assertNotIn("w1:p1", self.watcher.percentages)
        self.assertFalse(self.watcher.refresh_once(timeout=0.03))
        self.executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

    def test_same_session_in_two_panes_uses_one_job(self):
        self.panes.append(dict(self.pane, pane_id="w1:p2"))
        self.watcher.refresh_usage()
        self.assertEqual(len(self.futures), 1)
        self.futures[0].set_result(
            self.opencode.Result((50, 100), self.opencode.Reason.AVAILABLE)
        )
        self.watcher.refresh_usage()
        self.assertEqual(self.watcher.percentages, {"w1:p1": 50, "w1:p2": 50})

    def test_bounded_queue_and_completed_one_shot_samples(self):
        self.panes.extend(
            dict(
                self.pane,
                pane_id=f"w1:p{i}",
                agent_session={"kind": "id", "value": f"s{i}"},
            )
            for i in range(2, 6)
        )
        self.watcher.refresh_usage()
        self.assertEqual(
            (len(self.futures), len(self.watcher.opencode_pending)), (2, 3)
        )
        self.futures[0].set_result(
            self.opencode.Result((50, 100), self.opencode.Reason.AVAILABLE)
        )
        self.watcher.refresh_usage()
        self.assertEqual(
            (len(self.futures), len(self.watcher.opencode_pending)), (3, 2)
        )
        self.assertFalse(self.watcher.refresh_once(timeout=0.03))
        self.assertEqual(self.watcher.percentages, {"w1:p1": 50})

    def test_new_pane_joins_running_session_without_an_extra_job(self):
        self.watcher.refresh_usage()
        self.panes.append(dict(self.pane, pane_id="w1:p2"))
        self.watcher.refresh_usage()
        self.futures[0].set_result(
            self.opencode.Result((50, 100), self.opencode.Reason.AVAILABLE)
        )
        self.watcher.refresh_usage()
        self.assertEqual(
            (len(self.futures), self.watcher.percentages),
            (1, {"w1:p1": 50, "w1:p2": 50}),
        )

    def test_requires_a_nonempty_native_string_session_id(self):
        for reference in (
            {},
            {"kind": "path", "value": "s1"},
            {"kind": "id", "value": 123},
            {"kind": "id", "value": ""},
        ):
            with self.subTest(reference=reference):
                self.pane["agent_session"] = reference
                self.watcher.refresh_usage()
                self.assertEqual(self.futures, [])
                self.assertEqual(
                    self.watcher.opencode_states["w1:p1"]["reason"],
                    "session_unavailable",
                )

    def test_delayed_completion_is_not_treated_as_a_fresh_sample(self):
        self.watcher.refresh_usage()
        self.futures[0].set_result(
            self.opencode.Result((95, 100), self.opencode.Reason.AVAILABLE)
        )
        with mock.patch.object(
            ctx_bar.time, "monotonic", return_value=ctx_bar.time.monotonic() + 21
        ):
            self.watcher.refresh_usage()
        self.assertNotIn("w1:p1", self.watcher.percentages)
        self.assertEqual(
            self.watcher.opencode_states["w1:p1"]["reason"], "sample_expired"
        )

    def test_layout_rerender_does_not_wait_for_http(self):
        self.watcher.refresh_usage()
        self.watcher.percentages["w1:p2"] = 25
        self.watcher.rerender()
        self.watcher.publish.assert_any_call("w1:p2", 25)
        self.assertFalse(self.futures[0].done())

    def test_live_metadata_ttl_does_not_extend_on_rerender(self):
        result = self.opencode.Result((50, 100), self.opencode.Reason.AVAILABLE)
        self.watcher.opencode_samples["w1:p1"] = result, 100
        self.watcher.opencode_identities["w1:p1"] = "s1", 1
        self.watcher.publish = ctx_bar.Watcher.publish.__get__(self.watcher)
        self.watcher.client.request = mock.Mock()
        with (
            mock.patch.object(ctx_bar.time, "monotonic", side_effect=(105, 110)),
            mock.patch.object(ctx_bar.time, "time_ns", return_value=123),
        ):
            self.watcher.publish_opencode("w1:p1", result, {})
            self.watcher.rerender()
        metadata = {
            "pane_id": "w1:p1",
            "source": "herdr-ctx-bar",
            "seq": 123,
            "tokens": {
                "ctx_green": None,
                "ctx_yellow": ctx_bar.render_bar(50, self.watcher.bar_width),
                "ctx_red": None,
            },
        }
        self.assertEqual(
            self.watcher.client.request.call_args_list,
            [
                mock.call("pane.report_metadata", dict(metadata, ttl_ms=15000)),
                mock.call("pane.report_metadata", dict(metadata, ttl_ms=10000)),
            ],
        )

    def test_diagnostic_reason_age_does_not_reset_when_usage_changes(self):
        with mock.patch.object(ctx_bar.time, "time", side_effect=(100, 200)):
            self.watcher.record_opencode(
                "w1:p1",
                "s1",
                self.opencode.Result((50, 100), self.opencode.Reason.AVAILABLE),
                sampled=True,
            )
            self.watcher.record_opencode(
                "w1:p1",
                "s1",
                self.opencode.Result((60, 100), self.opencode.Reason.AVAILABLE),
                sampled=True,
            )
        self.assertEqual(
            self.watcher.opencode_states,
            {
                "w1:p1": {
                    "session_id": "s1",
                    "reason": "available",
                    "used": 60,
                    "context_window": 100,
                    "checked_at": 200,
                    "changed_at": 100,
                }
            },
        )

    def test_status_identifies_a_previous_daemon_snapshot(self):
        output = io.StringIO()
        with (
            mock.patch.object(
                ctx_bar, "state_dir", return_value=Path(self.socket_path).parent
            ),
            mock.patch.object(ctx_bar.time, "time", return_value=105),
            mock.patch.object(sys, "stdout", output),
        ):
            ctx_bar.opencode_status_path(self.socket_path).write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "pid": 1,
                        "panes": {"w1:p1": {"reason": "available", "checked_at": 100}},
                    }
                )
            )
            ctx_bar.print_opencode_status(self.socket_path, current_pid=2)
        self.assertEqual(
            output.getvalue(), "OpenCode w1:p1: available (checked 5s ago, stale)\n"
        )

    def test_latest_diagnostics_use_socket_scoped_atomic_state(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(ctx_bar, "state_dir", return_value=Path(directory)),
        ):
            self.watcher.opencode_daemon = True
            self.watcher.refresh_usage()
            self.futures[0].set_result(
                self.opencode.Result(reason=self.opencode.Reason.AUTHENTICATION)
            )
            self.watcher.refresh_usage()
            state = json.loads(
                ctx_bar.opencode_status_path(self.watcher.socket_path).read_text()
            )
        self.assertEqual(state["panes"]["w1:p1"]["reason"], "authentication_failed")
        self.assertEqual(set(state["panes"]), {"w1:p1"})
        self.assertNotIn(self.registration.url, json.dumps(state))


class WatcherTests(SocketTestCase):
    def test_subscription_disconnect_exits_and_closes_socket(self):
        subscription = mock.Mock()
        subscription.recv.return_value = b""
        watcher = ctx_bar.Watcher(self.socket_path)
        with (
            mock.patch.object(watcher, "refresh_usage"),
            mock.patch.object(ctx_bar.socket, "socket", return_value=subscription),
        ):
            watcher.run()

        request = json.loads(subscription.sendall.call_args.args[0])
        self.assertEqual(request["method"], "events.subscribe")
        subscription.close.assert_called_once_with()
        self.assertIsNone(watcher.subscription)


if __name__ == "__main__":
    unittest.main()
