"""bot.py 순수 로직 유닛테스트 — 텔레그램/claude 없이 도는 부분만.

실행:
    .venv/bin/python -m unittest discover -s tests
또는
    .venv/bin/python tests/test_bot.py
"""
import os
import sys
import unittest
from pathlib import Path

# repo 루트를 import 경로에 추가 (bot.py 임포트용)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402


class SplitMessageTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(bot.split_message(""), [])

    def test_short(self):
        self.assertEqual(bot.split_message("안녕"), ["안녕"])

    def test_exact_limit_stays_one(self):
        s = "a" * bot.TELEGRAM_MSG_LIMIT
        self.assertEqual(bot.split_message(s), [s])

    def test_over_limit_splits_and_reassembles(self):
        s = "a" * (bot.TELEGRAM_MSG_LIMIT + 1)
        chunks = bot.split_message(s)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), bot.TELEGRAM_MSG_LIMIT)
        self.assertEqual("".join(chunks), s)  # 나눠도 원문 보존


class ParseAllowedIdsTest(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get("ALLOWED_USER_IDS")

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("ALLOWED_USER_IDS", None)
        else:
            os.environ["ALLOWED_USER_IDS"] = self._orig

    def test_empty_is_empty_set(self):
        os.environ.pop("ALLOWED_USER_IDS", None)
        self.assertEqual(bot._parse_allowed_user_ids(), set())

    def test_single(self):
        os.environ["ALLOWED_USER_IDS"] = "123"
        self.assertEqual(bot._parse_allowed_user_ids(), {123})

    def test_multi_with_spaces(self):
        os.environ["ALLOWED_USER_IDS"] = " 1, 2 ,3 "
        self.assertEqual(bot._parse_allowed_user_ids(), {1, 2, 3})

    def test_invalid_raises_systemexit(self):
        # 조용히 무시하면 '빈 허용목록=전체허용'으로 오작동 → 반드시 종료해야 안전
        os.environ["ALLOWED_USER_IDS"] = "12,abc"
        with self.assertRaises(SystemExit):
            bot._parse_allowed_user_ids()


class EnvDefaultTest(unittest.TestCase):
    KEY = "X_TEST_ENV_DEFAULT"

    def tearDown(self):
        os.environ.pop(self.KEY, None)

    def test_unset_uses_fallback(self):
        os.environ.pop(self.KEY, None)
        self.assertEqual(bot._env_default(self.KEY, "fb"), "fb")

    def test_explicit_default_means_empty(self):
        os.environ[self.KEY] = "default"
        self.assertEqual(bot._env_default(self.KEY, "fb"), "")

    def test_korean_default_means_empty(self):
        os.environ[self.KEY] = "기본"
        self.assertEqual(bot._env_default(self.KEY, "fb"), "")

    def test_value_passthrough(self):
        os.environ[self.KEY] = "opus"
        self.assertEqual(bot._env_default(self.KEY, "fb"), "opus")

    def test_blank_falls_back(self):
        os.environ[self.KEY] = "   "
        self.assertEqual(bot._env_default(self.KEY, "fb"), "fb")


class DescribeToolTest(unittest.TestCase):
    def test_write_shows_basename(self):
        self.assertEqual(
            bot.describe_tool("Write", {"file_path": "/a/b/c.py"}), "📝 파일 작성: c.py"
        )

    def test_bash_shows_description(self):
        self.assertEqual(bot.describe_tool("Bash", {"description": "빌드"}), "⚡ 명령 실행: 빌드")

    def test_unknown_tool_generic(self):
        self.assertEqual(bot.describe_tool("Foo", {}), "🔧 Foo")


class ReplyContextTest(unittest.TestCase):
    def test_no_reply_unchanged(self):
        self.assertEqual(bot.build_prompt_with_reply("질문", None), "질문")

    def test_blank_reply_unchanged(self):
        self.assertEqual(bot.build_prompt_with_reply("질문", "   "), "질문")

    def test_reply_prepends_quote(self):
        out = bot.build_prompt_with_reply("이거 자세히", "이전 답변 내용")
        self.assertTrue(out.startswith("[사용자가 이 메시지에 답장함]"))
        self.assertIn("이전 답변 내용", out)
        self.assertIn("이거 자세히", out)

    def test_long_reply_truncated(self):
        long = "x" * (bot.REPLY_QUOTE_LIMIT + 500)
        out = bot.build_prompt_with_reply("q", long)
        self.assertIn("…(생략)", out)
        self.assertLess(len(out), len(long) + 100)  # 상한 근처로 잘림


class AccumulateUsageTest(unittest.TestCase):
    def test_sums_across_calls(self):
        acc = {}
        bot.accumulate_usage(acc, {
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
            "total_cost_usd": 0.01,
        })
        bot.accumulate_usage(acc, {"usage": {"input_tokens": 20, "output_tokens": 7}, "total_cost_usd": 0.02})
        self.assertEqual(acc["input"], 30)
        self.assertEqual(acc["output"], 12)
        self.assertEqual(acc["cache_read"], 3)
        self.assertEqual(acc["turns"], 2)
        self.assertAlmostEqual(acc["cost"], 0.03)

    def test_missing_usage_is_safe(self):
        acc = {}
        bot.accumulate_usage(acc, {})  # usage/cost 없어도 안 터지고 turns만 증가
        self.assertEqual(acc["turns"], 1)
        self.assertEqual(acc["input"], 0)
        self.assertAlmostEqual(acc["cost"], 0.0)


class SessionListFormatTest(unittest.TestCase):
    def test_empty_shows_default_as_new(self):
        out = bot.format_session_lines({"active": "기본", "names": {}})
        self.assertIn("기본", out)
        self.assertIn("새 대화", out)

    def test_active_marked_and_unused_flagged(self):
        entry = {"active": "결제", "names": {"기본": "sid-1", "결제": None}}
        out = bot.format_session_lines(entry)
        base_line = next(l for l in out.splitlines() if "기본" in l)
        pay_line = next(l for l in out.splitlines() if "결제" in l)
        self.assertTrue(pay_line.startswith("▶"))      # 활성 표시
        self.assertIn("새 대화", pay_line)              # 세션 없음
        self.assertFalse(base_line.startswith("▶"))     # 비활성
        self.assertNotIn("새 대화", base_line)          # 세션 있음


class LimitFormatTest(unittest.TestCase):
    def test_remaining_computed(self):
        line = bot.format_limit_line("5시간", {"utilization": 33.0, "resets_at": "2026-04-11T07:00:00+00:00"})
        self.assertIn("67% 남음", line)
        self.assertIn("사용 33%", line)

    def test_none_when_no_data(self):
        self.assertIsNone(bot.format_limit_line("주간", None))
        self.assertIsNone(bot.format_limit_line("주간", {"utilization": None}))

    def test_reset_converted_to_kst(self):
        # 07:00 UTC + 9h = 16:00 KST
        self.assertEqual(bot._fmt_reset("2026-04-11T07:00:00+00:00"), "04/11 16:00 KST")


class TranscriptExportTest(unittest.TestCase):
    def test_blocks_text_from_string(self):
        self.assertEqual(bot._blocks_text("안녕"), "안녕")

    def test_blocks_text_only_text_blocks(self):
        content = [
            {"type": "thinking", "text": "속으로"},
            {"type": "text", "text": "답1"},
            {"type": "tool_use", "name": "Bash"},
            {"type": "text", "text": "답2"},
        ]
        self.assertEqual(bot._blocks_text(content), "답1\n답2")

    def test_build_transcript_missing_file_returns_none(self):
        # 존재하지 않는 세션 id → None (안 터짐)
        self.assertIsNone(bot.build_transcript_md("no-such-session-xyz", bot.WORKDIR))


class ParseDurationTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(bot.parse_duration_seconds("30s"), 30)
        self.assertEqual(bot.parse_duration_seconds("10m"), 600)
        self.assertEqual(bot.parse_duration_seconds("2h"), 7200)
        self.assertEqual(bot.parse_duration_seconds("1d"), 86400)

    def test_bare_number_is_minutes(self):
        self.assertEqual(bot.parse_duration_seconds("45"), 45 * 60)

    def test_invalid(self):
        for bad in ("", "abc", "10x", "m10", None):
            self.assertIsNone(bot.parse_duration_seconds(bad))


class WiringSmokeTest(unittest.TestCase):
    """배선(핸들러 등록) 회귀 방지 — 앱을 오프라인으로 만들어 모든 명령이 붙었는지 확인."""

    def test_all_command_handlers_registered(self):
        from telegram.ext import CommandHandler

        app = bot.build_application("123456:DUMMYtokenDUMMYtokenDUMMYtoken1234")
        handlers = app.handlers[0]
        cmds = set()
        for h in handlers:
            if isinstance(h, CommandHandler):
                cmds |= set(getattr(h, "commands", []))
        expected = {
            "start", "help", "new", "model", "effort", "status", "usage",
            "session", "cd", "ls", "files", "export", "remind",
        }
        missing = expected - cmds
        self.assertEqual(missing, set(), f"핸들러 누락: {missing}")
        # COMMANDS 메뉴 목록과 실제 등록된 명령이 일치하는지도(참고)
        self.assertTrue({n for n, _ in bot.COMMANDS}.issubset(cmds))


class AtomicWriteTest(unittest.TestCase):
    def test_writes_and_leaves_no_tmp(self):
        import tempfile
        from pathlib import Path as _P

        p = _P(tempfile.mkdtemp()) / "state.json"
        bot._atomic_write(p, '{"a": 1}')
        self.assertEqual(p.read_text(), '{"a": 1}')
        self.assertFalse(p.with_name(p.name + ".tmp").exists())  # temp 파일 안 남음
        bot._atomic_write(p, '{"a": 2}')  # 덮어쓰기도 원자적
        self.assertEqual(p.read_text(), '{"a": 2}')


class PrettyModelTest(unittest.TestCase):
    def test_pinned_ids_get_version_label(self):
        self.assertEqual(bot.pretty_model("claude-opus-4-8"), "opus 4.8")
        self.assertEqual(bot.pretty_model("claude-opus-5"), "opus 5")
        self.assertEqual(bot.pretty_model("claude-sonnet-5"), "sonnet 5")
        self.assertEqual(bot.pretty_model("claude-haiku-4-5-20251001"), "haiku 4.5")
        self.assertEqual(bot.pretty_model("claude-fable-5"), "fable 5")

    def test_alias_and_empty_unchanged(self):
        self.assertEqual(bot.pretty_model("opus"), "opus")
        self.assertEqual(bot.pretty_model("sonnet"), "sonnet")
        self.assertEqual(bot.pretty_model(""), "기본(구독)")


class ResolveDisplayModelTest(unittest.TestCase):
    def setUp(self):
        self._m, self._l = dict(bot.chat_models), dict(bot.chat_last_model)
        bot.chat_models.clear()
        bot.chat_last_model.clear()

    def tearDown(self):
        bot.chat_models.clear()
        bot.chat_models.update(self._m)
        bot.chat_last_model.clear()
        bot.chat_last_model.update(self._l)

    def test_pinned_setting_shows_version(self):
        bot.chat_models[1] = "claude-opus-5"
        self.assertEqual(bot.resolve_display_model(1), "opus 5")

    def test_alias_uses_last_version_of_same_family(self):
        bot.chat_models[1] = "opus"
        bot.chat_last_model[1] = "claude-opus-4-8"
        self.assertEqual(bot.resolve_display_model(1), "opus 4.8")

    def test_alias_without_last_stays_alias(self):
        bot.chat_models[1] = "opus"
        self.assertEqual(bot.resolve_display_model(1), "opus")

    def test_alias_ignores_last_of_other_family(self):
        bot.chat_models[1] = "sonnet"
        bot.chat_last_model[1] = "claude-opus-4-8"
        self.assertEqual(bot.resolve_display_model(1), "sonnet")


class ThreadSessionTest(unittest.TestCase):
    """스레드별 세션 해석. 한 채팅에서 스레드 여러 개가 동시에 돌 때
    각 턴이 자기 스레드의 대화를 이어가는지가 여기 달려 있다."""

    def setUp(self):
        self._sessions = dict(bot.sessions)
        self._threads = dict(bot.chat_threads)
        bot.sessions.clear()
        bot.chat_threads.clear()
        # 디스크 쓰기는 막는다 (테스트가 실제 상태 파일을 건드리지 않게)
        self._save_s, self._save_t = bot.save_sessions, bot.save_threads
        bot.save_sessions = lambda *a, **k: None
        bot.save_threads = lambda *a, **k: None

    def tearDown(self):
        bot.save_sessions, bot.save_threads = self._save_s, self._save_t
        bot.sessions.clear(); bot.sessions.update(self._sessions)
        bot.chat_threads.clear(); bot.chat_threads.update(self._threads)

    def test_active_thread_defaults(self):
        self.assertEqual(bot.active_thread(1), bot.DEFAULT_THREAD)

    def test_initial_state_inherits_legacy_session(self):
        # 스레드 기록이 없던 시절의 sessions.json 값을 활성 스레드가 물려받는다
        bot.sessions["1"] = "old-session"
        self.assertEqual(bot.thread_session_id(1, bot.DEFAULT_THREAD), "old-session")

    def test_unknown_thread_has_no_session(self):
        bot.sessions["1"] = "old-session"
        self.assertIsNone(bot.thread_session_id(1, "다른스레드"))

    def test_set_and_read_back_per_thread(self):
        bot.chat_threads["1"] = {"active": "A", "names": {}}
        bot.set_thread_session(1, "A", "sid-a")
        bot.set_thread_session(1, "B", "sid-b")
        self.assertEqual(bot.thread_session_id(1, "A"), "sid-a")
        self.assertEqual(bot.thread_session_id(1, "B"), "sid-b")
        # 활성 스레드(A)만 sessions.json에 반영된다 (구버전·롤백 호환)
        self.assertEqual(bot.sessions["1"], "sid-a")

    def test_clearing_one_thread_keeps_others(self):
        bot.chat_threads["1"] = {"active": "A", "names": {}}
        bot.set_thread_session(1, "A", "sid-a")
        bot.set_thread_session(1, "B", "sid-b")
        bot.set_thread_session(1, "A", None)          # /new 가 하는 일
        self.assertIsNone(bot.thread_session_id(1, "A"))
        self.assertEqual(bot.thread_session_id(1, "B"), "sid-b")
        self.assertNotIn("1", bot.sessions)


class _FakeMsg:
    """route_thread 검증용 최소 메시지. 텔레그램 없이 라우팅 규칙만 본다."""

    def __init__(self, text=None, caption=None, reply_to=None, message_id=1):
        self.text = text
        self.caption = caption
        self.reply_to_message = reply_to
        self.message_id = message_id


class ThreadRoutingTest(unittest.TestCase):
    """채팅창은 하나인데 스레드가 여럿일 때, 이 메시지가 어디로 갈지 정하는 규칙."""

    def setUp(self):
        self._threads = dict(bot.chat_threads)
        self._msg = dict(bot.msg_thread)
        bot.chat_threads.clear()
        bot.msg_thread.clear()
        bot.chat_threads["1"] = {"active": "기본", "names": {"기본": "s0", "검색": "s1", "봇": "s2"}}

    def tearDown(self):
        bot.chat_threads.clear(); bot.chat_threads.update(self._threads)
        bot.msg_thread.clear(); bot.msg_thread.update(self._msg)

    def test_defaults_to_active_thread(self):
        self.assertEqual(bot.route_thread(1, _FakeMsg(text="안녕")), "기본")

    def test_prefix_routes_to_named_thread(self):
        self.assertEqual(bot.route_thread(1, _FakeMsg(text="@검색 진행 상황 알려줘")), "검색")

    def test_unknown_prefix_is_left_alone(self):
        # 실재하지 않는 이름이면 스레드 지정이 아니라 평범한 문장으로 본다
        self.assertEqual(bot.route_thread(1, _FakeMsg(text="@없는스레드 안녕")), "기본")

    def test_prefix_is_stripped_from_body(self):
        known = {"검색", "봇"}
        self.assertEqual(bot.parse_thread_prefix("@검색 진행 상황", known), ("검색", "진행 상황"))
        self.assertEqual(bot.parse_thread_prefix("@없음 진행 상황", known), (None, "@없음 진행 상황"))
        # @만 있고 내용이 없으면 스레드 지정으로 보지 않는다
        self.assertEqual(bot.parse_thread_prefix("@검색", known), (None, "@검색"))

    def test_reply_routes_to_that_threads_conversation(self):
        bot.msg_thread[100] = "봇"
        msg = _FakeMsg(text="이어서 해줘", reply_to=_FakeMsg(message_id=100))
        self.assertEqual(bot.route_thread(1, msg), "봇")

    def test_reply_to_unknown_message_falls_back_to_active(self):
        msg = _FakeMsg(text="이어서", reply_to=_FakeMsg(message_id=999))
        self.assertEqual(bot.route_thread(1, msg), "기본")

    def test_prefix_beats_reply(self):
        bot.msg_thread[100] = "봇"
        msg = _FakeMsg(text="@검색 이건 검색 쪽", reply_to=_FakeMsg(message_id=100))
        self.assertEqual(bot.route_thread(1, msg), "검색")

    def test_remember_thread_msg_is_bounded(self):
        for i in range(bot.MSG_THREAD_MAX + 50):
            bot.remember_thread_msg(_FakeMsg(message_id=i), "기본")
        self.assertLessEqual(len(bot.msg_thread), bot.MSG_THREAD_MAX)
        # 오래된 것부터 버리므로 최신 id는 남아 있다
        self.assertIn(bot.MSG_THREAD_MAX + 49, bot.msg_thread)


class SessionLinesRunningTest(unittest.TestCase):
    def test_running_thread_is_marked(self):
        entry = {"active": "기본", "names": {"기본": "s0", "검색": "s1"}}
        out = bot.format_session_lines(entry, running={"검색"})
        self.assertIn("▶ 기본", out)
        self.assertIn("검색 ⚙ 작업 중", out)

    def test_no_running_keeps_previous_format(self):
        entry = {"active": "기본", "names": {"기본": "s0", "검색": None}}
        out = bot.format_session_lines(entry)
        self.assertIn("검색 (새 대화)", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
