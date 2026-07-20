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


if __name__ == "__main__":
    unittest.main(verbosity=2)
