"""Interactive host setup for the Copilot sandbox."""

import argparse
import sys
import webbrowser

from support import (
    CommandError, Context, SetupError, azure_configuration, cli, confirm, context,
    hidden_input, install_helper, resolve_azure, shell_command, skip_azure,
)


def open_url(url: str) -> None:
    print(f"Open in your browser: {url}")
    try:
        opened = webbrowser.open(url)
    except webbrowser.Error:
        opened = False
    if not opened:
        print("The browser could not be opened. Visit the URL manually.")


def authentication_failure(error: CommandError) -> str:
    print(f"Azure authentication failed: {error} See the helper error above.")
    while True:
        answer = input("Try another secret, skip Azure, or stop? [retry/skip/stop] ").strip().lower()
        if answer in ("retry", "skip", "stop"):
            if answer == "stop":
                raise error
            return answer
        print("Enter retry, skip, or stop.")


def configure_azure(ctx: Context) -> Context:
    config = azure_configuration(ctx)
    display = config["appDisplayName"] or config["clientId"]
    print("\nOptional Azure Artifacts service principal")
    print("The app must be in Azure DevOps and have Feed Reader access.")
    print(f"App: {display} (client ID {config['clientId']})")
    print(f"Tenant ID: {config['tenantId']}")
    print("Each user must use a separate client secret. Never share secret values.")
    status = ctx.helper_run("status", capture=True)
    if status not in ("matching", "missing", "different", "invalid"):
        raise SetupError("Could not read the saved Azure Artifacts configuration status.")
    ready = False
    if status == "matching":
        print("Checking your saved personal secret.")
        try:
            ctx.helper_run("get-token", quiet=True)
            ready = True
            print("Your saved personal secret works and will be reused.")
        except CommandError as error:
            if authentication_failure(error) == "skip":
                return skip_azure(ctx)
    elif status in ("different", "invalid"):
        print(f"The saved configuration is {status}. Its secret will not be reused.")
    if not ready:
        if status != "matching":
            while True:
                answer = input("Do you already have your own active secret for this app? [y/N] ").strip().lower()
                if answer in ("n", "no", ""):
                    return skip_azure(ctx)
                if answer in ("y", "yes"):
                    break
                print("Enter yes or no.")
        while True:
            secret = hidden_input("Your client secret (hidden): ")
            if not secret.strip():
                raise SetupError("A service principal client secret is required.")
            try:
                ctx.helper_run("configure", input_text=secret)
                del secret
                break
            except CommandError as error:
                if authentication_failure(error) == "skip":
                    return skip_azure(ctx)
    print("The client secret stays on the host, protected by DPAPI or Linux Secret Service.")
    print("Docker will inject a short-lived token only for pkgs.dev.azure.com.")
    command = shell_command(ctx.helper_command("get-token"))
    ctx.sbx_run(
        "secret", "set", "azure-artifacts", "--sandbox", ctx.name,
        "--command", command, "--refresh", "45m",
    )
    return ctx


def configure_github(ctx: Context) -> None:
    print("\nSeparate Copilot-only GitHub token")
    print("Create a new token. Do not paste an existing host token or an SSH key.")
    open_url("https://github.com/settings/personal-access-tokens/new")
    print("Set Token name to Copilot sandbox and use a short expiration.")
    print("Select your personal account as Resource owner, not an organization.")
    print("Select Public repositories for Repository access.")
    print("Under Permissions > Account, add Copilot Requests with Read-only access.")
    print("Do not add repository permissions. Generate the token and copy its value.")
    token = hidden_input("Paste the new token (hidden): ")
    if not token.startswith("github_pat_"):
        raise SetupError("Expected a new fine-grained GitHub token.")
    print("The helper stores the token in DPAPI or Linux Secret Service, outside the workspace.")
    print("Docker stores command references instead of the long-lived token value.")
    print("Both copilot and github slots are needed for model and account requests.")
    confirm("Store or replace the same dedicated token in both slots for this sandbox?")
    ctx.helper_run("configure-copilot", sandbox=True, input_text=token)
    del token
    command = shell_command(ctx.helper_command("get-copilot-token", sandbox=True))
    for slot in ("copilot", "github"):
        ctx.sbx_run(
            "secret", "set", slot, "--sandbox", ctx.name,
            "--command", command, "--refresh", "45m",
        )


def setup(ctx: Context, *, azure_only: bool = False) -> Context:
    ctx = resolve_azure(ctx)
    ctx.validate()
    if azure_only and not ctx.azure_enabled:
        print("Azure Artifacts is disabled. No Azure repair is needed.")
        return ctx
    if azure_only and ctx.name not in ctx.sandboxes():
        raise SetupError("Azure-only repair requires an existing sandbox. Run full setup first.")
    print("Azure Artifacts credential repair" if azure_only else "Copilot sandbox setup")
    print(f"Host helper and Python environment: {ctx.home}")
    print("Setup installs the host helper's Python dependencies in this folder.")
    confirm("Install or update the host-only Python helper and dependencies?")
    install_helper(ctx)
    if not azure_only:
        print("\nDocker sign-in and host settings")
        print("Sign in in the browser, then return to this terminal.")
        ctx.sbx_run("login")
        print("The next step changes global settings and stops other local sandbox sessions.")
        confirm("Turn off SSH and clipboard sharing, enable UDP, and restart the daemon?")
        for key, value in (
            ("ssh.agentForwardingEnabled", "false"),
            ("clipboard.imagePaste", "false"),
            ("platform.allowExperimentalFeatures", "true"),
            ("feature.udp-egress", "true"),
        ):
            ctx.sbx_run("settings", "set", key, value)
        ctx.sbx_run("daemon", "restart")
        if ctx.sbx_run("settings", "get", "ssh.agentForwardingEnabled", capture=True) != "false":
            raise SetupError("SSH agent forwarding is not disabled. Setup stopped.")
    if ctx.azure_enabled:
        ctx = configure_azure(ctx)
    if not azure_only:
        print("\nCreate the selected sandbox")
        print(f"The sandbox can read and change the entire parent folder: {ctx.workspace.parent}")
        print("Secrets already saved in that folder remain visible.")
        print("No host SSH agent, home folder, Docker socket, or shared skills will be shared.")
        print("Choose Open at the first network-policy prompt. This is a global default.")
        print("This sandbox also gets an explicit allow rule for all TCP and UDP destinations.")
        if ctx.azure_enabled:
            print("Approve the azure-artifacts credential for pkgs.dev.azure.com when Docker prompts.")
        confirm("Create or reuse the sandbox with the parent folder shared?")
        if ctx.name not in ctx.sandboxes():
            ctx.create()
        ctx.allow_network()
    if ctx.azure_enabled:
        ctx.helper_run("check-binding")
    if not azure_only:
        configure_github(ctx)
    print("\nSetup complete.")
    if azure_only:
        print("Global settings, the existing sandbox, and the Copilot token were left unchanged.")
    else:
        print("Trust this workspace in VS Code and allow automatic tasks once.")
        print("Use Tasks: Run Task > Copilot CLI to start now.")
    return ctx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace")
    parser.add_argument("--sandbox-name")
    parser.add_argument("--azure-only", action="store_true")
    args = parser.parse_args()
    setup(context(args.workspace, args.sandbox_name), azure_only=args.azure_only)
    return 0


if __name__ == "__main__":
    sys.exit(cli(main))
