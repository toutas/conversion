"""Host-only Azure Artifacts credentials and short-lived tokens."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


class CredentialError(Exception):
    pass


def absolute_directory(value: str | Path, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise CredentialError(f"{name} must be an absolute path.")
    return path.resolve()


def workspace_folder_name(value: str) -> str:
    windows_path = PureWindowsPath(value)
    if (
        not value or "\0" in value or value in {".", ".."}
        or PurePosixPath(value).name != value
        or windows_path.name != value or windows_path.drive or windows_path.root
    ):
        raise CredentialError("Repository root folder name must be a single path component.")
    return value


def data_directory(workspace_name: str) -> Path:
    name = workspace_folder_name(workspace_name)
    if sys.platform == "win32":
        value = os.environ.get("LOCALAPPDATA")
        if not value:
            raise CredentialError("LOCALAPPDATA is not set.")
        root = absolute_directory(value, "LOCALAPPDATA")
    elif sys.platform == "linux":
        root = absolute_directory(
            os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share",
            "XDG_DATA_HOME",
        )
    else:
        raise CredentialError("This setup supports Windows and Linux hosts only.")
    return root / name / "azure-artifacts"


def secret_service_name(home: Path) -> str:
    return f"{workspace_folder_name(home.parent.name)}/azure-artifacts"


def config_directory() -> Path:
    if sys.platform == "win32":
        value = os.environ.get("APPDATA")
        if not value:
            raise CredentialError("APPDATA is not set.")
        return absolute_directory(value, "APPDATA")
    if sys.platform == "linux":
        return absolute_directory(
            os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config",
            "XDG_CONFIG_HOME",
        )
    raise CredentialError("This setup supports Windows and Linux hosts only.")


def binding_path() -> Path:
    return config_directory() / "sbx" / "credentials.yaml"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def dpapi(data: bytes, *, decrypt: bool) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [
            ("size", ctypes.c_uint32),
            ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob),
    ]
    function.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    try:
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise CredentialError(
                "Windows DPAPI could not protect or unlock the client secret. "
                "Use the Windows account that configured it, or run setup again."
            )
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)


def secret_service():
    try:
        from keyring.backends.SecretService import Keyring
        from keyring.errors import KeyringError
        from secretstorage.exceptions import SecretStorageException
    except ImportError as error:
        raise CredentialError(
            "Python keyring is missing. Run the sandbox setup to install host dependencies."
        ) from error
    try:
        backend = Keyring()
        if backend.priority <= 0:
            raise CredentialError("Secret Service is unavailable.")
        backend.get_preferred_collection()
        return backend
    except (KeyringError, SecretStorageException, RuntimeError) as error:
        raise CredentialError(
            "Linux Secret Service is unavailable or locked. Start and unlock a "
            "Secret Service keyring in this user session, then retry. "
            "Plaintext credential storage is not supported."
        ) from error


def validate_ids(tenant: str, client: str) -> None:
    if not GUID.fullmatch(tenant) or not GUID.fullmatch(client):
        raise CredentialError("Tenant ID and client ID must be GUIDs.")


def load_config(home: Path) -> dict[str, str]:
    try:
        config = json.loads((home / "service-principal.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise CredentialError("Azure Artifacts configuration is missing or invalid. Run setup.") from error
    if not isinstance(config, dict):
        raise CredentialError("Azure Artifacts configuration must be a JSON object. Run setup.")
    tenant, client = config.get("tenantId"), config.get("clientId")
    if not isinstance(tenant, str) or not isinstance(client, str):
        raise CredentialError("Azure Artifacts configuration has invalid IDs. Run setup.")
    validate_ids(tenant, client)
    return {"tenantId": tenant.lower(), "clientId": client.lower()}


def saved_status(home: Path, tenant: str, client: str) -> str:
    validate_ids(tenant, client)
    if not (home / "service-principal.json").is_file():
        return "missing"
    if sys.platform == "win32" and not (home / "client-secret.dpapi").is_file():
        return "missing"
    try:
        config = load_config(home)
    except CredentialError:
        return "invalid"
    return "matching" if config == {"tenantId": tenant.lower(), "clientId": client.lower()} else "different"


def intended_config(home: Path, tenant: str, client: str) -> dict[str, str]:
    validate_ids(tenant, client)
    config = load_config(home)
    if config != {"tenantId": tenant.lower(), "clientId": client.lower()}:
        raise CredentialError("The saved Azure identity differs from the intended IDs. Run setup.")
    return config


def store_secure(home: Path, filename: str, account: str, secret: str) -> None:
    if not secret.strip():
        raise CredentialError("Credential input was empty.")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform == "win32":
        encrypted = dpapi(secret.encode("utf-16-le"), decrypt=False)
        atomic_write(home / filename, encrypted.hex())
    elif sys.platform == "linux":
        backend = secret_service()
        from keyring.errors import KeyringError
        from secretstorage.exceptions import SecretStorageException

        try:
            backend.set_password(secret_service_name(home), account, secret)
        except (KeyringError, SecretStorageException) as error:
            raise CredentialError("Secret Service could not store the credential.") from error
    else:
        raise CredentialError("Unsupported host OS.")


def store_secret(home: Path, tenant: str, client: str, secret: str) -> None:
    validate_ids(tenant, client)
    tenant, client = tenant.lower(), client.lower()
    store_secure(home, "client-secret.dpapi", f"{tenant}/{client}", secret)
    atomic_write(
        home / "service-principal.json",
        json.dumps({"tenantId": tenant, "clientId": client}),
    )


def read_secure(home: Path, filename: str, account: str) -> str:
    if sys.platform == "win32":
        try:
            encrypted = bytes.fromhex((home / filename).read_text(encoding="utf-8-sig").strip())
            secret = dpapi(encrypted, decrypt=True).decode("utf-16-le")
        except (OSError, ValueError, UnicodeError) as error:
            raise CredentialError("The saved DPAPI credential is missing or invalid. Run setup.") from error
    elif sys.platform == "linux":
        backend = secret_service()
        from keyring.errors import KeyringError
        from secretstorage.exceptions import SecretStorageException

        try:
            secret = backend.get_password(secret_service_name(home), account)
        except (KeyringError, SecretStorageException) as error:
            raise CredentialError("Secret Service could not unlock the credential.") from error
    else:
        raise CredentialError("Unsupported host OS.")
    if not secret or not secret.strip():
        raise CredentialError("The saved credential is missing. Run setup.")
    return secret


def read_secret(home: Path, config: dict[str, str]) -> str:
    return read_secure(home, "client-secret.dpapi", f"{config['tenantId']}/{config['clientId']}")


def copilot_filename(sandbox: str) -> str:
    return f"copilot-{hashlib.sha256(sandbox.encode('utf-8')).hexdigest()}.dpapi"


def store_copilot_token(home: Path, sandbox: str, token: str) -> None:
    if not re.fullmatch(r"github_pat_[A-Za-z0-9_]+", token):
        raise CredentialError("Expected a new fine-grained GitHub token.")
    store_secure(home, copilot_filename(sandbox), f"copilot/{sandbox}", token)


def get_copilot_token(home: Path, sandbox: str) -> str:
    return read_secure(home, copilot_filename(sandbox), f"copilot/{sandbox}")


def has_binding(path: Path) -> bool:
    try:
        import yaml
    except ImportError as error:
        raise CredentialError("PyYAML is missing. Run the sandbox setup to install host dependencies.") from error
    if not path.is_file():
        return False
    try:
        binding = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError) as error:
        raise CredentialError("The Docker credential binding file is unreadable or invalid.") from error
    for key in ("bindings", "azure-artifacts", "apiKey"):
        if not isinstance(binding, dict):
            return False
        binding = binding.get(key)
    domains = binding.get("domains") if isinstance(binding, dict) else binding
    return isinstance(domains, list) and "pkgs.dev.azure.com" in domains


def get_token(home: Path, tenant: str, client: str) -> str:
    config = intended_config(home, tenant, client)
    return request_token(config, read_secret(home, config))


def request_token(config: dict[str, str], secret: str) -> str:
    body = urllib.parse.urlencode({
        "client_id": config["clientId"],
        "client_secret": secret,
        "scope": "https://app.vssps.visualstudio.com/.default",
        "grant_type": "client_credentials",
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://login.microsoftonline.com/{config['tenantId']}/oauth2/v2.0/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise CredentialError(f"Azure DevOps token request failed with HTTP {error.code}.") from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise CredentialError("Azure DevOps token request failed. Check network access and credentials.") from error
    except ValueError as error:
        raise CredentialError("Azure DevOps returned an invalid token response.") from error
    token = result.get("access_token") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise CredentialError("Azure DevOps token response did not contain an access token.")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=(
        "configure", "get-token", "check-binding", "check-store", "status",
        "configure-copilot", "get-copilot-token",
    ))
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--tenant-id")
    parser.add_argument("--client-id")
    parser.add_argument("--sandbox-name")
    args = parser.parse_args()
    if args.mode in ("configure-copilot", "get-copilot-token") and not args.sandbox_name:
        parser.error("--sandbox-name is required for Copilot credentials")
    if args.mode in ("configure", "get-token", "status", "check-binding") and (
        not args.tenant_id or not args.client_id
    ):
        parser.error("--tenant-id and --client-id are required for Azure credentials")
    try:
        home = data_directory(args.workspace_name)
        if args.mode == "configure":
            validate_ids(args.tenant_id, args.client_id)
            secret = sys.stdin.read().rstrip("\r\n")
            if not secret.strip():
                raise CredentialError("Credential input was empty.")
            request_token({"tenantId": args.tenant_id.lower(), "clientId": args.client_id.lower()}, secret)
            store_secret(home, args.tenant_id, args.client_id, secret)
        elif args.mode == "get-token":
            sys.stdout.write(get_token(home, args.tenant_id, args.client_id))
        elif args.mode == "configure-copilot":
            store_copilot_token(home, args.sandbox_name, sys.stdin.read().rstrip("\r\n"))
        elif args.mode == "get-copilot-token":
            sys.stdout.write(get_copilot_token(home, args.sandbox_name))
        elif args.mode == "status":
            print(saved_status(home, args.tenant_id, args.client_id))
        elif args.mode == "check-store":
            if sys.platform == "linux":
                secret_service()
        else:
            read_secret(home, intended_config(home, args.tenant_id, args.client_id))
            if not has_binding(binding_path()):
                raise CredentialError(
                    "The pkgs.dev.azure.com credential binding is missing. "
                    "Run setup and approve the Azure Artifacts credential."
                )
        return 0
    except (CredentialError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Credential operation cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
