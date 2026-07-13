import json
import unittest
from unittest.mock import patch

from trendradar.notification.senders import send_to_feishu_app


class MockResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FeishuAppSenderTests(unittest.TestCase):
    @patch("trendradar.notification.senders.requests.post")
    def test_sends_interactive_card_to_chat_id(self, post):
        post.side_effect = [
            MockResponse({"code": 0, "tenant_access_token": "test-token"}),
            MockResponse({"code": 0, "msg": "success"}),
        ]

        result = send_to_feishu_app(
            app_id="test-app",
            app_secret="test-secret",
            receive_id="oc_test",
            receive_id_type="chat_id",
            report_data={"stats": []},
            report_type="测试报告",
            split_content_func=lambda *args, **kwargs: ["测试内容"],
        )

        self.assertTrue(result)
        self.assertEqual(2, post.call_count)
        token_call, message_call = post.call_args_list
        self.assertTrue(token_call.args[0].endswith("/tenant_access_token/internal"))
        self.assertNotIn("test-secret", str(message_call))
        self.assertIn("receive_id_type=chat_id", message_call.args[0])
        self.assertEqual("oc_test", message_call.kwargs["json"]["receive_id"])
        self.assertEqual("interactive", message_call.kwargs["json"]["msg_type"])
        card = json.loads(message_call.kwargs["json"]["content"])
        self.assertEqual("2.0", card["schema"])
        self.assertIn("测试内容", card["body"]["elements"][0]["content"])
        self.assertEqual(
            "Bearer test-token",
            message_call.kwargs["headers"]["Authorization"],
        )

    @patch("trendradar.notification.senders.requests.post")
    def test_stops_when_token_request_fails(self, post):
        post.return_value = MockResponse({"code": 10003, "msg": "invalid app"})

        result = send_to_feishu_app(
            app_id="bad-app",
            app_secret="bad-secret",
            receive_id="oc_test",
            report_data={"stats": []},
            report_type="测试报告",
            split_content_func=lambda *args, **kwargs: ["不应发送"],
        )

        self.assertFalse(result)
        self.assertEqual(1, post.call_count)


if __name__ == "__main__":
    unittest.main()
