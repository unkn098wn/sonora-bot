import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import urllib.error

# Add parent directory to path
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot import (
    EmailService,
    ReportsManager,
    ADMIN_ID,
    parse_adm_st_callback,
    parse_adm_del_callback,
    parse_adm_view_callback,
    parse_adm_list_callback,
)


class TestEmailService(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp_dir.name, "email_config.json")
        self.email_svc = EmailService(config_file=self.config_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_demo_mode_when_unconfigured(self):
        self.email_svc.reset_config()
        self.assertFalse(self.email_svc.is_configured)
        ok, msg = self.email_svc._send_sync("test@example.com", "123456")
        self.assertTrue(ok)
        self.assertIn("123456", msg)
        self.assertIn("демо-режим", msg)

    def test_resend_api_success(self):
        self.email_svc.configure_resend("re_test_key_12345", "noreply@sonora.app")
        self.assertTrue(self.email_svc.is_resend_configured)
        self.assertTrue(self.email_svc.is_configured)

        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.read.return_value = json.dumps({"id": "msg_abc123"}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            ok, msg = self.email_svc._send_sync("recipient@domain.com", "654321")
            self.assertTrue(ok)
            self.assertIn("Resend API", msg)
            mock_urlopen.assert_called_once()
            req = mock_urlopen.call_args[0][0]
            self.assertEqual(req.get_header("Authorization"), "Bearer re_test_key_12345")
            data = json.loads(req.data.decode("utf-8"))
            self.assertEqual(data["to"], ["recipient@domain.com"])
            self.assertIn("654321", data["subject"])

    def test_resend_api_error_handling(self):
        self.email_svc.configure_resend("re_invalid_key", "onboarding@resend.dev")

        http_err = urllib.error.HTTPError(
            url="https://api.resend.com/emails",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=MagicMock(read=lambda: json.dumps({"message": "Invalid API key"}).encode("utf-8"))
        )

        with patch("urllib.request.urlopen", side_effect=http_err):
            ok, msg = self.email_svc._send_sync("user@domain.com", "999888")
            self.assertFalse(ok)
            self.assertIn("401", msg)
            self.assertIn("Invalid API key", msg)

    def test_smtp_auto_detect(self):
        svc = EmailService(config_file=os.path.join(self.tmp_dir.name, "smtp_test.json"))
        svc.user = "tester@gmail.com"
        svc.password = "secret"
        svc.host = ""
        svc.port = 0
        svc._auto_detect()
        self.assertEqual(svc.host, "smtp.gmail.com")
        self.assertEqual(svc.port, 587)

        svc.user = "admin@yandex.ru"
        svc.host = ""
        svc.port = 0
        svc._auto_detect()
        self.assertEqual(svc.host, "smtp.yandex.ru")
        self.assertEqual(svc.port, 465)

    def test_config_persistence(self):
        self.email_svc.configure_resend("re_saved_key_xyz", "support@sonora.app")
        # Load second instance pointing to same file
        loaded = EmailService(config_file=self.config_path)
        self.assertEqual(loaded.resend_api_key, "re_saved_key_xyz")
        self.assertEqual(loaded.resend_from, "support@sonora.app")
        self.assertTrue(loaded.is_resend_configured)


class TestReportsManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "reports.json")
        self.rm = ReportsManager(self.db_path)

    async def asyncTearDown(self):
        self.tmp_dir.cleanup()

    async def test_add_and_get_reports(self):
        r1 = await self.rm.add_report(
            category="bug",
            name="John Doe",
            contact="@johndoe",
            message="Player crashes on track switch",
            version="v2.6.0"
        )
        self.assertTrue(r1["ticket_id"].startswith("SON-"))
        self.assertEqual(r1["status"], "new")

        all_reports = await self.rm.get_all()
        self.assertEqual(len(all_reports), 1)

        # Persistence check
        with open(self.db_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        self.assertEqual(len(disk_data), 1)
        self.assertEqual(disk_data[0]["ticket_id"], r1["ticket_id"])

    async def test_update_status_and_case_insensitivity(self):
        r = await self.rm.add_report("bug", "Alice", "@alice", "Volume slider issue")
        tid = r["ticket_id"]

        # Update with lowercase and '#' prefix
        updated = await self.rm.update_status(f"#{tid.lower()}", "in_progress")
        self.assertTrue(updated)

        fetched = await self.rm.get_by_ticket(tid)
        self.assertEqual(fetched["status"], "in_progress")

        # Mark resolved
        await self.rm.update_status(tid, "resolved")
        resolved_list = await self.rm.get_all(status="resolved")
        self.assertEqual(len(resolved_list), 1)

    async def test_delete_report_and_persistence(self):
        r = await self.rm.add_report("bug", "Bob", "@bob", "UI glitch")
        tid = r["ticket_id"]

        deleted = await self.rm.delete_report(tid)
        self.assertTrue(deleted)

        self.assertIsNone(await self.rm.get_by_ticket(tid))
        with open(self.db_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        self.assertEqual(len(disk_data), 0)

    async def test_bulk_delete_resolved_and_all(self):
        r1 = await self.rm.add_report("bug", "User1", "@u1", "Issue 1")
        r2 = await self.rm.add_report("idea", "User2", "@u2", "Idea 1")
        r3 = await self.rm.add_report("question", "User3", "@u3", "Q 1")

        await self.rm.update_status(r1["ticket_id"], "resolved")
        await self.rm.update_status(r2["ticket_id"], "resolved")

        # Delete resolved (should delete r1 and r2)
        cnt = await self.rm.delete_resolved_reports()
        self.assertEqual(cnt, 2)
        remaining = await self.rm.get_all()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["ticket_id"], r3["ticket_id"])

        # Delete all
        all_cnt = await self.rm.delete_all_reports()
        self.assertEqual(all_cnt, 1)
        self.assertEqual(len(await self.rm.get_all()), 0)


class TestCallbackParsers(unittest.TestCase):
    def test_parse_adm_st_callback_variations(self):
        # Long format with in_progress (containing underscore)
        tid, st, page, flt = parse_adm_st_callback("adm_st_SON-1234_in_progress_0_all")
        self.assertEqual(tid, "SON-1234")
        self.assertEqual(st, "in_progress")
        self.assertEqual(page, 0)
        self.assertEqual(flt, "all")

        # Long format with resolved and specific page/filter
        tid, st, page, flt = parse_adm_st_callback("adm_st_SON-5678_resolved_2_in_progress")
        self.assertEqual(tid, "SON-5678")
        self.assertEqual(st, "resolved")
        self.assertEqual(page, 2)
        self.assertEqual(flt, "in_progress")

        # Short format (legacy notification alerts)
        tid, st, page, flt = parse_adm_st_callback("adm_st_SON-9999_resolved")
        self.assertEqual(tid, "SON-9999")
        self.assertEqual(st, "resolved")
        self.assertEqual(page, 0)
        self.assertEqual(flt, "all")

        tid, st, page, flt = parse_adm_st_callback("adm_st_SON-8888_in_progress")
        self.assertEqual(tid, "SON-8888")
        self.assertEqual(st, "in_progress")
        self.assertEqual(page, 0)
        self.assertEqual(flt, "all")

    def test_parse_adm_del_callback_variations(self):
        tid, page, flt = parse_adm_del_callback("adm_del_SON-1234_0_all")
        self.assertEqual(tid, "SON-1234")
        self.assertEqual(page, 0)
        self.assertEqual(flt, "all")

        tid, page, flt = parse_adm_del_callback("adm_del_SON-4321")
        self.assertEqual(tid, "SON-4321")
        self.assertEqual(page, 0)
        self.assertEqual(flt, "all")

    def test_parse_adm_list_callback(self):
        page, flt = parse_adm_list_callback("adm_list_0_all")
        self.assertEqual(page, 0)
        self.assertEqual(flt, "all")

        page, flt = parse_adm_list_callback("adm_list_3_in_progress")
        self.assertEqual(page, 3)
        self.assertEqual(flt, "in_progress")


class TestReportsManagerEdgeCases(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "reports_edge.json")
        self.rm = ReportsManager(self.db_path)

    async def asyncTearDown(self):
        self.tmp_dir.cleanup()

    async def test_missing_ticket_graceful_handling(self):
        # Ticket that doesn't exist (simulates Render reboot or deleted message)
        res_get = await self.rm.get_by_ticket("NON_EXISTENT_999")
        self.assertIsNone(res_get)

        res_update = await self.rm.update_status("NON_EXISTENT_999", "resolved")
        self.assertFalse(res_update)

        res_del = await self.rm.delete_report("NON_EXISTENT_999")
        self.assertFalse(res_del)

    async def test_pagination_and_bulk_operations(self):
        for i in range(10):
            await self.rm.add_report("bug", f"User {i}", f"@u{i}", f"Message {i}")

        all_r = await self.rm.get_all()
        self.assertEqual(len(all_r), 10)

        # Mark 4 as resolved
        for r in all_r[:4]:
            await self.rm.update_status(r["ticket_id"], "resolved")

        resolved_cnt = len(await self.rm.get_all(status="resolved"))
        self.assertEqual(resolved_cnt, 4)

        del_res = await self.rm.delete_resolved_reports()
        self.assertEqual(del_res, 4)

        remaining = await self.rm.get_all()
        self.assertEqual(len(remaining), 6)

        del_all = await self.rm.delete_all_reports()
        self.assertEqual(del_all, 6)
        self.assertEqual(len(await self.rm.get_all()), 0)


if __name__ == "__main__":
    unittest.main()
