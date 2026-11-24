import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import urllib.error

# Add parent directory to path
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot import (
    EmailService,
    ReportsManager,
    UserManager,
    ADMIN_ID,
    parse_adm_st_callback,
    parse_adm_del_callback,
    parse_adm_view_callback,
    parse_adm_list_callback,
    get_guest_keyboard,
    get_user_keyboard,
    get_profile_keyboard,
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
        # Ensure SMTP is unconfigured so it doesn't attempt network fallback
        self.email_svc.host = ""
        self.email_svc.user = ""
        self.email_svc.password = ""

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

    def test_resend_fallback_to_smtp_on_failure(self):
        self.email_svc.configure_resend("re_invalid_key", "onboarding@resend.dev")
        self.email_svc.configure_smtp("smtp.gmail.com", 587, "user@gmail.com", "secret")

        http_err = urllib.error.HTTPError(
            url="https://api.resend.com/emails",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=MagicMock(read=lambda: json.dumps({"message": "Invalid API key"}).encode("utf-8"))
        )

        with patch("urllib.request.urlopen", side_effect=http_err), \
             patch.object(self.email_svc, "_send_smtp_sync", return_value=(True, "Sent via SMTP fallback")) as mock_smtp:
            ok, msg = self.email_svc._send_sync("user@domain.com", "999888")
            self.assertTrue(ok)
            self.assertIn("Sent via SMTP fallback", msg)
            mock_smtp.assert_called_once_with("user@domain.com", "999888")

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

    def test_save_config_updates_environ(self):
        self.email_svc.configure_resend("re_env_key_123", "bot@sonora.app")
        self.assertEqual(os.environ.get("RESEND_API_KEY"), "re_env_key_123")
        self.assertEqual(os.environ.get("RESEND_FROM"), "bot@sonora.app")

        self.email_svc.configure_smtp("smtp.gmail.com", 587, "user@gmail.com", "mypassword")
        self.assertEqual(os.environ.get("SMTP_HOST"), "smtp.gmail.com")
        self.assertEqual(os.environ.get("SMTP_PORT"), "587")
        self.assertEqual(os.environ.get("SMTP_USER"), "user@gmail.com")
        self.assertEqual(os.environ.get("SMTP_PASSWORD"), "mypassword")

    def test_google_app_password_with_spaces_logic(self):
        raw_pw_parts = ["abcd", "efgh", "ijkl", "mnop"]
        joined_no_space = "".join(raw_pw_parts)
        self.assertEqual(len(joined_no_space), 16)
        self.assertEqual(joined_no_space, "abcdefghijklmnop")
        self.email_svc.configure_smtp("", 0, "user@gmail.com", joined_no_space)
        self.assertEqual(self.email_svc.host, "smtp.gmail.com")
        self.assertEqual(self.email_svc.port, 587)
        self.assertEqual(self.email_svc.password, "abcdefghijklmnop")


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


class TestKeyboardsAndAdminMenu(unittest.TestCase):
    def test_guest_keyboard_visibility(self):
        admin_kb = get_guest_keyboard(user_id=ADMIN_ID)
        admin_texts = [btn.text for row in admin_kb.inline_keyboard for btn in row]
        self.assertIn("🐞 Баги и отчеты", admin_texts)
        self.assertIn("🔐 Панель администратора", admin_texts)

        guest_kb = get_guest_keyboard(user_id=12345678)
        guest_texts = [btn.text for row in guest_kb.inline_keyboard for btn in row]
        self.assertNotIn("🐞 Баги и отчеты", guest_texts)
        self.assertNotIn("🔐 Панель администратора", guest_texts)

    def test_user_keyboard_prominence(self):
        admin_kb = get_user_keyboard(user_id=ADMIN_ID)
        first_row = admin_kb.inline_keyboard[0]
        self.assertEqual(len(first_row), 1)
        self.assertEqual(first_row[0].text, "🐞 Баги и отчеты")
        self.assertEqual(first_row[0].callback_data, "adm_list_0_all")

        user_kb = get_user_keyboard(user_id=12345678)
        user_texts = [btn.text for row in user_kb.inline_keyboard for btn in row]
        self.assertNotIn("🐞 Баги и отчеты", user_texts)
        self.assertNotIn("🔐 Панель администратора", user_texts)

    def test_profile_keyboard_admin_visibility(self):
        admin_kb = get_profile_keyboard(user_id=ADMIN_ID)
        admin_texts = [btn.text for row in admin_kb.inline_keyboard for btn in row]
        self.assertIn("🐞 Баги и отчеты", admin_texts)
        self.assertIn("🔐 Панель администратора", admin_texts)

        user_kb = get_profile_keyboard(user_id=12345678)
        user_texts = [btn.text for row in user_kb.inline_keyboard for btn in row]
        self.assertNotIn("🐞 Баги и отчеты", user_texts)
        self.assertNotIn("🔐 Панель администратора", user_texts)


class TestUserManagerLinking(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.users_path = os.path.join(self.tmp_dir.name, "users.json")
        self.um = UserManager(self.users_path)

    async def asyncTearDown(self):
        self.tmp_dir.cleanup()

    async def test_link_email_to_existing_telegram_user(self):
        user = await self.um.upsert_user(960674720, username="wezreg", first_name="Admin")
        self.assertEqual(len(self.um._users), 1)

        # Link email using "tg_wezreg"
        linked1 = await self.um.link_email_to_user("tg_wezreg", "admin@sonora.app")
        self.assertEqual(linked1.get("email"), "admin@sonora.app")
        self.assertEqual(len(self.um._users), 1, "Should not create duplicate user")

        # Link email using "960674720"
        linked2 = await self.um.link_email_to_user("960674720", "admin_new@sonora.app")
        self.assertEqual(linked2.get("email"), "admin_new@sonora.app")
        self.assertEqual(len(self.um._users), 1, "Should update same record")


class TestLoginAlertAndEmailNotification(unittest.IsolatedAsyncioTestCase):
    async def test_login_alert_device_and_msk_time_formatting(self):
        import bot
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()

        original_bot = bot.active_bot_instance
        try:
            bot.active_bot_instance = mock_bot

            # Test Android device
            await bot.send_telegram_login_alert(
                960674720, "wezreg", device="Android (Смартфон)"
            )
            mock_bot.send_message.assert_called_once()
            call_args = mock_bot.send_message.call_args[0]
            text = call_args[1]
            self.assertIn("Sonora Music (Android)", text)
            self.assertIn("📱 Устройство: Android (Смартфон)", text)
            self.assertIn("(МСК)", text)

            # Test Windows device
            mock_bot.reset_mock()
            await bot.send_telegram_login_alert(
                960674720, "wezreg", device="Windows Desktop (x64)"
            )
            mock_bot.send_message.assert_called_once()
            win_text = mock_bot.send_message.call_args[0][1]
            self.assertIn("Sonora Music Desktop", win_text)
            self.assertIn("💻 Устройство: Windows Desktop (x64)", win_text)
            self.assertIn("(МСК)", win_text)
        finally:
            bot.active_bot_instance = original_bot


class TestDownloadsAndFeaturesCard(unittest.TestCase):
    def test_downloads_menu_content(self):
        import bot
        text = bot.format_downloads_menu()
        self.assertIn("v2.6.0", text)
        self.assertIn("Студия кастомизации", text)
        self.assertIn("Aura Glow", text)
        self.assertIn("Android", text)
        self.assertIn("Windows", text)
        self.assertIn("Setup.exe", text)
        self.assertIn("APK", text)

    def test_features_card_content(self):
        import bot
        text = bot.format_features_card()
        self.assertIn("v2.6.0", text)
        self.assertIn("Студия кастомизации", text)
        self.assertIn("новые темы", text.lower())
        self.assertIn("живые фоны", text.lower())
        self.assertIn("Aura Glow", text)
        self.assertIn("18 музыкальных платформ", text)

    def test_download_keyboard_links(self):
        import bot
        kb = bot.get_download_keyboard()
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        urls = [b.url for b in buttons if b.url]
        callbacks = [b.callback_data for b in buttons if b.callback_data]

        # Verify Android APK link
        android_urls = [u for u in urls if "Sonora-v2.6.0-Android.apk" in u]
        self.assertTrue(len(android_urls) >= 1)

        # Verify Windows Setup.exe link
        windows_urls = [u for u in urls if "Sonora-v2.6.0-Setup.exe" in u]
        self.assertTrue(len(windows_urls) >= 1)

        # Verify direct document sending button
        self.assertIn("send_setup_doc", callbacks)
        self.assertIn("send_apk_doc", callbacks)


if __name__ == "__main__":
    unittest.main()
