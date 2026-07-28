"""steering.py(작업 중 메시지 실시간 끼워넣기 큐) + hooks/steer_hook.py 유닛테스트.

봇·claude 없이 파일 큐와 훅 스크립트(서브프로세스)만 검증한다.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import steering  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "steer_hook.py"


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".steer" / "123.json"

    def tearDown(self):
        self.tmp.cleanup()

    def read_entries(self):
        raw = self.path.read_text()
        return json.loads(raw) if raw.strip() else []

    def test_take_pending_marks_injected_in_order(self):
        steering.enqueue(self.path, 1, "첫번째")
        steering.enqueue(self.path, 2, "두번째")
        self.assertEqual(steering.take_pending(self.path), ["첫번째", "두번째"])
        self.assertTrue(all(e["status"] == "injected" for e in self.read_entries()))
        # 두 번째 호출엔 pending이 없다 → Stop 차단 무한 반복 불가의 근거
        self.assertEqual(steering.take_pending(self.path), [])

    def test_take_pending_missing_or_empty(self):
        self.assertEqual(steering.take_pending(self.path), [])

    def test_claim_injected_removes_entry(self):
        steering.enqueue(self.path, 1, "메시지")
        steering.take_pending(self.path)  # 훅이 주입했다고 가정
        status, merged = steering.claim(self.path, 1)
        self.assertEqual(status, "injected")
        self.assertIsNone(merged)
        self.assertEqual(self.read_entries(), [])
        # 큐가 비면 0바이트로 남는다 ([ -s ] 가드 빠른 경로)
        self.assertEqual(self.path.stat().st_size, 0)

    def test_claim_pending_merges_all_pending(self):
        steering.enqueue(self.path, 1, "다른 대기자의 주입된 메시지")
        steering.take_pending(self.path)  # id 1은 injected로
        steering.enqueue(self.path, 2, "내 메시지")
        steering.enqueue(self.path, 3, "뒤에 온 메시지")
        status, merged = steering.claim(self.path, 2)
        self.assertEqual(status, "run")
        self.assertEqual(merged, "내 메시지\n\n뒤에 온 메시지")
        # pending은 전부 가져가고 injected(다른 대기자 소유)는 남긴다
        self.assertEqual([e["id"] for e in self.read_entries()], [1])

    def test_claim_gone_after_merge(self):
        steering.enqueue(self.path, 1, "먼저")
        steering.enqueue(self.path, 2, "나중")
        steering.claim(self.path, 1)  # 1번 대기자가 2번 것까지 합쳐 감
        status, merged = steering.claim(self.path, 2)
        self.assertEqual(status, "gone")
        self.assertIsNone(merged)

    def test_drain_leftovers(self):
        workdir = Path(self.tmp.name)
        steering.enqueue(steering.steer_file(workdir, 123), 1, "남은 메시지")
        steering.enqueue(steering.steer_file(workdir, -456), 2, "그룹 채팅")  # 음수 id
        (workdir / steering.STEER_DIRNAME / "junk.json").write_text("[]")  # 무시돼야 함
        left = dict(steering.drain_leftovers(workdir))
        self.assertEqual(left, {123: ["남은 메시지"], -456: ["그룹 채팅"]})
        # 비운 뒤엔 아무것도 안 나온다
        self.assertEqual(steering.drain_leftovers(workdir), [])


class FormatTest(unittest.TestCase):
    def test_injected_context_contains_texts(self):
        out = steering.format_injected_context(["테스트 돌려줘", "로그도 봐줘"])
        self.assertIn("- 테스트 돌려줘", out)
        self.assertIn("- 로그도 봐줘", out)
        self.assertIn("하던 작업을 버리지 마세요", out)

    def test_stop_reason_contains_texts(self):
        out = steering.format_stop_reason(["하나 더"])
        self.assertIn("- 하나 더", out)


class HookScriptTest(unittest.TestCase):
    """훅 스크립트를 실제 서브프로세스로 실행해 stdout JSON 규격을 검증."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "1.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, event, steer_file=None):
        env = dict(os.environ)
        env["TG_STEER_FILE"] = str(steer_file if steer_file is not None else self.path)
        r = subprocess.run(
            [sys.executable, str(HOOK), event],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_post_outputs_additional_context(self):
        steering.enqueue(self.path, 1, "이것도 반영해줘")
        out = json.loads(self.run_hook("post"))
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PostToolUse")
        self.assertIn("이것도 반영해줘", hso["additionalContext"])
        # 주입 후엔 pending이 남지 않는다
        self.assertEqual(steering.take_pending(self.path), [])

    def test_stop_blocks_with_reason(self):
        steering.enqueue(self.path, 1, "끝내기 전에 이것도")
        out = json.loads(self.run_hook("stop"))
        self.assertEqual(out["decision"], "block")
        self.assertIn("끝내기 전에 이것도", out["reason"])

    def test_silent_when_empty_or_missing(self):
        self.assertEqual(self.run_hook("post", steer_file=self.path), "")  # 파일 없음
        steering.enqueue(self.path, 1, "x")
        steering.take_pending(self.path)  # 전부 injected → pending 없음
        self.assertEqual(self.run_hook("post"), "")
        self.assertEqual(self.run_hook("stop"), "")


class EnsureHooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self.tmp.name) / ".claude" / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_merges_preserving_existing_and_idempotent(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({
            "includeCoAuthoredBy": False,
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "bash block-dangerous.sh"}]}]},
        }))
        for _ in range(2):  # 두 번 돌려도 중복 등록되지 않아야 한다
            self.assertTrue(steering.ensure_steer_hooks(self.settings, HOOK, sys.executable))
        s = json.loads(self.settings.read_text())
        self.assertFalse(s["includeCoAuthoredBy"])  # 기존 키 보존
        self.assertEqual(len(s["hooks"]["PreToolUse"]), 1)  # 기존 훅 보존
        self.assertEqual(len(s["hooks"]["PostToolUse"]), 1)  # 중복 없음
        self.assertEqual(len(s["hooks"]["Stop"]), 1)
        post_cmd = s["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        self.assertIn('[ -s "$TG_STEER_FILE" ]', post_cmd)  # 빈 큐 빠른 경로 가드
        self.assertIn("steer_hook.py post", post_cmd)
        self.assertEqual(s["hooks"]["PostToolUse"][0]["matcher"], "*")
        self.assertIn("steer_hook.py stop", s["hooks"]["Stop"][0]["hooks"][0]["command"])

    def test_creates_when_missing(self):
        self.assertTrue(steering.ensure_steer_hooks(self.settings, HOOK, sys.executable))
        s = json.loads(self.settings.read_text())
        self.assertIn("PostToolUse", s["hooks"])

    def test_refuses_broken_settings(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{broken json")
        self.assertFalse(steering.ensure_steer_hooks(self.settings, HOOK, sys.executable))
        self.assertEqual(self.settings.read_text(), "{broken json")  # 원본 안 건드림


if __name__ == "__main__":
    unittest.main()
