# Connecting your AI app

VEX AIM MCP works with any app that supports MCP, the open Model Context Protocol. This page shows how to add it to
the common ones. The [README](../README.md#install) has the first steps (installing `uv`).

There are two kinds of app:

- **Apps that start the server themselves** run the `vex-aim-mcp` command on your computer: Claude Code, Claude
  Desktop, OpenAI Codex, VS Code (GitHub Copilot), Cursor, Gemini CLI and most others. They all need the same three
  things: the command, its arguments, and the robot's address.
- **Apps that connect to a web address** need the server running in HTTP mode, reachable from the internet through
  a tunnel: ChatGPT. See [ChatGPT](#chatgpt).

In every example, replace `/Users/you/.local/bin/uvx` with what `which uvx` prints in Terminal, and `192.168.1.50`
with your robot's address. Apps opened from the Dock often can't find `uvx` by name, so the full path is safest.

| App | Tested |
|---|---|
| Claude Code (the Claude desktop app's Code tab, and the `claude` command) | Yes, with a real robot |
| Any app over HTTP (MCP's own test client) | Yes, in the automated tests |
| Claude Desktop, ChatGPT, Codex, VS Code, Cursor, Gemini CLI | Not yet: please report how it goes |

## Claude Code

**In the Claude desktop app's Code tab**, put a file called `.mcp.json` in your project folder:

```json
{
  "mcpServers": {
    "vex-aim": {
      "command": "/Users/you/.local/bin/uvx",
      "args": ["--from", "git+https://github.com/dbbudd/VEX-AIM-MCP", "vex-aim-mcp"],
      "env": { "AIM_HOST": "192.168.1.50" }
    }
  }
}
```

Start a new session in that folder and approve the `vex-aim` server when asked.

**With the `claude` command**, for every project:

```bash
claude mcp add vex-aim --scope user --env AIM_HOST=192.168.1.50 -- uvx --from git+https://github.com/dbbudd/VEX-AIM-MCP vex-aim-mcp
```

In the desktop app, Claude can show the control panel in its browser pane next to the chat.

## Claude Desktop

Open Settings → Developer → Edit Config, add the same `mcpServers` entry as above to `claude_desktop_config.json`,
then quit and reopen Claude Desktop.

## OpenAI Codex

The Codex app, command line and IDE extension share `~/.codex/config.toml`. Add:

```toml
[mcp_servers.vex-aim]
command = "/Users/you/.local/bin/uvx"
args = ["--from", "git+https://github.com/dbbudd/VEX-AIM-MCP", "vex-aim-mcp"]
env = { AIM_HOST = "192.168.1.50" }
tool_timeout_sec = 300
```

`tool_timeout_sec` gives longer jobs (exploring the arena, plays) time to finish. Or, from Terminal:

```bash
codex mcp add vex-aim --env AIM_HOST=192.168.1.50 -- uvx --from git+https://github.com/dbbudd/VEX-AIM-MCP vex-aim-mcp
```

## VS Code (GitHub Copilot)

Put a file called `mcp.json` in your project's `.vscode` folder. VS Code calls the list `servers`:

```json
{
  "servers": {
    "vex-aim": {
      "type": "stdio",
      "command": "/Users/you/.local/bin/uvx",
      "args": ["--from", "git+https://github.com/dbbudd/VEX-AIM-MCP", "vex-aim-mcp"],
      "env": { "AIM_HOST": "192.168.1.50" }
    }
  }
}
```

Then use Copilot Chat in agent mode, and start the server when VS Code offers to.

## Cursor

Add the `mcpServers` entry from [Claude Code](#claude-code) to `~/.cursor/mcp.json` (every project) or
`.cursor/mcp.json` in a project.

## Gemini CLI

Add the same `mcpServers` entry to `~/.gemini/settings.json`.

## ChatGPT

ChatGPT runs in the cloud, so it can't start a program on your computer. Instead, you run the server in HTTP mode
and give ChatGPT a web address that reaches it, through a tunnel. You need a ChatGPT plan with Developer mode.

1. **Start the server in HTTP mode**, in Terminal:
   ```bash
   AIM_HOST=192.168.1.50 uvx --from git+https://github.com/dbbudd/VEX-AIM-MCP vex-aim-mcp --http
   ```
   It prints its address, like `http://127.0.0.1:8000/Xy3…/mcp`. The middle part is a secret, made up fresh each
   time it starts.
2. **Start a tunnel**, in a second Terminal window. Cloudflare's free quick tunnel needs no account:
   ```bash
   brew install cloudflared
   ```
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8000
   ```
   It prints a web address like `https://some-words.trycloudflare.com`.
3. **Add it to ChatGPT.** In Settings → Apps & Connectors → Advanced, turn on Developer mode. Then create a
   connector (an "app"):
   - **URL:** the tunnel's address followed by the server's path, e.g. `https://some-words.trycloudflare.com/Xy3…/mcp`.
   - **Authentication:** none. The secret part of the address is what keeps others out.
   ChatGPT's menus change from time to time, so the names may differ a little.
4. **In a chat**, turn on the connector, then ask, e.g. "Connect to the robot and tell me its battery." Ask it to
   open the control panel with `open_browser`, and it opens in your web browser.

**Keep the address private.** Anyone who has it can drive the robot, with the usual motion lock and speed caps.
Stop the tunnel (Ctrl+C) when you're done.

**The same address every time:** a quick tunnel gets a new address each time it starts, and the secret changes too,
so you'd update the connector. To keep both, set `AIM_HTTP_SECRET` to a long random phrase of your own, and use a
named Cloudflare tunnel or an ngrok static domain.

## Other apps

- An app that can **start a local MCP server** (stdio) needs the command `uvx` with the arguments
  `--from git+https://github.com/dbbudd/VEX-AIM-MCP vex-aim-mcp`, and `AIM_HOST` in its environment.
- An app that **connects to an MCP address** (streamable HTTP) needs `vex-aim-mcp --http`, and a tunnel if the app
  runs in the cloud.

## What the panel calls your assistant

The control panel uses the connected app's name on its buttons and in its log: **Point out to Claude**,
**Point out to ChatGPT**, and so on. With no app connected, or one it doesn't recognise, it says "your AI".
