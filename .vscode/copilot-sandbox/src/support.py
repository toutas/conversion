"""Shared host operations for the Copilot sandbox task and setup."""

from collections.abc import Callable
from dataclasses import dataclass, replace
import getpass
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import warnings

import azure_artifacts_token as credentials


class SetupError(Exception):
    pass


class CommandError(SetupError):
    def __init__(self, program: str, code: int):
        super().__init__(f"{Path(program).name} failed with exit code {code}.")
        self.code = code


def run(
    command: list[str], *, capture: bool = False,
    quiet: bool = False, input_text: str | None = None,
) -> str:
    result = subprocess.run(
        command, input=input_text, text=True, encoding="utf-8",
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL if quiet else None,
        check=False,
    )
    if result.returncode:
        raise CommandError(command[0], result.returncode)
    return result.stdout.strip() if capture else ""


def outside_mount(path: Path, parent: Path) -> None:
    if path.resolve().is_relative_to(parent.resolve()):
        raise SetupError(
            f"Host credentials, helper, and Python dependencies must be outside "
            f"the mounted parent folder: {parent}. Unsafe path: {path}"
        )


def shell_command(arguments: list[str]) -> str:
    if any("\n" in value or "\r" in value or "\0" in value for value in arguments):
        raise SetupError("Host command arguments must not contain newlines or NULs.")
    if sys.platform == "win32":
        # Make the inner shell explicit; CALL also handles executable paths with spaces.
        if any(character in value for value in arguments for character in '%!"&|<>^`$'):
            raise SetupError("Host helper paths must not contain Windows shell control characters.")
        return "cmd.exe /d /c call " + " ".join(f'"{value}"' for value in arguments)
    return shlex.join(arguments)


@dataclass(frozen=True)
class Context:
    workspace: Path
    name: str
    sbx: str
    home: Path
    azure_enabled: bool | None = None

    @property
    def configuration(self) -> Path:
        return self.workspace / ".vscode" / "copilot-sandbox.json"

    @property
    def local_configuration(self) -> Path:
        return self.workspace / ".vscode" / "copilot-sandbox.local.json"

    @property
    def feature(self) -> Path:
        return self.workspace / ".vscode" / "copilot-sandbox"

    @property
    def source(self) -> Path:
        return self.feature / "src"

    @property
    def mixin(self) -> Path:
        return self.feature / "azure-artifacts-mixin"

    @property
    def environment(self) -> Path:
        return self.home / ".venv"

    @property
    def python(self) -> Path:
        return self.environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

    @property
    def helper(self) -> Path:
        return self.home / "azure_artifacts_token.py"

    def sbx_run(
        self, *arguments: str, capture: bool = False,
        quiet: bool = False, input_text: str | None = None,
    ) -> str:
        return run([self.sbx, *arguments], capture=capture, quiet=quiet, input_text=input_text)

    def helper_command(self, mode: str, *, sandbox: bool = False) -> list[str]:
        arguments = [
            str(self.python), "-I", str(self.helper), "--mode", mode,
            "--workspace-name", self.workspace.name,
        ]
        if sandbox:
            arguments.extend(["--sandbox-name", self.name])
        if mode in ("status", "configure", "get-token", "check-binding"):
            config = azure_configuration(self)
            credentials.validate_ids(config["tenantId"], config["clientId"])
            arguments.extend([
                "--tenant-id", config["tenantId"], "--client-id", config["clientId"],
            ])
        return arguments

    def helper_run(
        self, mode: str, *, sandbox: bool = False, capture: bool = False,
        quiet: bool = False, input_text: str | None = None,
    ) -> str:
        return run(
            self.helper_command(mode, sandbox=sandbox),
            capture=capture, quiet=quiet, input_text=input_text,
        )

    def secrets(self, service: str) -> list[str]:
        return self.sbx_run(
            "secret", "ls", "--sandbox", self.name,
            "--service", service, "--quiet", capture=True,
        ).splitlines()

    def sandboxes(self) -> list[str]:
        return self.sbx_run("ls", "--quiet", capture=True).splitlines()

    def validate(self) -> None:
        for path in (
            self.home, self.environment, self.python, self.helper,
            self.home / "service-principal.json", self.home / "client-secret.dpapi",
            self.home / "requirements.txt", credentials.binding_path(),
            credentials.config_directory() / "com.docker.sandboxes",
            Path(sys.base_prefix),
        ):
            outside_mount(path, self.workspace.parent)
        if self.azure_enabled and not (self.mixin / "spec.yaml").is_file():
            raise SetupError(f"The Azure Artifacts mixin is missing: {self.mixin}")
        settings = self.workspace / ".vscode" / "copilot-settings.json"
        try:
            value = json.loads(settings.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise SetupError(f"Copilot defaults are missing or invalid: {settings}") from error
        if not isinstance(value, dict):
            raise SetupError("Copilot defaults must be a JSON object.")
        if self.azure_enabled:
            self.sbx_run("kit", "validate", str(self.mixin))

    def create(self) -> None:
        arguments = ["create", "--name", self.name, "--skills", "off", "copilot"]
        if self.azure_enabled:
            arguments.extend(["--kit", str(self.mixin)])
        self.sbx_run(*arguments, str(self.workspace), str(self.workspace.parent))

    def allow_network(self) -> None:
        self.sbx_run(
            "policy", "allow", "network", "--sandbox", self.name,
            "--protocol", "tcp,udp", "**",
        )


def context(workspace: str | None, name: str | None) -> Context:
    if sys.version_info < (3, 11):
        raise SetupError("Host Python 3.11 or later is required.")
    path = Path(workspace).resolve() if workspace else Path(__file__).resolve().parents[3]
    if not path.is_dir():
        raise SetupError(f"Workspace folder does not exist: {path}")
    home = credentials.data_directory(path.name)
    executable = shutil.which("sbx.exe" if sys.platform == "win32" else "sbx")
    if executable is None and sys.platform == "win32":
        candidate = home.parents[1] / "DockerSandboxes/bin/sbx.exe"
        if candidate.is_file():
            executable = str(candidate)
    if executable is None:
        raise SetupError(
            "Docker Sandboxes is not installed or is not on PATH. "
            "See the host installation steps in README.md."
        )
    sandbox = name or f"copilot-{path.name}-isolated"
    return Context(path, sandbox, executable, home)


def initialize_azure_configuration(ctx: Context) -> None:
    if not ctx.configuration.exists():
        credentials.atomic_write(ctx.configuration, json.dumps({
            "tenantId": "", "clientId": "", "appDisplayName": "",
        }, indent=2) + "\n")


def azure_configuration(ctx: Context) -> dict[str, str]:
    initialize_azure_configuration(ctx)
    path = ctx.configuration
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise SetupError(f"Azure configuration is unreadable or invalid JSON: {path}") from error
    if not isinstance(value, dict):
        raise SetupError(f"Azure configuration must be a JSON object: {path}")
    for field in ("tenantId", "clientId", "appDisplayName"):
        if not isinstance(value.get(field), str):
            raise SetupError(f"{path}: {field} must be a string.")
        if field != "appDisplayName" and value[field] and not credentials.GUID.fullmatch(value[field]):
            raise SetupError(f"{path}: {field} must be an empty string or a valid GUID.")
    return {field: value[field] for field in ("tenantId", "clientId", "appDisplayName")}


def azure_mode(ctx: Context, enabled: bool) -> Context:
    name = ctx.name
    suffix = "-azure-artifacts"
    ending = name[-len(suffix):] if name.lower().endswith(suffix) else suffix
    while name.lower().endswith(suffix):
        name = name[:-len(suffix)]
    return replace(ctx, name=name + ending if enabled else name, azure_enabled=enabled)


def skip_azure(ctx: Context) -> Context:
    save_azure_choice(ctx, False)
    print(
        "Azure Artifacts is disabled. Copilot still runs. Private package downloads "
        "and dependent installs, builds, and tests can fail. Cached packages may work."
    )
    return azure_mode(ctx, False)


def save_azure_choice(ctx: Context, enabled: bool) -> None:
    credentials.atomic_write(
        ctx.local_configuration, json.dumps({"azureArtifactsEnabled": enabled}, indent=2) + "\n",
    )


def resolve_azure(ctx: Context) -> Context:
    if ctx.azure_enabled is not None:
        return ctx
    initialize_azure_configuration(ctx)
    enabled = None
    if ctx.local_configuration.exists():
        try:
            local = json.loads(ctx.local_configuration.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise SetupError(f"Local configuration is unreadable or invalid JSON: {ctx.local_configuration}") from error
        if not isinstance(local, dict) or type(local.get("azureArtifactsEnabled")) is not bool:
            raise SetupError(f"{ctx.local_configuration}: azureArtifactsEnabled must be a boolean.")
        enabled = local["azureArtifactsEnabled"]
    if enabled is False:
        return azure_mode(ctx, False)
    while True:
        config = azure_configuration(ctx)
        missing = [field for field in ("tenantId", "clientId") if not config[field]]
        print(f"Azure Artifacts settings: {ctx.configuration}")
        if missing:
            print(f"Missing fields: {', '.join(missing)}.")
        if not config["appDisplayName"]:
            print("Warning: appDisplayName is empty. The client ID will identify the app.")
        if enabled is True and not missing:
            return azure_mode(ctx, True)
        print("Azure access needs valid tenantId and clientId GUIDs and your own active client secret.")
        print("You can edit the settings file while this prompt waits.")
        answer = input("Enable Azure Artifacts with these settings? [y/N] ").strip().lower()
        if answer in ("n", "no", ""):
            return skip_azure(ctx)
        if answer not in ("y", "yes"):
            print("Enter yes or no.")
            continue
        config = azure_configuration(ctx)
        if any(not config[field] for field in ("tenantId", "clientId")):
            print("The IDs are still incomplete. Edit the settings file before answering yes.")
            continue
        save_azure_choice(ctx, True)
        enabled = True


def install_helper(ctx: Context) -> None:
    for path in (ctx.home, ctx.environment, ctx.python, ctx.helper):
        outside_mount(path, ctx.workspace.parent)
    ctx.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform != "win32":
        ctx.home.chmod(0o700)
    if ctx.environment.is_symlink():
        raise SetupError(f"The host Python environment must not be a symlink: {ctx.environment}")
    if ctx.environment.exists() and not ctx.python.is_file():
        raise SetupError(f"The host Python environment is incomplete: {ctx.environment}. Do not replace it without review.")
    if not ctx.environment.exists():
        run([sys.executable, "-m", "venv", str(ctx.environment)])
    details = json.loads(run([
        str(ctx.python), "-I", "-c",
        "import json,sys; print(json.dumps([list(sys.version_info[:2]), sys.prefix, sys.base_prefix]))",
    ], capture=True))
    if details[0] < [3, 11] or Path(details[1]).resolve() != ctx.environment.resolve():
        raise SetupError(f"The host Python environment is incompatible: {ctx.environment}. Do not replace it without review.")
    outside_mount(Path(details[2]), ctx.workspace.parent)
    requirements = ctx.feature / "requirements.txt"
    run([
        str(ctx.python), "-I", "-m", "pip", "install", "--disable-pip-version-check",
        "-r", str(requirements),
    ])
    credentials.atomic_write(
        ctx.helper, (ctx.source / "azure_artifacts_token.py").read_text(encoding="utf-8")
    )
    credentials.atomic_write(ctx.home / "requirements.txt", requirements.read_text(encoding="utf-8"))
    ctx.helper_run("check-store")


def helper_is_current(ctx: Context) -> bool:
    if not ctx.python.is_file():
        return False
    for source, target in (
        (ctx.source / "azure_artifacts_token.py", ctx.helper),
        (ctx.feature / "requirements.txt", ctx.home / "requirements.txt"),
    ):
        if not target.is_file() or target.read_text(encoding="utf-8") != source.read_text(encoding="utf-8"):
            return False
    return True


def confirm(message: str) -> None:
    if input(f"{message} [y/N] ").strip().lower() not in ("y", "yes"):
        raise SetupError("Setup stopped. No consent was given for this step.")


def hidden_input(message: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(message)
        except getpass.GetPassWarning as error:
            raise SetupError("A terminal with hidden input is required for credentials.") from error


def cli(action: Callable[[], int]) -> int:
    try:
        return action()
    except CommandError as error:
        print(f"Error: {error}", file=sys.stderr)
        return error.code if error.code > 0 else 128 - error.code
    except (SetupError, credentials.CredentialError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Setup or launch cancelled.", file=sys.stderr)
        return 130
