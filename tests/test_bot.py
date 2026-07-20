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


if __name__ == "__main__":
    unittest.main(verbosity=2)
