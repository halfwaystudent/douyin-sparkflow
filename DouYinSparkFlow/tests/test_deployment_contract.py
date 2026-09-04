import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "DouYinSparkFlow"


class DeploymentContractTests(unittest.TestCase):
    def test_github_workflow_is_at_repository_root(self):
        workflow = REPO_ROOT / ".github" / "workflows" / "schedule.yml"
        self.assertTrue(workflow.is_file())
        self.assertFalse((SOURCE_ROOT / ".github" / "workflows" / "schedule.yml").exists())
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("working-directory: DouYinSparkFlow", text)
        self.assertIn("SPARKFLOW_BROWSER_PROFILE_ROOT", text)
        self.assertIn("SPARKFLOW_MANUAL_RUN", text)
        self.assertIn('USER_DATA: "[]"', text)
        self.assertIn("run_task:", text)
        self.assertGreaterEqual(text.count("github.event_name == 'schedule' || inputs.run_task"), 2)
        self.assertIn("path: DouYinSparkFlow/logs/", text)

    def test_github_actions_are_pinned_to_commit_shas(self):
        import re

        workflow = (REPO_ROOT / ".github" / "workflows" / "schedule.yml").read_text(encoding="utf-8")
        uses_values = re.findall(r"^\s*-?\s*uses:\s*([^#\s]+)", workflow, flags=re.MULTILINE)
        self.assertTrue(uses_values)
        for value in uses_values:
            self.assertRegex(value, r"^[^@]+@[0-9a-f]{40}$")

    def test_runtime_config_is_not_tracked_as_the_template(self):
        self.assertTrue((SOURCE_ROOT / "config.example.json").is_file())
        self.assertIn("config.json", (SOURCE_ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_scheduler_uses_local_task_runner_and_migrates_legacy_commands(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        runner_path = SOURCE_ROOT / "scripts" / "run_scheduled_task.sh"
        runner = runner_path.read_text(encoding="utf-8")
        cron_runner = (SOURCE_ROOT / "scripts" / "cron_runner.py").read_text(encoding="utf-8")
        scheduler = compose.split("  scheduler:", 1)[1].split("\n  task:", 1)[0]

        self.assertTrue(runner_path.is_file())
        self.assertIn("python main.py --doTask", runner)
        self.assertIn("migrate_legacy_crontab", cron_runner)
        self.assertNotIn("/var/run/docker.sock:/var/run/docker.sock", scheduler)

    def test_compose_runtime_mounts_follow_least_privilege(self):
        text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        web = text.split("  web:", 1)[1].split("\n  login-desktop:", 1)[0]
        scheduler = text.split("  scheduler:", 1)[1].split("\n  task:", 1)[0]
        task = text.split("  task:", 1)[1]

        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", web)
        self.assertIn(".:/opt/douyin-sparkflow", web)
        self.assertNotIn("/var/run/docker.sock:/var/run/docker.sock", scheduler)
        self.assertNotIn(".:/opt/douyin-sparkflow", scheduler)
        self.assertNotIn("/var/run/docker.sock:/var/run/docker.sock", task)
        self.assertNotIn(".:/opt/douyin-sparkflow", task)
        for service in (scheduler, task):
            self.assertIn(
                "./state/browser-profiles:/opt/douyin-sparkflow/state/browser-profiles",
                service,
            )

    def test_cron_reader_accepts_windows_utf8_bom(self):
        import tempfile

        from scripts.cron_runner import read_crontab

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "root"
            path.write_text(
                "*/20 10-17 * * * cd /app && python main.py --doTask\n",
                encoding="utf-8-sig",
            )
            lines = read_crontab(path)

        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("*/20 "))

    def test_legacy_cron_command_migrates_without_touching_schedule(self):
        from scripts.cron_runner import migrate_legacy_line

        legacy = (
            "20 18 * * * /bin/bash -lc 'docker ps --format \"{{.Names}}\" | "
            "grep douyin-web; docker exec douyin-web sh -lc \"cd /app && "
            "env SPARKFLOW_MANUAL_RUN=1 SPARKFLOW_MANUAL_UNSENT_ONLY=1 "
            "python main.py --doTask\"' >> /var/log/douyin-sparkflow.log 2>&1"
        )
        migrated = migrate_legacy_line(legacy)

        self.assertTrue(migrated.startswith("20 18 * * * "))
        self.assertIn("run_scheduled_task.sh", migrated)
        self.assertIn("SPARKFLOW_MANUAL_UNSENT_ONLY=1", migrated)
        self.assertNotIn("docker ps", migrated)
        self.assertNotIn("docker exec", migrated)

    def test_build_proxy_does_not_leak_into_runtime_and_runtime_proxy_is_explicit(self):
        dockerfile = (SOURCE_ROOT / "Dockerfile.server").read_text(encoding="utf-8")
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("# Build proxies must never leak", dockerfile)
        self.assertIn("http_proxy=", dockerfile)
        self.assertIn("HTTP_PROXY: ${HTTP_PROXY_BUILD:-}", compose)
        self.assertIn("HTTPS_PROXY: ${HTTPS_PROXY_BUILD:-}", compose)
        self.assertIn("ALL_PROXY: ${ALL_PROXY_BUILD:-}", compose)
        for service in ("web", "scheduler", "task"):
            block = compose.split(f"  {service}:", 1)[1]
            block = block.split("\n  ", 1)[0]
            self.assertNotIn("HTTP_PROXY: http://proxy:7890", block)
            self.assertNotIn("http_proxy: http://proxy:7890", block)

    def test_sensitive_ports_bind_to_loopback_by_default(self):
        text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("${PROXY_BIND_ADDRESS:-127.0.0.1}:${PROXY_HTTP_PORT:-7890}:7890", text)
        self.assertIn(
            "${LOGIN_DESKTOP_BIND_ADDRESS:-127.0.0.1}:${LOGIN_DESKTOP_WEB_PORT:-8788}:6080",
            text,
        )

    def test_container_login_api_and_public_url_are_wired(self):
        text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("SPARKFLOW_LOGIN_DESKTOP_API_URL: http://login-desktop:18090", text)
        self.assertIn("SPARKFLOW_LOGIN_DESKTOP_PUBLIC_URL", text)
        self.assertIn("/login-desktop/proxy/vnc.html", text)
        self.assertIn("SPARKFLOW_LOGIN_DESKTOP_NOVNC_WS_URL", text)

    def test_installers_preserve_runtime_config_and_do_not_require_bash_on_windows(self):
        server = (REPO_ROOT / "deploy" / "install-server.sh").read_text(encoding="utf-8")
        windows = (REPO_ROOT / "deploy" / "install-local.ps1").read_text(encoding="utf-8")
        self.assertIn("runtime_config_backup", server)
        self.assertIn("Restored runtime config.json", server)
        self.assertIn("remove_legacy_host_cron", server)
        self.assertIn("host-crontab-", server)
        self.assertIn("docker ps --format", server)
        self.assertNotIn("bash ./refresh_proxy.sh", windows)
        self.assertIn("Initialize-ProxyConfig", windows)

    def test_playwright_base_image_argument_is_used(self):
        dockerfile = (SOURCE_ROOT / "Dockerfile.server").read_text(encoding="utf-8")
        self.assertTrue(dockerfile.startswith("ARG PLAYWRIGHT_BASE_IMAGE="))
        self.assertIn("FROM ${NODE_RUNTIME_IMAGE} AS node-runtime", dockerfile)
        self.assertIn("FROM ${PLAYWRIGHT_BASE_IMAGE}", dockerfile)
        self.assertIn("COPY --from=node-runtime /usr/local/bin/node", dockerfile)
        self.assertIn("docker.io", dockerfile)
        self.assertIn("node --version", dockerfile)
        self.assertNotIn("github.com/docker/compose", dockerfile)

    def test_docker_build_context_excludes_runtime_data(self):
        dockerignore = (SOURCE_ROOT / ".dockerignore").read_text(encoding="utf-8")
        for entry in ("logs/", "config.json", "usersData.json", "webui_settings.json"):
            self.assertIn(entry, dockerignore)

    def test_login_desktop_uses_direct_first_network_route(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        server = (SOURCE_ROOT / "login_desktop_server.py").read_text(encoding="utf-8")
        login_block = compose.split("  login-desktop:", 1)[1].split("  scheduler:", 1)[0]
        self.assertIn("LOGIN_DESKTOP_PROXY_MODE: ${LOGIN_DESKTOP_PROXY_MODE:-auto}", login_block)
        self.assertIn("LOGIN_DESKTOP_PROXY: ${LOGIN_DESKTOP_PROXY:-http://proxy:7890}", login_block)
        self.assertNotIn("HTTP_PROXY: http://proxy:7890", login_block)
        self.assertIn("LOGIN_DESKTOP_PROXY_MODE=auto", env_example)
        self.assertIn('candidates.append(("direct", None))', server)
        self.assertIn('candidates.append(("proxy", LOGIN_PROXY_SERVER))', server)
        self.assertIn('"--no-proxy-server"', server)
        self.assertIn('"/preflight"', server)
        self.assertIn('LOGIN_DESKTOP_PROXY_MODE: ${LOGIN_DESKTOP_PROXY_MODE:-auto}', login_block)
        dashboard = (SOURCE_ROOT / "webui" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('name="douyin_network_mode"', dashboard)
        self.assertIn('name="douyin_proxy_url"', dashboard)
        browser = (SOURCE_ROOT / "core" / "browser.py").read_text(encoding="utf-8")
        self.assertIn("--no-proxy-server", browser)
        self.assertIn("SPARKFLOW_DOUYIN_NETWORK_MODE", browser)

    def test_login_desktop_resource_controls_are_configured(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        start_script = (SOURCE_ROOT / "scripts" / "start_login_desktop.sh").read_text(encoding="utf-8")
        server = (SOURCE_ROOT / "login_desktop_server.py").read_text(encoding="utf-8")
        self.assertIn("cpus: ${LOGIN_DESKTOP_CPUS:-0.8}", compose)
        self.assertIn("mem_limit: ${LOGIN_DESKTOP_MEMORY_LIMIT:-1200m}", compose)
        self.assertIn("pids_limit: ${LOGIN_DESKTOP_PIDS_LIMIT:-256}", compose)
        self.assertIn("-nap -wait 50 -defer 50", start_script)
        self.assertIn("--window-size=1600,1000", server)
        self.assertIn("start_idle_monitor", server)
        self.assertIn("schedule_stop_after_export", server)
        self.assertIn('reduced_motion="reduce"', server)
        self.assertIn('"/creator-micro/" in page.url', server)
        self.assertIn('"qr_ready": qr_ready', server)

    def test_login_desktop_uses_fastapi_lifespan(self):
        server = (SOURCE_ROOT / "login_desktop_server.py").read_text(encoding="utf-8")
        self.assertIn("lifespan=lifespan", server)
        self.assertNotIn("@app.on_event", server)

    def test_login_desktop_exposes_cropped_qr_endpoint(self):
        server = (SOURCE_ROOT / "login_desktop_server.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/qr")', server)
        self.assertIn('@app.post("/refresh-qr")', server)
        self.assertIn("qr_refresh=", server)
        self.assertIn('img[class*="qrcode"]', server)
        self.assertIn('qrcode_expired', server)
        self.assertIn('Cache-Control": "no-store, max-age=0', server)

    def test_legacy_unused_entrypoints_are_removed(self):
        self.assertFalse((SOURCE_ROOT / "webui" / "login_sessions.py").exists())
        self.assertFalse((SOURCE_ROOT / "relogin_worker.py").exists())
        self.assertFalse((SOURCE_ROOT / "docker-compose.example.yml").exists())


if __name__ == "__main__":
    unittest.main()
