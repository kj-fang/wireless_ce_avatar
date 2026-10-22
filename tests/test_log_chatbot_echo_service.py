import unittest
from unittest.mock import patch

from services import log_chatbot_echo_service as echo


class EchoChatServiceTests(unittest.TestCase):
    def test_question_uses_only_echo_user_and_assistant_turns(self):
        question = echo.build_echo_question(
            "What about the second option?",
            [
                {"role": "system", "content": "must not be included"},
                {"role": "user", "content": "Compare two options."},
                {"role": "assistant", "content": "Option A and option B."},
            ],
        )

        self.assertIn("User: Compare two options.", question)
        self.assertIn("Echo: Option A and option B.", question)
        self.assertTrue(question.endswith("User: What about the second option?"))
        self.assertNotIn("must not be included", question)

    def test_ask_echo_chat_returns_answer_from_independent_transport(self):
        captured = {}

        async def fake_ask(question, url, timeout, result_box):
            captured["question"] = question
            captured["url"] = url
            result_box["answer"] = "Echo answer"

        with patch.object(echo, "_ask_async", new=fake_ask):
            answer = echo.ask_echo_chat(
                "Follow up",
                history=[{"role": "user", "content": "Earlier question"}],
                url="https://example.invalid/mcp/sse",
            )

        self.assertEqual(answer, "Echo answer")
        self.assertIn("User: Earlier question", captured["question"])
        self.assertTrue(captured["question"].endswith("User: Follow up"))
        self.assertEqual(captured["url"], "https://example.invalid/mcp/sse")


if __name__ == "__main__":
    unittest.main()