import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import warnings


FEATURE = Path(__file__).resolve().parents[1]
SOURCE = FEATURE / "src"
WORKSPACE = FEATURE.parents[1]
sys.path.insert(0, str(SOURCE))

import azure_artifacts_token as credentials
import setup
import start
import support

SHARED_CONFIG = json.loads((WORKSPACE / ".vscode/copilot-sandbox.json").read_text())
TENANT_ID = SHARED_CONFIG["tenantId"]
CLIENT_ID = SHARED_CONFIG["clientId"]


class SandboxFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="copilot sandbox tests ", dir=WORKSPACE / ".scratch")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        workspace = self.root / "mount" / WORKSPACE.name
        shutil.copytree(WORKSPACE / ".vscode", workspace / ".vscode")
        self.ctx = support.Context(
            workspace, "copilot-test-isolated-azure-artifacts",
            "fake-sbx", self.root / WORKSPACE.name / "azure-artifacts",
            azure_enabled=True,
        )


class FakeCommands:
    def __init__(self, ctx, *, existing=True):
        self.ctx = ctx
        self.calls = []
        self.names = {ctx.name} if existing else set()
        self.slots = {"azure-artifacts", "copilot", "github"}
        self.ssh = "false"
        self.udp = True
        self.status = "matching"
        self.fail_mode: str | None = None

    def __call__(self, command, **options):
        self.calls.append((command, options))
        if "--mode" in command:
            mode = command[command.index("--mode") + 1]
            if mode == self.fail_mode:
                raise support.CommandError(command[0], 1)
            return self.status if mode == "status" else ""
        args = command[1:]
        if args[:2] == ["ls", "--quiet"]:
            return "\n".join(sorted(self.names))
        if args[:2] == ["secret", "ls"]:
            service = args[args.index("--service") + 1]
            return service if service in self.slots else ""
        if args[:2] == ["settings", "get"]:
            return self.ssh if args[2] == "ssh.agentForwardingEnabled" else json.dumps({"value": {"enabled": self.udp}})
        if args[:1] == ["create"]:
            self.names.add(args[args.index("--name") + 1])
        if args[:2] == ["secret", "set"]:
            self.slots.add(args[2])
        return ""

    def sbx_calls(self):
        return [command[1:] for command, _ in self.calls if command[0] == self.ctx.sbx]


class ConfigurationTests(unittest.TestCase):
    def test_task_uses_uv_project_python_and_preserves_automatic_launch(self):
        task = json.loads((WORKSPACE / ".vscode/tasks.json").read_text())["tasks"][0]
        self.assertEqual(task["label"], "Copilot CLI")
        self.assertEqual(task["type"], "process")
        self.assertEqual(task["command"], "uv")
        self.assertNotIn("windows", task)
        self.assertEqual(task["args"][:6], [
            "run", "--project", "${workspaceFolder}", "python", "-B",
            "${workspaceFolder}/.vscode/copilot-sandbox/src/start.py",
        ])
        self.assertEqual(
            task["options"]["env"]["UV_PROJECT_ENVIRONMENT"],
            "${workspaceFolder}/.venv",
        )
        self.assertEqual(task["runOptions"]["runOn"], "folderOpen")
        self.assertEqual(task["options"]["cwd"], "${workspaceFolder}")
        project = (WORKSPACE / "pyproject.toml").read_text()
        self.assertIn('requires-python = ">=3.11"', project)
        self.assertIn("dependencies = []", project)

    def test_settings_remain_at_vscode_root(self):
        settings = WORKSPACE / ".vscode/copilot-settings.json"
        self.assertIsInstance(json.loads(settings.read_text()), dict)
        self.assertFalse((FEATURE / "copilot-settings.json").exists())
        self.assertIn("path.join('.vscode', 'copilot-settings.json')", start.APPLY_SETTINGS)

    def test_packaged_mixin_keeps_proxy_managed_domain_bound_bearer(self):
        import yaml

        spec = yaml.safe_load((FEATURE / "azure-artifacts-mixin/spec.yaml").read_text())
        self.assertEqual(spec["schemaVersion"], "2")
        self.assertEqual(spec["requires"]["agent"], "copilot")
        credential = spec["credentials"][0]
        self.assertEqual(credential["service"], "azure-artifacts")
        self.assertTrue(credential["required"])
        self.assertTrue(credential["apiKey"]["proxyManaged"])
        self.assertEqual(credential["apiKey"]["inject"], [{
            "domain": "pkgs.dev.azure.com", "scheme": "bearer",
        }])

    def test_shared_identity_is_not_an_executable_default_and_local_choice_is_ignored(self):
        self.assertEqual(set(SHARED_CONFIG), {"tenantId", "clientId", "appDisplayName"})
        for filename in ("setup.py", "support.py", "start.py", "azure_artifacts_token.py"):
            source = (SOURCE / filename).read_text()
            for value in SHARED_CONFIG.values():
                self.assertNotIn(value, source)
        self.assertIn(
            ".vscode/copilot-sandbox.local.json",
            (WORKSPACE / ".gitignore").read_text().splitlines(),
        )


class AzureChoiceTests(SandboxFixture):
    def setUp(self):
        super().setUp()
        self.ctx = replace(self.ctx, azure_enabled=None)
        self.ctx.local_configuration.unlink(missing_ok=True)

    def write_shared(self, value):
        self.ctx.configuration.write_text(json.dumps(value))

    def choose(self, answers):
        with mock.patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
            return support.resolve_azure(self.ctx)

    def test_missing_shared_config_creates_empty_placeholder_and_no_skips(self):
        self.ctx.configuration.unlink()
        with mock.patch("builtins.input", return_value="no"), contextlib.redirect_stdout(io.StringIO()) as output:
            ctx = support.resolve_azure(self.ctx)
        self.assertFalse(ctx.azure_enabled)
        self.assertEqual(json.loads(ctx.configuration.read_text()), {
            "tenantId": "", "clientId": "", "appDisplayName": "",
        })
        self.assertIn(str(ctx.configuration), output.getvalue())
        self.assertIn("tenantId, clientId", output.getvalue())
        self.assertIn("Copilot still runs", output.getvalue())
        self.assertIn("Cached packages may work", output.getvalue())
        self.assertEqual(json.loads(ctx.local_configuration.read_text()), {"azureArtifactsEnabled": False})

    def test_yes_rereads_file_after_user_edits_it(self):
        self.write_shared({"tenantId": "", "clientId": "", "appDisplayName": ""})
        def edit(_):
            self.write_shared(SHARED_CONFIG)
            return "yes"
        with mock.patch("builtins.input", side_effect=edit) as prompt, contextlib.redirect_stdout(io.StringIO()):
            ctx = support.resolve_azure(self.ctx)
        prompt.assert_called_once()
        self.assertTrue(ctx.azure_enabled)
        self.assertEqual(json.loads(ctx.local_configuration.read_text()), {"azureArtifactsEnabled": True})

    def test_yes_with_incomplete_ids_reprompts_and_names_all_missing_fields(self):
        self.write_shared({"tenantId": "", "clientId": "", "appDisplayName": "app"})
        with mock.patch("builtins.input", side_effect=["yes", "no"]) as prompt, contextlib.redirect_stdout(io.StringIO()) as output:
            ctx = support.resolve_azure(self.ctx)
        self.assertEqual(prompt.call_count, 2)
        self.assertIn("tenantId, clientId", output.getvalue())
        self.assertIn("still incomplete", output.getvalue())
        self.assertFalse(ctx.azure_enabled)

    def test_incomplete_saved_true_also_waits_for_edit_or_skip(self):
        support.save_azure_choice(self.ctx, True)
        self.write_shared({"tenantId": TENANT_ID, "clientId": "", "appDisplayName": ""})
        with mock.patch("builtins.input", return_value="no") as prompt, contextlib.redirect_stdout(io.StringIO()) as output:
            ctx = support.resolve_azure(self.ctx)
        prompt.assert_called_once()
        self.assertIn("Missing fields: clientId", output.getvalue())
        self.assertFalse(ctx.azure_enabled)

    def test_absent_choice_prompts_even_when_saved_secret_is_matching(self):
        fake = FakeCommands(self.ctx)
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "builtins.input", return_value="yes"
        ) as prompt, contextlib.redirect_stdout(io.StringIO()):
            ctx = support.resolve_azure(self.ctx)
            setup.configure_azure(ctx)
        prompt.assert_called_once()
        modes = [command[command.index("--mode") + 1] for command, _ in fake.calls if "--mode" in command]
        self.assertEqual(modes, ["status", "get-token"])

    def test_explicit_true_does_not_prompt_when_config_complete(self):
        support.save_azure_choice(self.ctx, True)
        with mock.patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()):
            ctx = support.resolve_azure(self.ctx)
        prompt.assert_not_called()
        self.assertTrue(ctx.azure_enabled)

    def test_explicit_false_skips_even_invalid_azure_configuration(self):
        support.save_azure_choice(self.ctx, False)
        self.ctx.configuration.write_text("invalid JSON")
        with mock.patch("support.azure_configuration") as configuration, mock.patch("builtins.input") as prompt:
            ctx = support.resolve_azure(self.ctx)
        configuration.assert_not_called()
        prompt.assert_not_called()
        self.assertFalse(ctx.azure_enabled)
        self.assertFalse(ctx.name.endswith("-azure-artifacts"))

    def test_missing_shared_file_is_initialized_even_when_azure_is_disabled(self):
        support.save_azure_choice(self.ctx, False)
        self.ctx.configuration.unlink()
        ctx = self.choose([])
        self.assertFalse(ctx.azure_enabled)
        self.assertEqual(json.loads(ctx.configuration.read_text()), {
            "tenantId": "", "clientId": "", "appDisplayName": "",
        })

    def test_empty_display_name_warns_and_uses_client_id(self):
        self.write_shared({**SHARED_CONFIG, "appDisplayName": ""})
        fake = FakeCommands(self.ctx)
        with mock.patch("builtins.input", return_value="yes"), mock.patch(
            "support.run", side_effect=fake
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            setup.configure_azure(support.resolve_azure(self.ctx))
        self.assertIn("Warning: appDisplayName", output.getvalue())
        self.assertIn(f"App: {CLIENT_ID}", output.getvalue())

    def test_bad_shared_json_types_and_nonempty_ids_fail_with_path(self):
        for value in ("{", "[]", '{"tenantId": true}', json.dumps({**SHARED_CONFIG, "tenantId": "not-a-guid"}),
                      json.dumps({**SHARED_CONFIG, "clientId": 3}), json.dumps({**SHARED_CONFIG, "appDisplayName": None})):
            self.ctx.configuration.write_text(value)
            with self.subTest(value=value), mock.patch("builtins.input") as prompt:
                with self.assertRaisesRegex(support.SetupError, str(self.ctx.configuration)):
                    support.resolve_azure(self.ctx)
                prompt.assert_not_called()

    def test_corrupt_local_choice_is_not_treated_as_absent(self):
        for value in ("{", "[]", "{}", '{"azureArtifactsEnabled": "false"}', '{"azureArtifactsEnabled": null}', '{"azureArtifactsEnabled": 0}'):
            self.ctx.local_configuration.write_text(value)
            with self.subTest(value=value), mock.patch("builtins.input") as prompt:
                with self.assertRaisesRegex(support.SetupError, str(self.ctx.local_configuration)):
                    support.resolve_azure(self.ctx)
                prompt.assert_not_called()

    def test_mode_suffix_policy_for_default_and_custom_names(self):
        for base in ("copilot-folder-isolated", "custom"):
            for name in (base, base + "-azure-artifacts", base + "-azure-artifacts-azure-artifacts"):
                for enabled in (True, False):
                    with self.subTest(name=name, enabled=enabled):
                        ctx = support.azure_mode(replace(self.ctx, name=name), enabled)
                        self.assertEqual(ctx.name, base + ("-azure-artifacts" if enabled else ""))

    def test_switching_local_boolean_selects_a_separate_name(self):
        support.save_azure_choice(self.ctx, True)
        enabled = self.choose([])
        support.save_azure_choice(self.ctx, False)
        disabled = self.choose([])
        self.assertNotEqual(enabled.name, disabled.name)
        self.assertEqual(disabled.name + "-azure-artifacts", enabled.name)

    def test_existing_enabled_custom_name_keeps_its_case(self):
        ctx = replace(self.ctx, name="Custom-AZURE-ARTIFACTS")
        self.assertEqual(support.azure_mode(ctx, True).name, ctx.name)
        self.assertEqual(support.azure_mode(ctx, False).name, "Custom")


class PathTests(SandboxFixture):
    def test_secret_service_key_uses_checkout_folder_name(self):
        for name in ("au-knowledgebase", "different-checkout"):
            home = self.root / name / "azure-artifacts"
            with self.subTest(name=name):
                self.assertEqual(
                    credentials.secret_service_name(home),
                    f"{name}/azure-artifacts",
                )

    def test_linux_xdg_paths(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }):
            self.assertEqual(
                credentials.data_directory("different-checkout"),
                self.root / "data/different-checkout/azure-artifacts",
            )
            self.assertEqual(credentials.binding_path(), self.root / "config/sbx/credentials.yaml")

    def test_windows_data_path_uses_checkout_folder_name(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.root / "Local"),
            "APPDATA": str(self.root / "Roaming"),
        }):
            self.assertEqual(
                credentials.data_directory("different-checkout"),
                self.root / "Local/different-checkout/azure-artifacts",
            )
            self.assertEqual(
                credentials.data_directory("au-knowledgebase"),
                self.root / "Local/au-knowledgebase/azure-artifacts",
            )
            self.assertEqual(credentials.binding_path(), self.root / "Roaming/sbx/credentials.yaml")

    def test_relative_xdg_path_is_rejected(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}):
            with self.assertRaisesRegex(credentials.CredentialError, "absolute"):
                credentials.data_directory("au-knowledgebase")

    def test_workspace_folder_name_must_be_one_path_component(self):
        for name in ("", ".", "..", "../outside", r"..\outside", "C:outside"):
            with self.subTest(name=name), self.assertRaisesRegex(
                credentials.CredentialError, "single path component"
            ):
                credentials.workspace_folder_name(name)

    def test_missing_windows_environment_is_explicit(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(credentials.CredentialError, "LOCALAPPDATA"):
                credentials.data_directory("au-knowledgebase")
            with self.assertRaisesRegex(credentials.CredentialError, "APPDATA"):
                credentials.binding_path()

    def test_unsupported_host_is_rejected(self):
        with mock.patch.object(sys, "platform", "darwin"):
            with self.assertRaisesRegex(credentials.CredentialError, "Windows and Linux"):
                credentials.data_directory("au-knowledgebase")

    def test_context_default_workspace_and_name(self):
        with mock.patch("support.shutil.which", return_value="/usr/bin/sbx"), mock.patch(
            "support.credentials.data_directory", return_value=self.ctx.home
        ) as data_directory:
            ctx = support.context(None, None)
        self.assertEqual(ctx.workspace, WORKSPACE)
        self.assertEqual(ctx.name, f"copilot-{WORKSPACE.name}-isolated")
        self.assertIsNone(ctx.azure_enabled)
        data_directory.assert_called_once_with(WORKSPACE.name)

    def test_context_uses_alternate_workspace_folder_name(self):
        workspace = self.root / "different-checkout"
        workspace.mkdir()
        with mock.patch("support.shutil.which", return_value="/usr/bin/sbx"), mock.patch(
            "support.credentials.data_directory", return_value=self.ctx.home
        ) as data_directory:
            ctx = support.context(str(workspace), None)
        self.assertEqual(ctx.workspace, workspace)
        data_directory.assert_called_once_with("different-checkout")

    def test_custom_name_suffix_is_not_duplicated(self):
        with mock.patch("support.shutil.which", return_value="sbx"), mock.patch(
            "support.credentials.data_directory", return_value=self.ctx.home
        ):
            for name in ("custom", "custom-azure-artifacts"):
                ctx = support.context(str(WORKSPACE), name)
                self.assertEqual(support.azure_mode(ctx, True).name, "custom-azure-artifacts")

    def test_missing_sbx_is_explicit(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch("support.shutil.which", return_value=None):
            with self.assertRaisesRegex(support.SetupError, "not installed"):
                support.context(str(WORKSPACE), None)

    def test_windows_sbx_fallback(self):
        candidate = self.root / "Local/DockerSandboxes/bin/sbx.exe"
        candidate.parent.mkdir(parents=True)
        candidate.touch()
        with mock.patch.object(sys, "platform", "win32"), mock.patch(
            "support.shutil.which", return_value=None
        ), mock.patch("support.credentials.data_directory", return_value=self.root / "Local/au-knowledgebase/azure-artifacts"):
            ctx = support.context(str(WORKSPACE), None)
            self.assertEqual(ctx.python.name, "python.exe")
        self.assertEqual(ctx.sbx, str(candidate))

    def test_host_paths_cannot_be_inside_or_equal_to_parent_mount(self):
        parent = self.root / "projects"
        for path in (parent, parent / "helper", parent / "repo/.venv"):
            with self.subTest(path=path), self.assertRaisesRegex(support.SetupError, "outside"):
                support.outside_mount(path, parent)
        support.outside_mount(self.root / "projects-other", parent)

    def test_symlink_into_mount_is_rejected(self):
        parent = self.root / "projects"
        parent.mkdir()
        link = self.root / "helper"
        try:
            link.symlink_to(parent, target_is_directory=True)
        except OSError:
            self.skipTest("Creating directory symlinks is not permitted on this host.")
        with self.assertRaisesRegex(support.SetupError, "outside"):
            support.outside_mount(link / "helper.py", parent)

    def test_validation_checks_nested_helper_paths(self):
        with mock.patch("support.outside_mount") as guard, mock.patch("support.run"):
            self.ctx.validate()
        checked = [call.args[0] for call in guard.call_args_list]
        for path in (self.ctx.home, self.ctx.environment, self.ctx.python, self.ctx.helper):
            self.assertIn(path, checked)


class CommandTests(SandboxFixture):
    def test_posix_command_round_trips_paths_with_spaces_and_quotes(self):
        arguments = ["/home/O'Brien/.local/python", "-I", "/host path/helper.py", "--mode", "get-token"]
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(shlex.split(support.shell_command(arguments)), arguments)

    @unittest.skipUnless(os.name == "posix", "Requires a POSIX command shell.")
    def test_native_posix_command_from_an_unrelated_working_directory(self):
        script = self.root / "helper's folder" / "token helper.py"
        script.parent.mkdir()
        script.write_text("import sys; print(sys.argv[1])", encoding="utf-8")
        command = support.shell_command([sys.executable, "-I", str(script), "literal argument"])
        result = subprocess.run(
            command, shell=True, cwd=self.root, text=True, capture_output=True, check=True,
        )
        self.assertEqual(result.stdout, "literal argument\n")

    def test_windows_command_uses_explicit_cmd_and_quotes_spaces(self):
        arguments = [r"C:\Users\Some User\python.exe", "-I", r"C:\Host Folder\helper.py", "--mode", "get-token"]
        with mock.patch.object(sys, "platform", "win32"):
            command = support.shell_command(arguments)
        self.assertTrue(command.startswith("cmd.exe /d /c call "))
        self.assertIn('"C:\\Users\\Some User\\python.exe"', command)
        self.assertIn('"C:\\Host Folder\\helper.py"', command)

    def test_shell_control_characters_fail_closed(self):
        with mock.patch.object(sys, "platform", "win32"):
            for character in '%!"&|<>^`$':
                with self.subTest(character=character), self.assertRaises(support.SetupError):
                    support.shell_command([f"C:\\user{character}\\python.exe"])
        for value in ("line\nbreak", "line\rbreak", "nul\0value"):
            with self.assertRaises(support.SetupError):
                support.shell_command([value])

    def test_run_uses_argv_and_stdin_not_a_shell(self):
        result = subprocess.CompletedProcess([], 0, "  output\n")
        with mock.patch("support.subprocess.run", return_value=result) as process:
            self.assertEqual(support.run(["program", "path with spaces"], capture=True, input_text="private-value"), "output")
        args, options = process.call_args
        self.assertEqual(args[0], ["program", "path with spaces"])
        self.assertNotIn("shell", options)
        self.assertEqual(options["input"], "private-value")

    def test_failed_command_preserves_exit_code_without_secret_in_error(self):
        with mock.patch("support.subprocess.run", return_value=subprocess.CompletedProcess([], 7, "private-value")):
            with self.assertRaises(support.CommandError) as result:
                support.run(["program"], capture=True, input_text="private-value")
        self.assertEqual(result.exception.code, 7)
        self.assertNotIn("private-value", str(result.exception))

    def test_helper_command_is_isolated_and_has_absolute_paths(self):
        command = self.ctx.helper_command("get-copilot-token", sandbox=True)
        self.assertEqual(command[:2], [str(self.ctx.python), "-I"])
        self.assertEqual(command[2], str(self.ctx.helper))
        self.assertEqual(
            command[command.index("--workspace-name") + 1], self.ctx.workspace.name
        )
        self.assertEqual(command[-2:], ["--sandbox-name", self.ctx.name])

    def test_hidden_input_does_not_fall_back_to_visible_input(self):
        def insecure_input(_):
            warnings.warn("echo enabled", support.getpass.GetPassWarning)
        with mock.patch("support.getpass.getpass", side_effect=insecure_input):
            with self.assertRaisesRegex(support.SetupError, "hidden input"):
                support.hidden_input("Secret: ")

    def test_cli_reports_errors_and_cancellation(self):
        for error, expected in (
            (support.CommandError("sbx", 9), 9),
            (support.CommandError("sbx", -15), 143),
            (support.SetupError("stopped"), 1),
            (EOFError(), 130),
            (KeyboardInterrupt(), 130),
        ):
            with self.subTest(error=error), contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(support.cli(mock.Mock(side_effect=error)), expected)
                self.assertTrue(output.getvalue())

    @unittest.skipUnless(os.name == "nt", "Requires a native Windows command shell.")
    def test_native_windows_command_with_spaces(self):
        script = self.root / "helper with spaces.py"
        script.write_text("print('command-ok')", encoding="utf-8")
        command = support.shell_command([sys.executable, "-I", str(script)])
        result = subprocess.run(command, shell=True, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), "command-ok")


class CredentialTests(SandboxFixture):
    def write_config(self, value=None):
        self.ctx.home.mkdir(parents=True, exist_ok=True)
        value = value if value is not None else {"tenantId": TENANT_ID, "clientId": CLIENT_ID}
        (self.ctx.home / "service-principal.json").write_text(json.dumps(value), encoding="utf-8")

    def test_windows_reads_existing_powershell_dpapi_hex_and_utf16(self):
        self.write_config()
        (self.ctx.home / "client-secret.dpapi").write_text("01020304", encoding="utf-8")
        with mock.patch.object(sys, "platform", "win32"), mock.patch(
            "azure_artifacts_token.dpapi", return_value="old-secret".encode("utf-16-le")
        ) as decrypt:
            self.assertEqual(credentials.read_secret(self.ctx.home, credentials.load_config(self.ctx.home)), "old-secret")
        decrypt.assert_called_once_with(b"\x01\x02\x03\x04", decrypt=True)

    def test_windows_writes_compatible_dpapi_format(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch(
            "azure_artifacts_token.dpapi", return_value=b"\x01\x02\xab"
        ) as encrypt:
            credentials.store_secret(self.ctx.home, TENANT_ID, CLIENT_ID, "new-secret")
        encrypt.assert_called_once_with("new-secret".encode("utf-16-le"), decrypt=False)
        self.assertEqual((self.ctx.home / "client-secret.dpapi").read_text(), "0102ab")
        self.assertNotIn("new-secret", (self.ctx.home / "service-principal.json").read_text())

    def test_windows_saved_status(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(credentials.saved_status(self.ctx.home, TENANT_ID, CLIENT_ID), "missing")
            self.write_config()
            self.assertEqual(credentials.saved_status(self.ctx.home, TENANT_ID, CLIENT_ID), "missing")
            (self.ctx.home / "client-secret.dpapi").touch()
            self.assertEqual(credentials.saved_status(self.ctx.home, TENANT_ID, CLIENT_ID), "matching")
            self.write_config({
                "tenantId": TENANT_ID.upper(),
                "clientId": CLIENT_ID.upper(),
            })
            self.assertEqual(credentials.saved_status(self.ctx.home, TENANT_ID, CLIENT_ID), "matching")
            self.write_config({"tenantId": CLIENT_ID, "clientId": TENANT_ID})
            self.assertEqual(credentials.saved_status(self.ctx.home, TENANT_ID, CLIENT_ID), "different")
            self.write_config([])
            self.assertEqual(credentials.saved_status(self.ctx.home, TENANT_ID, CLIENT_ID), "invalid")

    def test_invalid_config_and_empty_secret_are_rejected(self):
        for config in ([], None, {"tenantId": 1, "clientId": None}, {"tenantId": "../bad", "clientId": CLIENT_ID}):
            self.ctx.home.mkdir(parents=True, exist_ok=True)
            (self.ctx.home / "service-principal.json").write_text(json.dumps(config))
            with self.subTest(config=config), self.assertRaises(credentials.CredentialError):
                credentials.load_config(self.ctx.home)
        with self.assertRaisesRegex(credentials.CredentialError, "empty"):
            credentials.store_secret(self.ctx.home, TENANT_ID, CLIENT_ID, " \n")

    @unittest.skipUnless(sys.platform == "linux", "Requires Linux keyring dependencies.")
    def test_linux_uses_secret_service_without_plaintext_file(self):
        from keyring.errors import KeyringError # pyright: ignore[reportMissingImports]

        backend = mock.Mock()
        backend.get_password.return_value = "private-client-secret"
        with mock.patch.object(sys, "platform", "linux"), mock.patch(
            "azure_artifacts_token.secret_service", return_value=backend
        ):
            credentials.store_secret(self.ctx.home, TENANT_ID, CLIENT_ID, "private-client-secret")
            config = credentials.load_config(self.ctx.home)
            self.assertEqual(credentials.read_secret(self.ctx.home, config), "private-client-secret")
            backend.set_password.assert_called_once_with(
                credentials.secret_service_name(self.ctx.home),
                f"{TENANT_ID}/{CLIENT_ID}", "private-client-secret",
            )
            backend.get_password.side_effect = KeyringError("private-client-secret")
            with self.assertRaises(credentials.CredentialError) as error:
                credentials.read_secret(self.ctx.home, config)
            self.assertNotIn("private-client-secret", str(error.exception))
        self.assertEqual([file.name for file in self.ctx.home.iterdir()], ["service-principal.json"])

    @unittest.skipUnless(sys.platform == "linux", "Requires Linux keyring dependencies.")
    def test_linux_missing_or_locked_keyring_fails_without_default_backend(self):
        from keyring.errors import KeyringLocked # pyright: ignore[reportMissingImports]

        with mock.patch("keyring.backends.SecretService.Keyring") as constructor, mock.patch("keyring.get_keyring") as default:
            constructor.return_value.priority = 1
            constructor.return_value.get_preferred_collection.side_effect = KeyringLocked("locked")
            with self.assertRaisesRegex(credentials.CredentialError, "Plaintext"):
                credentials.secret_service()
            default.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux", "Requires Linux keyring dependencies.")
    def test_linux_missing_keyring_credential_is_explicit(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch(
            "azure_artifacts_token.secret_service"
        ) as service:
            service.return_value.get_password.return_value = None
            with self.assertRaisesRegex(credentials.CredentialError, "missing"):
                credentials.read_secure(self.ctx.home, "unused", "account")

    @unittest.skipUnless(sys.platform == "linux", "Requires Linux keyring dependencies.")
    def test_linux_store_failure_does_not_write_config_or_plaintext(self):
        from keyring.errors import KeyringError # pyright: ignore[reportMissingImports]

        backend = mock.Mock()
        backend.set_password.side_effect = KeyringError("private-client-secret")
        with mock.patch.object(sys, "platform", "linux"), mock.patch(
            "azure_artifacts_token.secret_service", return_value=backend
        ):
            with self.assertRaises(credentials.CredentialError) as error:
                credentials.store_secret(
                    self.ctx.home, TENANT_ID, CLIENT_ID, "private-client-secret"
                )
        self.assertNotIn("private-client-secret", str(error.exception))
        self.assertEqual(list(self.ctx.home.iterdir()), [])

    @unittest.skipUnless(sys.platform == "linux", "Requires Linux keyring dependencies.")
    def test_copilot_token_is_scoped_by_sandbox_in_secure_store(self):
        backend = mock.Mock()
        with mock.patch.object(sys, "platform", "linux"), mock.patch(
            "azure_artifacts_token.secret_service", return_value=backend
        ):
            credentials.store_copilot_token(self.ctx.home, "sandbox-a", "github_pat_private")
            credentials.store_copilot_token(self.ctx.home, "sandbox-b", "github_pat_other")
        self.assertEqual(backend.set_password.call_args_list[0].args[1], "copilot/sandbox-a")
        self.assertEqual(backend.set_password.call_args_list[1].args[1], "copilot/sandbox-b")
        self.assertNotEqual(credentials.copilot_filename("sandbox-a"), credentials.copilot_filename("sandbox-b"))
        self.assertEqual(list(self.ctx.home.iterdir()), [])
        with self.assertRaises(credentials.CredentialError):
            credentials.store_copilot_token(self.ctx.home, "sandbox-a", "existing-ghp-token")

    def test_windows_copilot_token_uses_dpapi_outside_config(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch(
            "azure_artifacts_token.dpapi", return_value=b"\xaa\xbb"
        ):
            credentials.store_copilot_token(self.ctx.home, "sandbox-a", "github_pat_private")
        path = self.ctx.home / credentials.copilot_filename("sandbox-a")
        self.assertEqual(path.read_text(), "aabb")
        self.assertEqual(list(self.ctx.home.iterdir()), [path])

    def test_binding_parser_accepts_yaml_forms_and_only_correct_domain(self):
        path = self.root / "credentials.yaml"
        for content, expected in (
            ("bindings:\n  azure-artifacts:\n    apiKey: [pkgs.dev.azure.com]\n", True),
            ("bindings:\n  azure-artifacts:\n    apiKey:\n      domains: ['pkgs.dev.azure.com']\n", True),
            ("bindings:\n  azure-artifacts:\n    apiKey:\n      domains:\n      - pkgs.dev.azure.com # approved\n", True),
            ("bindings:\n  azure-artifacts:\n    apiKey:\n      domains: [pkgs.dev.azure.com.evil]\n", False),
            ("bindings:\n  github:\n    apiKey: [pkgs.dev.azure.com]\n", False),
            ("bindings:\n  azure-artifacts:\n    apiKey:\n      domains: pkgs.dev.azure.com\n", False),
            ("[]", False),
            ("", False),
        ):
            with self.subTest(content=content):
                path.write_text(content)
                self.assertEqual(credentials.has_binding(path), expected)
        path.write_text("bindings: [")
        with self.assertRaisesRegex(credentials.CredentialError, "invalid"):
            credentials.has_binding(path)
        self.assertFalse(credentials.has_binding(self.root / "missing.yaml"))

    def test_token_request_uses_fixed_scope_form_encoding_and_timeout(self):
        self.write_config()
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"access_token":"short-lived-token"}')
        with mock.patch("azure_artifacts_token.read_secret", return_value="secret&plus+"), mock.patch(
            "azure_artifacts_token.urllib.request.urlopen", return_value=response
        ) as send:
            self.assertEqual(credentials.get_token(self.ctx.home, TENANT_ID, CLIENT_ID), "short-lived-token")
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token")
        values = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(values["client_secret"], ["secret&plus+"])
        self.assertEqual(values["scope"], ["https://app.vssps.visualstudio.com/.default"])
        self.assertEqual(values["grant_type"], ["client_credentials"])
        self.assertEqual(send.call_args.kwargs["timeout"], 30)

    def test_token_errors_do_not_expose_response_or_secret(self):
        self.write_config()
        errors = [
            urllib.error.HTTPError("https://login.invalid", 401, "private-client-secret", {}, None), # pyright: ignore[reportArgumentType]
            urllib.error.URLError("private-client-secret"),
        ]
        with mock.patch("azure_artifacts_token.read_secret", return_value="private-client-secret"):
            for error in errors:
                with self.subTest(error=error), mock.patch(
                    "azure_artifacts_token.urllib.request.urlopen", side_effect=error
                ), self.assertRaises(credentials.CredentialError) as result:
                    credentials.get_token(self.ctx.home, TENANT_ID, CLIENT_ID)
                self.assertNotIn("private-client-secret", str(result.exception))
            for body in (b"private-client-secret", b"[]", b"{}", b'{"access_token":42}', b'{"access_token":""}'):
                response = mock.MagicMock()
                response.__enter__.return_value = io.BytesIO(body)
                with self.subTest(body=body), mock.patch(
                    "azure_artifacts_token.urllib.request.urlopen", return_value=response
                ), self.assertRaises(credentials.CredentialError) as result:
                    credentials.get_token(self.ctx.home, TENANT_ID, CLIENT_ID)
                self.assertNotIn("private-client-secret", str(result.exception))

    def test_helper_stdout_contains_only_token(self):
        with mock.patch.object(sys, "argv", [
            "helper", "--mode", "get-token", "--workspace-name", "test-checkout",
            "--tenant-id", TENANT_ID, "--client-id", CLIENT_ID,
        ]), mock.patch(
            "azure_artifacts_token.data_directory", return_value=self.ctx.home
        ), mock.patch("azure_artifacts_token.get_token", return_value="short-lived-token"), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(credentials.main(), 0)
        self.assertEqual(output.getvalue(), "short-lived-token")

    def test_identity_change_rejects_old_secret_before_read_or_network(self):
        self.write_config()
        self.assertEqual(credentials.saved_status(self.ctx.home, CLIENT_ID, TENANT_ID), "different")
        with mock.patch("azure_artifacts_token.read_secret") as read, mock.patch(
            "azure_artifacts_token.urllib.request.urlopen"
        ) as send:
            with self.assertRaisesRegex(credentials.CredentialError, "differs"):
                credentials.get_token(self.ctx.home, CLIENT_ID, TENANT_ID)
        read.assert_not_called()
        send.assert_not_called()

    def test_configure_authenticates_before_storing_and_does_not_store_bad_secret(self):
        arguments = [
            "helper", "--mode", "configure", "--workspace-name", "test",
            "--tenant-id", TENANT_ID, "--client-id", CLIENT_ID,
        ]
        for failed in (True, False):
            events = []
            def authenticate(config, secret):
                events.append("authenticate")
                self.assertEqual(config, {"tenantId": TENANT_ID, "clientId": CLIENT_ID})
                self.assertEqual(secret, "private-secret")
                if failed:
                    raise credentials.CredentialError("HTTP 401")
                return "short-lived"
            with self.subTest(failed=failed), mock.patch.object(sys, "argv", arguments), mock.patch(
                "azure_artifacts_token.data_directory", return_value=self.ctx.home
            ), mock.patch("azure_artifacts_token.sys.stdin", io.StringIO("private-secret")), mock.patch(
                "azure_artifacts_token.request_token", side_effect=authenticate
            ), mock.patch("azure_artifacts_token.store_secret", side_effect=lambda *args: events.append("store")), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(credentials.main(), 1 if failed else 0)
            self.assertEqual(events, ["authenticate"] if failed else ["authenticate", "store"])

    def test_helper_azure_modes_require_explicit_identity_but_copilot_does_not(self):
        for mode in ("configure", "get-token", "status", "check-binding"):
            with self.subTest(mode=mode), mock.patch.object(sys, "argv", [
                "helper", "--mode", mode, "--workspace-name", "test",
            ]), contextlib.redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(SystemExit):
                    credentials.main()
                self.assertIn("--tenant-id and --client-id", output.getvalue())
        with mock.patch.object(sys, "argv", [
            "helper", "--mode", "get-copilot-token", "--workspace-name", "test",
            "--sandbox-name", "disabled-sandbox",
        ]), mock.patch("azure_artifacts_token.get_copilot_token", return_value="github_pat_private"), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(credentials.main(), 0)
        self.assertEqual(output.getvalue(), "github_pat_private")

    def test_helper_binding_check_rejects_changed_identity(self):
        self.write_config()
        with mock.patch.object(sys, "argv", [
            "helper", "--mode", "check-binding", "--workspace-name", "test",
            "--tenant-id", CLIENT_ID, "--client-id", TENANT_ID,
        ]), mock.patch("azure_artifacts_token.data_directory", return_value=self.ctx.home), mock.patch(
            "azure_artifacts_token.read_secret"
        ) as read, contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(credentials.main(), 1)
        self.assertIn("differs", output.getvalue())
        read.assert_not_called()

    def test_helper_commands_follow_shared_identity_edits(self):
        first = self.ctx.helper_command("get-token")
        self.ctx.configuration.write_text(json.dumps({
            **SHARED_CONFIG, "tenantId": CLIENT_ID, "clientId": TENANT_ID,
        }))
        for mode in ("status", "configure", "get-token", "check-binding"):
            command = self.ctx.helper_command(mode)
            self.assertEqual(command[command.index("--tenant-id") + 1], CLIENT_ID)
            self.assertEqual(command[command.index("--client-id") + 1], TENANT_ID)
        self.assertEqual(first[first.index("--tenant-id") + 1], TENANT_ID)

    def test_atomic_write_is_private_and_leaves_no_temporary_files(self):
        path = self.ctx.home / "config.json"
        credentials.atomic_write(path, "first")
        credentials.atomic_write(path, "second")
        self.assertEqual(path.read_text(), "second")
        self.assertEqual(list(self.ctx.home.iterdir()), [path])
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "nt", "Requires native Windows DPAPI.")
    def test_native_windows_dpapi_round_trip(self):
        value = "temporary test secret".encode("utf-16-le")
        encrypted = credentials.dpapi(value, decrypt=False)
        self.assertNotEqual(encrypted, value)
        self.assertEqual(credentials.dpapi(encrypted, decrypt=True), value)


class InstallationTests(SandboxFixture):
    def test_current_helper_requires_matching_source_and_requirements(self):
        self.ctx.python.parent.mkdir(parents=True)
        self.ctx.python.touch()
        credentials.atomic_write(self.ctx.helper, (SOURCE / "azure_artifacts_token.py").read_text())
        self.assertFalse(support.helper_is_current(self.ctx))
        credentials.atomic_write(self.ctx.home / "requirements.txt", (FEATURE / "requirements.txt").read_text())
        self.assertTrue(support.helper_is_current(self.ctx))
        credentials.atomic_write(self.ctx.helper, "# old version")
        self.assertFalse(support.helper_is_current(self.ctx))

    def test_current_helper_comparison_ignores_checkout_line_endings(self):
        self.ctx.python.parent.mkdir(parents=True)
        self.ctx.python.touch()
        for source, target in (
            (SOURCE / "azure_artifacts_token.py", self.ctx.helper),
            (FEATURE / "requirements.txt", self.ctx.home / "requirements.txt"),
        ):
            target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertTrue(support.helper_is_current(self.ctx))

    def test_copied_helper_is_standalone_and_fails_without_leaking_output(self):
        self.ctx.home.mkdir(parents=True)
        credentials.atomic_write(self.ctx.helper, (SOURCE / "azure_artifacts_token.py").read_text())
        environment = dict(os.environ)
        environment.update({
            "XDG_DATA_HOME": str(self.root / "isolated data"),
            "LOCALAPPDATA": str(self.root / "isolated local"),
            "APPDATA": str(self.root / "isolated roaming"),
            "PYTHONPATH": str(SOURCE),
        })
        command = [
            sys.executable, "-I", str(self.ctx.helper),
            "--tenant-id", TENANT_ID, "--client-id", CLIENT_ID,
            "--workspace-name", "test-checkout", "--mode",
        ]
        result = subprocess.run(
            [*command, "status"], cwd=self.root, env=environment,
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(result.stdout, "missing\n")
        result = subprocess.run(
            [*command, "get-token"], cwd=self.root, env=environment,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("configuration is missing or invalid", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_install_uses_host_environment_and_checks_store(self):
        details = json.dumps([[3, 11], str(self.ctx.environment), str(Path(sys.base_prefix))])
        with mock.patch("support.run", side_effect=lambda command, **options: details if "-c" in command else "") as run:
            support.install_helper(self.ctx)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn([sys.executable, "-m", "venv", str(self.ctx.environment)], commands)
        install = next(command for command in commands if "pip" in command)
        self.assertEqual(install[0], str(self.ctx.python))
        self.assertEqual(install[-1], str(self.ctx.feature / "requirements.txt"))
        self.assertEqual(
            commands[-1][commands[-1].index("--mode"):][:2],
            ["--mode", "check-store"],
        )
        self.assertEqual(
            commands[-1][commands[-1].index("--workspace-name") + 1],
            self.ctx.workspace.name,
        )
        self.assertEqual(
            self.ctx.helper.read_text(),
            (SOURCE / "azure_artifacts_token.py").read_text(),
        )

    def test_incomplete_environment_is_not_replaced(self):
        self.ctx.environment.mkdir(parents=True)
        with mock.patch("support.run") as run, self.assertRaisesRegex(support.SetupError, "incomplete"):
            support.install_helper(self.ctx)
        run.assert_not_called()

    def test_incompatible_environment_is_not_replaced(self):
        self.ctx.python.parent.mkdir(parents=True)
        self.ctx.python.touch()
        details = json.dumps([[3, 10], str(self.ctx.environment), str(Path(sys.base_prefix))])
        with mock.patch("support.run", return_value=details) as run, self.assertRaisesRegex(support.SetupError, "incompatible"):
            support.install_helper(self.ctx)
        self.assertEqual(run.call_count, 1)


class WorkflowTests(SandboxFixture):
    def run_start(self, fake, **patches):
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "start.helper_is_current", return_value=True
        ), mock.patch("start.setup", **patches) as wizard, contextlib.redirect_stdout(io.StringIO()):
            start.start(self.ctx)
        return wizard

    def test_daily_launch_keeps_order_mounts_and_defaults(self):
        fake = FakeCommands(self.ctx)
        wizard = self.run_start(fake)
        wizard.assert_not_called()
        calls = fake.sbx_calls()
        self.assertEqual(calls[0], ["kit", "validate", str(self.ctx.mixin)])
        apply = next(call for call in calls if call[0] == "exec")
        self.assertIn("path.join('.vscode', 'copilot-settings.json')", apply[-1])
        self.assertEqual(calls[-2], [
            "policy", "allow", "network", "--sandbox", self.ctx.name, "--protocol", "tcp,udp", "**",
        ])
        self.assertEqual(calls[-1], ["run", "--name", self.ctx.name, "--", "--yolo"])

    def test_missing_sandbox_runs_full_setup_for_same_context(self):
        fake = FakeCommands(self.ctx, existing=False)
        def create(ctx, **options):
            self.assertIs(ctx, self.ctx)
            self.assertEqual(options, {})
            fake.names.add(ctx.name)
            return ctx
        wizard = self.run_start(fake, side_effect=create)
        wizard.assert_called_once_with(self.ctx)

    def test_setup_that_does_not_create_expected_sandbox_fails(self):
        fake = FakeCommands(self.ctx, existing=False)
        with self.assertRaisesRegex(support.SetupError, "expected sandbox"):
            self.run_start(fake, return_value=self.ctx)
        self.assertFalse(any(call[0] == "run" for call in fake.sbx_calls()))

    def test_missing_feed_slot_triggers_azure_only_repair(self):
        fake = FakeCommands(self.ctx)
        fake.slots.remove("azure-artifacts")
        def repair(ctx, **options):
            fake.slots.add("azure-artifacts")
            return ctx
        wizard = self.run_start(fake, side_effect=repair)
        wizard.assert_called_once_with(self.ctx, azure_only=True)

    def test_failed_binding_repair_stops_launch(self):
        fake = FakeCommands(self.ctx)
        fake.fail_mode = "check-binding" # pyright: ignore[reportAttributeAccessIssue]
        with self.assertRaises(support.CommandError):
            self.run_start(fake, return_value=self.ctx)
        self.assertFalse(any(call[0] == "run" for call in fake.sbx_calls()))

    def test_changed_helper_triggers_migration_repair(self):
        fake = FakeCommands(self.ctx)
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "start.helper_is_current", return_value=False
        ), mock.patch("start.setup", return_value=self.ctx) as wizard, contextlib.redirect_stdout(io.StringIO()):
            start.start(self.ctx)
        wizard.assert_called_once_with(self.ctx, azure_only=True)

    def test_unsafe_settings_and_missing_github_slots_block_launch(self):
        for change in ("ssh", "udp", "github", "copilot"):
            fake = FakeCommands(self.ctx)
            if change == "ssh":
                fake.ssh = "true"
            elif change == "udp":
                fake.udp = False
            else:
                fake.slots.remove(change)
            with self.subTest(change=change), self.assertRaises(support.SetupError):
                self.run_start(fake)
            self.assertFalse(any(call[0] == "run" for call in fake.sbx_calls()))

    def test_full_setup_preserves_mounts_and_registers_only_commands(self):
        fake = FakeCommands(self.ctx, existing=False)
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "setup.install_helper"
        ), mock.patch("builtins.input", side_effect=["y", "y", "y", "y"]), mock.patch(
            "setup.hidden_input", return_value="github_pat_private"
        ), mock.patch("setup.open_url"), contextlib.redirect_stdout(io.StringIO()):
            setup.setup(self.ctx)
        calls = fake.sbx_calls()
        self.assertIn(["daemon", "restart"], calls)
        create = next(call for call in calls if call[0] == "create")
        self.assertEqual(create[-2:], [str(self.ctx.workspace), str(self.ctx.workspace.parent)])
        self.assertIn("--skills", create)
        slots = [call for call in calls if call[:2] == ["secret", "set"]]
        self.assertEqual([call[2] for call in slots], ["azure-artifacts", "copilot", "github"])
        for call in slots:
            self.assertIn("--command", call)
            self.assertIn("--refresh", call)
            self.assertNotIn("github_pat_private", " ".join(call))
        self.assertFalse(any(options.get("input_text") for command, options in fake.calls if command[0] == self.ctx.sbx))
        helper_writes = [
            (command, options) for command, options in fake.calls if "configure-copilot" in command
        ]
        self.assertEqual(helper_writes[0][1]["input_text"], "github_pat_private")

    def test_azure_only_repair_leaves_sandbox_global_settings_and_github_unchanged(self):
        fake = FakeCommands(self.ctx)
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "setup.install_helper"
        ), mock.patch("builtins.input", return_value="y"), mock.patch(
            "setup.configure_github"
        ) as github, contextlib.redirect_stdout(io.StringIO()):
            setup.setup(self.ctx, azure_only=True)
        github.assert_not_called()
        for call in fake.sbx_calls():
            self.assertNotIn(call[0], ("create", "login", "settings", "daemon", "policy"))
            if call[:2] == ["secret", "set"]:
                self.assertEqual(call[2], "azure-artifacts")

    def test_azure_only_requires_existing_sandbox(self):
        fake = FakeCommands(self.ctx, existing=False)
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "setup.install_helper"
        ) as install, self.assertRaisesRegex(support.SetupError, "existing sandbox"):
            setup.setup(self.ctx, azure_only=True)
        install.assert_not_called()

    def test_declining_dependency_install_causes_no_host_install_or_setting_changes(self):
        fake = FakeCommands(self.ctx)
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "setup.install_helper"
        ) as install, mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(support.SetupError, "consent"):
                setup.setup(self.ctx)
        install.assert_not_called()
        self.assertEqual(fake.sbx_calls(), [["kit", "validate", str(self.ctx.mixin)]])

    def test_saved_azure_secret_is_reused_without_visible_token_output(self):
        fake = FakeCommands(self.ctx)
        with mock.patch("support.run", side_effect=fake), mock.patch("setup.hidden_input") as prompt, contextlib.redirect_stdout(io.StringIO()):
            setup.configure_azure(self.ctx)
        prompt.assert_not_called()
        token_calls = [(command, options) for command, options in fake.calls if "get-token" in command]
        self.assertTrue(token_calls[0][1]["quiet"])

    def test_new_azure_secret_is_checked_before_registration(self):
        fake = FakeCommands(self.ctx)
        fake.status = "missing"
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "builtins.input", return_value="y"
        ), mock.patch("setup.hidden_input", return_value="private-client-secret"), contextlib.redirect_stdout(io.StringIO()):
            setup.configure_azure(self.ctx)
        modes = [command[command.index("--mode") + 1] for command, _ in fake.calls if "--mode" in command]
        self.assertEqual(modes, ["status", "configure"])
        for call in fake.sbx_calls():
            self.assertNotIn("private-client-secret", " ".join(call))

    def test_token_request_failure_stops_new_secret_registration(self):
        fake = FakeCommands(self.ctx)
        fake.status = "missing"
        fake.fail_mode = "configure"
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "builtins.input", side_effect=["y", "stop"]
        ), mock.patch("setup.hidden_input", return_value="private-client-secret"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(support.CommandError):
                setup.configure_azure(self.ctx)
        self.assertFalse(any(call[:2] == ["secret", "set"] for call in fake.sbx_calls()))

    def test_no_at_secret_question_skips_without_secret_creation_or_input(self):
        fake = FakeCommands(self.ctx)
        fake.status = "missing"
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "builtins.input", return_value="no"
        ), mock.patch("setup.hidden_input") as secret, mock.patch("setup.open_url") as browser, contextlib.redirect_stdout(io.StringIO()) as output:
            ctx = setup.configure_azure(self.ctx)
        self.assertFalse(ctx.azure_enabled)
        self.assertEqual(json.loads(ctx.local_configuration.read_text()), {"azureArtifactsEnabled": False})
        secret.assert_not_called()
        browser.assert_not_called()
        self.assertFalse(fake.sbx_calls())
        self.assertIn("Copilot still runs", output.getvalue())

    def test_authentication_failure_allows_explicit_skip_or_stop_for_saved_and_new_secrets(self):
        for saved in (True, False):
            for action in ("skip", "stop"):
                fake = FakeCommands(self.ctx)
                fake.status = "matching" if saved else "missing"
                fake.fail_mode = "get-token" if saved else "configure"
                answers = [action] if saved else ["yes", action]
                with self.subTest(saved=saved, action=action), mock.patch("support.run", side_effect=fake), mock.patch(
                    "builtins.input", side_effect=answers
                ), mock.patch("setup.hidden_input", return_value="private-secret"), contextlib.redirect_stdout(io.StringIO()) as output:
                    if action == "stop":
                        with self.assertRaises(support.CommandError):
                            setup.configure_azure(self.ctx)
                    else:
                        self.assertFalse(setup.configure_azure(self.ctx).azure_enabled)
                self.assertIn("Azure authentication failed:", output.getvalue())
                self.assertFalse(any(call[:2] == ["secret", "set"] for call in fake.sbx_calls()))

    def test_authentication_failure_can_retry_another_secret_without_silent_skip(self):
        fake = FakeCommands(self.ctx)
        fake.status = "missing"
        failures = 0
        def commands(command, **options):
            nonlocal failures
            if "--mode" in command and command[command.index("--mode") + 1] == "configure":
                failures += 1
                if failures == 1:
                    raise support.CommandError(command[0], 1)
            return fake(command, **options)
        with mock.patch("support.run", side_effect=commands), mock.patch(
            "builtins.input", side_effect=["yes", "retry"]
        ), mock.patch("setup.hidden_input", side_effect=["bad-secret", "good-secret"]) as secret, contextlib.redirect_stdout(io.StringIO()):
            ctx = setup.configure_azure(self.ctx)
        self.assertTrue(ctx.azure_enabled)
        self.assertEqual(secret.call_count, 2)
        self.assertFalse(ctx.local_configuration.exists())
        self.assertTrue(any(call[:3] == ["secret", "set", "azure-artifacts"] for call in fake.sbx_calls()))

    def disabled_context(self):
        self.ctx = support.azure_mode(self.ctx, False)
        self.ctx.configuration.write_text("invalid Azure settings are not used")
        shutil.rmtree(self.ctx.mixin)
        return FakeCommands(self.ctx)

    def assert_no_azure_operations(self, fake):
        for command, _ in fake.calls:
            if "--mode" in command:
                self.assertNotIn(command[command.index("--mode") + 1], (
                    "status", "configure", "get-token", "check-binding",
                ))
            else:
                self.assertNotIn("azure-artifacts", command)
                self.assertNotIn("--kit", command)
                self.assertNotIn("kit", command)

    def test_disabled_full_setup_keeps_generic_controls_helper_and_copilot_slots(self):
        fake = self.disabled_context()
        fake.names.clear()
        fake.slots.discard("azure-artifacts")
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "setup.install_helper"
        ) as install, mock.patch("builtins.input", return_value="yes"), mock.patch(
            "setup.hidden_input", return_value="github_pat_private"
        ), mock.patch("setup.open_url"), mock.patch("setup.configure_azure") as azure, contextlib.redirect_stdout(io.StringIO()):
            ctx = setup.setup(self.ctx)
        self.assertIs(ctx, self.ctx)
        install.assert_called_once_with(ctx)
        azure.assert_not_called()
        self.assert_no_azure_operations(fake)
        calls = fake.sbx_calls()
        self.assertIn(["login"], calls)
        self.assertIn(["daemon", "restart"], calls)
        create = next(call for call in calls if call[0] == "create")
        self.assertEqual(create[-2:], [str(ctx.workspace), str(ctx.workspace.parent)])
        self.assertIn("--skills", create)
        self.assertEqual([call[2] for call in calls if call[:2] == ["secret", "set"]], ["copilot", "github"])

    def test_disabled_start_skips_all_azure_validation_slots_auth_and_repair(self):
        fake = self.disabled_context()
        fake.slots.discard("azure-artifacts")
        wizard = self.run_start(fake)
        wizard.assert_not_called()
        self.assert_no_azure_operations(fake)
        self.assertEqual(fake.sbx_calls()[-1], ["run", "--name", self.ctx.name, "--", "--yolo"])

    def test_persisted_false_gates_full_setup_and_daily_start(self):
        for full_setup in (True, False):
            fake = self.disabled_context() if full_setup else FakeCommands(self.ctx)
            support.save_azure_choice(self.ctx, False)
            unresolved = replace(self.ctx, azure_enabled=None)
            with self.subTest(full_setup=full_setup), mock.patch("support.run", side_effect=fake), mock.patch(
                "start.helper_is_current", return_value=True
            ), mock.patch("setup.install_helper"), mock.patch("setup.configure_github"), mock.patch(
                "setup.configure_azure"
            ) as azure, mock.patch("builtins.input", return_value="yes") as prompt, contextlib.redirect_stdout(io.StringIO()):
                if full_setup:
                    setup.setup(unresolved)
                else:
                    start.start(unresolved)
                    prompt.assert_not_called()
            azure.assert_not_called()
            self.assert_no_azure_operations(fake)

    def test_disabled_start_still_requires_safe_settings_and_both_copilot_slots(self):
        self.disabled_context()
        for change in ("ssh", "udp", "copilot", "github"):
            fake = FakeCommands(self.ctx)
            if change == "ssh":
                fake.ssh = "true"
            elif change == "udp":
                fake.udp = False
            else:
                fake.slots.remove(change)
            with self.subTest(change=change), self.assertRaises(support.SetupError):
                self.run_start(fake)
            self.assert_no_azure_operations(fake)
            self.assertFalse(any(call[0] == "run" for call in fake.sbx_calls()))

    def test_disabled_start_still_updates_standalone_helper_without_azure_repair(self):
        fake = self.disabled_context()
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "start.helper_is_current", return_value=False
        ), mock.patch("start.install_helper") as install, mock.patch("start.setup") as wizard, mock.patch(
            "builtins.input", return_value="yes"
        ), contextlib.redirect_stdout(io.StringIO()):
            start.start(self.ctx)
        install.assert_called_once_with(self.ctx)
        wizard.assert_not_called()
        self.assert_no_azure_operations(fake)

    def test_disabled_azure_only_repair_does_not_install_or_check_azure(self):
        fake = self.disabled_context()
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "setup.install_helper"
        ) as install, mock.patch("builtins.input") as prompt, contextlib.redirect_stdout(io.StringIO()):
            ctx = setup.setup(self.ctx, azure_only=True)
        self.assertIs(ctx, self.ctx)
        install.assert_not_called()
        prompt.assert_not_called()
        self.assert_no_azure_operations(fake)

    def test_disabled_start_does_not_reuse_existing_enabled_sandbox(self):
        fake = self.disabled_context()
        fake.names = {self.ctx.name + "-azure-artifacts"}
        def create(ctx):
            fake.names.add(ctx.name)
            return ctx
        wizard = self.run_start(fake, side_effect=create)
        wizard.assert_called_once_with(self.ctx)
        self.assert_no_azure_operations(fake)
        self.assertEqual(len(fake.names), 2)

    def test_no_secret_during_full_setup_uses_disabled_name_for_creation_and_copilot(self):
        fake = FakeCommands(self.ctx, existing=False)
        fake.status = "missing"
        with mock.patch("support.run", side_effect=fake), mock.patch(
            "setup.install_helper"
        ), mock.patch("builtins.input", side_effect=["yes", "yes", "no", "yes", "yes"]), mock.patch(
            "setup.hidden_input", return_value="github_pat_private"
        ), mock.patch("setup.open_url"), contextlib.redirect_stdout(io.StringIO()):
            ctx = setup.setup(self.ctx)
        self.assertFalse(ctx.azure_enabled)
        create = next(call for call in fake.sbx_calls() if call[0] == "create")
        self.assertEqual(create[create.index("--name") + 1], ctx.name)
        self.assertNotIn("--kit", create)
        for command, _ in fake.calls:
            if "configure-copilot" in command:
                self.assertEqual(command[command.index("--sandbox-name") + 1], ctx.name)
        self.assertFalse(any("check-binding" in command for command, _ in fake.calls))

    def test_skip_during_azure_repair_switches_to_separate_disabled_sandbox(self):
        fake = FakeCommands(self.ctx)
        fake.slots.remove("azure-artifacts")
        disabled = support.azure_mode(self.ctx, False)
        def repair(ctx, **options):
            if options.get("azure_only"):
                return disabled
            self.assertEqual(ctx, disabled)
            fake.names.add(ctx.name)
            return ctx
        wizard = self.run_start(fake, side_effect=repair)
        self.assertEqual(wizard.call_args_list, [
            mock.call(self.ctx, azure_only=True), mock.call(disabled),
        ])
        self.assertEqual(fake.sbx_calls()[-1], ["run", "--name", disabled.name, "--", "--yolo"])


if __name__ == "__main__":
    unittest.main()
