"""使用临时配置验证模式选择，禁止真实登录、查询或发送邮件。"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
import probe_session
from config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "config"
        self.root.mkdir()
        self.entry = self.root.parent / ".env"
        for suffix in ("dev", "prod"):
            (self.root / f"config_{suffix}.yaml").write_text(
                f'# preserved comment\nsession:\n  jsessionid: "{suffix}"\n',
                encoding="utf-8")

    def test_modes_ignore_environment(self):
        for env, suffix, opposite in (("dev", "dev", "prod"), ("prod", "prod", "dev")):
            with self.subTest(env=env), patch.dict(os.environ, {"ELECFEE_ENV": opposite}):
                self.entry.write_text(f"ELECFEE_ENV={env}\n", encoding="utf-8")
                mode, cfg, subs, data = load_config(self.root)
                self.assertEqual(mode, env)
                self.assertEqual(Path(cfg).name, f"config_{suffix}.yaml")
                self.assertEqual(Path(subs).name, f"subscriptions_{suffix}.yaml")
                self.assertEqual(data["session"]["jsessionid"], suffix)

    def test_invalid_entry(self):
        for text in ("", "env: dev", "ELECFEE_ENV=staging", "ELECFEE_ENV=", "ELECFEE_ENV=1", 'ELECFEE_ENV="dev', "ELECFEE_ENV=dev prod", "ELECFEE_ENV=dev\nELECFEE_ENV=prod"):
            with self.subTest(text=text):
                self.entry.write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(self.root)

    def test_dotenv_quotes_comments_and_export(self):
        for text in (
            "# mode\nELECFEE_ENV=prod # production\n",
            'ELECFEE_ENV = "prod"\n',
            "export ELECFEE_ENV='prod'\n",
        ):
            with self.subTest(text=text):
                self.entry.write_text(text, encoding="utf-8")
                self.assertEqual(load_config(self.root)[0], "prod")

    def test_no_fallback_to_process_environment_or_old_yaml(self):
        (self.root / "config.yaml").write_text("env: prod\n", encoding="utf-8")
        with patch.dict(os.environ, {"ELECFEE_ENV": "prod"}):
            with self.assertRaisesRegex(ConfigError, r"\.env"):
                load_config(self.root)

    def test_subscription_commands_read_selected_mode(self):
        for env, other in (("dev", "prod"), ("prod", "dev")):
            with self.subTest(env=env):
                self.entry.write_text(f"ELECFEE_ENV={env}\n", encoding="utf-8")
                (self.root / f"subscriptions_{env}.yaml").write_text(
                    f"C1-101:\n  - email: {env}@example.com\n    threshold: 10\n",
                    encoding="utf-8")
                # The other mode must never be loaded, even if its YAML is invalid.
                (self.root / f"subscriptions_{other}.yaml").write_text("[", encoding="utf-8")
                with patch.object(main, "CONFIG_DIR", str(self.root)), patch.object(
                    sys, "argv", ["main.py", "--list-subs"]
                ), patch.object(main, "logger") as logger:
                    self.assertEqual(main.main(), 0)
                self.assertIn(f"{env}@example.com", str(logger.info.call_args_list))
                self.assertNotIn(f"{other}@example.com", str(logger.info.call_args_list))

    def test_prod_does_not_fall_back_to_old_filename(self):
        self.entry.write_text("ELECFEE_ENV=prod\n", encoding="utf-8")
        (self.root / "config_prod.yaml").rename(self.root / "config_prods.yaml")
        with self.assertRaisesRegex(ConfigError, "config_prod.yaml"):
            load_config(self.root)

    def test_missing_files_and_invalid_business_config(self):
        with self.assertRaises(ConfigError):
            load_config(self.root)
        self.entry.write_text("ELECFEE_ENV=prod\n", encoding="utf-8")
        path = self.root / "config_prod.yaml"
        for text in ("", "[]", "session: ["):
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(self.root)
        path.unlink()
        with self.assertRaises(ConfigError):
            load_config(self.root)

    def test_login_dispatch_writes_only_selected_config(self):
        for env, suffix, other in (("dev", "dev", "prod"), ("prod", "prod", "dev")):
            with self.subTest(env=env):
                self.entry.write_text(f"ELECFEE_ENV={env}\n", encoding="utf-8")
                entry_before = self.entry.read_bytes()
                other_path = self.root / f"config_{other}.yaml"
                other_before = other_path.read_bytes()

                def fake_login(config):
                    self.assertEqual(config["session"]["jsessionid"], suffix)
                    main.set_config_jsessionid("new-session")
                    return 0

                with patch.object(main, "CONFIG_DIR", str(self.root)), patch.object(
                    sys, "argv", ["main.py", "--login"]
                ), patch.object(main, "do_login", side_effect=fake_login):
                    self.assertEqual(main.main(), 0)
                selected = (self.root / f"config_{suffix}.yaml").read_text()
                self.assertIn('jsessionid: "new-session"', selected)
                self.assertIn("# preserved comment", selected)
                self.assertEqual(self.entry.read_bytes(), entry_before)
                self.assertEqual(other_path.read_bytes(), other_before)
                # Restore the fixture for the next mode.
                (self.root / f"config_{suffix}.yaml").write_text(
                    f'# preserved comment\nsession:\n  jsessionid: "{suffix}"\n', encoding="utf-8")

    def test_entrypoints_reject_bad_mode_before_network(self):
        self.entry.write_text("ELECFEE_ENV=invalid\n", encoding="utf-8")
        for module in (main, probe_session):
            with self.subTest(module=module.__name__), patch.object(
                module, "CONFIG_DIR", str(self.root)
            ), patch.object(sys, "argv", [module.__name__]), patch.object(module, "ScutClient") as client:
                self.assertEqual(module.main(), 2)
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
