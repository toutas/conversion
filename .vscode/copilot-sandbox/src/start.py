"""Start Copilot with the repository's isolated sandbox settings."""

import argparse
import json
import sys

from setup import setup
from support import (
    CommandError, Context, SetupError, cli, confirm, context, helper_is_current,
    install_helper, resolve_azure,
)


APPLY_SETTINGS = """\
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const source = path.join('.vscode', 'copilot-settings.json');
JSON.parse(fs.readFileSync(source, 'utf8'));
const home = process.env.COPILOT_HOME || path.join(os.homedir(), '.copilot');
fs.mkdirSync(home, { recursive: true });
fs.copyFileSync(source, path.join(home, 'settings.json'));
"""


def start(ctx: Context) -> None:
    ctx = resolve_azure(ctx)
    ctx.validate()
    if ctx.name not in ctx.sandboxes():
        print("The selected sandbox is missing. Starting full setup.")
        ctx = setup(ctx)
        if ctx.name not in ctx.sandboxes():
            raise SetupError(f"Setup did not create the expected sandbox: {ctx.name}")
    needs_setup = not helper_is_current(ctx)
    if ctx.azure_enabled and not needs_setup:
        try:
            ctx.helper_run("check-binding", quiet=True)
        except CommandError:
            print("The host credential check failed. Starting Azure-only repair.")
            needs_setup = True
    if ctx.azure_enabled and "azure-artifacts" not in ctx.secrets("azure-artifacts"):
        needs_setup = True
    if needs_setup:
        if ctx.azure_enabled:
            print("Azure Artifacts setup is incomplete or the host helper needs an update.")
            ctx = setup(ctx, azure_only=True)
            if ctx.name not in ctx.sandboxes():
                ctx = setup(ctx)
        else:
            confirm("Install or update the host-only Python helper and dependencies?")
            install_helper(ctx)
    if ctx.azure_enabled:
        ctx.helper_run("check-binding")
        if "azure-artifacts" not in ctx.secrets("azure-artifacts"):
            raise SetupError("The sandbox Azure Artifacts credential is still missing after setup.")
    if ctx.sbx_run("settings", "get", "ssh.agentForwardingEnabled", capture=True) != "false":
        raise SetupError(
            "SSH agent forwarding must be off. Run: "
            "sbx settings set ssh.agentForwardingEnabled false; sbx daemon restart"
        )
    udp = json.loads(ctx.sbx_run("settings", "get", "feature.udp-egress", "--json", capture=True))
    if not isinstance(udp, dict) or not isinstance(udp.get("value"), dict) or udp["value"].get("enabled") is not True:
        raise SetupError("UDP access is not enabled. Complete the setup in README.md first.")
    if ctx.name not in ctx.sandboxes():
        ctx.create()
    for service in ("copilot", "github"):
        if service not in ctx.secrets(service):
            raise SetupError(
                f"The sandbox {service} credential is missing. Run full setup with "
                "a dedicated Copilot Requests-only token."
            )
    ctx.sbx_run("exec", ctx.name, "node", "-e", APPLY_SETTINGS)
    ctx.allow_network()
    ctx.sbx_run("run", "--name", ctx.name, "--", "--yolo")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace")
    parser.add_argument("--sandbox-name")
    args = parser.parse_args()
    start(context(args.workspace, args.sandbox_name))
    return 0


if __name__ == "__main__":
    sys.exit(cli(main))
