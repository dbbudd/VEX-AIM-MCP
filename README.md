# VEX AIM MCP

Let an AI assistant drive a [VEX AIM](https://www.vexrobotics.com/aim) coding robot. Ask in plain English, and the
assistant drives it over Wi-Fi, sees through its camera, uses its lights, screen, speaker and kicker, and plays
soccer. A live control panel in your browser shows everything the robot sees and lets you drive, set up teams and
run matches.

It works with apps that support MCP, the open Model Context Protocol: Claude, ChatGPT, OpenAI Codex, GitHub Copilot
in VS Code, Cursor, Gemini CLI and others.

![The control panel: live camera with the robot's AI overlaid, the Explore/Teams/Strategy tabs and the map](docs/images/panel.png)

```
Your AI app ──MCP──▶ vex-aim-mcp ──Wi-Fi──▶ VEX AIM robot
                         └── control panel at http://127.0.0.1:8765 (for you)
```

## What it can do

- **See:** photos with a bearing ruler, the robot's own AI detections (balls, barrels, robots, AprilTags), and an
  optional second opinion from a YOLO model on your computer.
- **Move:** drive in any direction, turn, find and approach objects, kick. Moves are bounded and speed-capped, and
  motion starts locked.
- **Play:** fetch the ball, score between two barrels, pass, guard the goal, and advise whether to pass, shoot or
  dribble, from measured kick distances and driving speed.
- **Express:** a player card on the robot's screen (name, team colour and a face that celebrates or sulks), lights,
  sounds, notes and speech.
- **Control panel:** the live camera with AI overlays, a map of everything seen, driving with a game controller,
  keyboard or on-screen pad, matches (autonomous, then driver), teams of several robots, and Wi-Fi set-up.
- **Simulators:** try all of it without a robot.

## What you need

- A VEX AIM robot on Wi-Fi (see [Set up the robot](#set-up-the-robot)).
- An AI app that supports MCP (see [Install](#install)).
- [uv](https://docs.astral.sh/uv/), which installs and runs the server. In Terminal:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- A Mac is best. The Wi-Fi tools and speech use macOS. The rest should work on Windows and Linux, but isn't tested there.

## Install

1. Check it installs (the first run downloads it):
   ```bash
   uvx --from git+https://github.com/dbbudd/VEX-AIM-MCP vex-aim-mcp --version
   ```
2. Find where `uvx` lives. Apps opened from the Dock often can't find it by name, so give them the full path:
   ```bash
   which uvx
   ```
3. Add the server to your app. Most apps take an entry like this, with `/Users/you/.local/bin/uvx` replaced by what
   `which uvx` printed and `192.168.1.50` by your robot's address:
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

Where that goes depends on the app:

| App | Where |
|---|---|
| Claude Code, in the Claude desktop app's Code tab | `.mcp.json` in your project folder |
| Claude Code, the `claude` command | `claude mcp add …` |
| Claude Desktop | Settings → Developer → Edit Config |
| OpenAI Codex (app, command line, IDE) | `~/.codex/config.toml` |
| VS Code with GitHub Copilot | `.vscode/mcp.json` |
| Cursor | `~/.cursor/mcp.json` |
| Gemini CLI | `~/.gemini/settings.json` |
| ChatGPT | HTTP mode, through a tunnel |

[docs/clients.md](docs/clients.md) has the exact steps for each, including ChatGPT, which runs in the cloud and so
reaches the server through a tunnel.

**Options:**
- **YOLO** (a bigger object detector, run on your computer; a large download, as it includes PyTorch): use
  `"vex-aim-mcp[yolo] @ git+https://github.com/dbbudd/VEX-AIM-MCP"` in place of `git+https://github.com/dbbudd/VEX-AIM-MCP`.
- **A fixed version:** add a release tag, e.g. `git+https://github.com/dbbudd/VEX-AIM-MCP@v0.1.0`. Without one,
  `uvx` keeps the version it first downloaded; add `--refresh` to the arguments once to update.

## Set up the robot

The robot has to join the same Wi-Fi network as your computer (Station mode). AIM robots only join **2.4 GHz**
networks, with a name and password of up to 20 characters each, and no sign-in pages.

- **Already on your Wi-Fi?** On the robot, open Settings → Radio → Station to see its address, and put it in `AIM_HOST`.
- **New robot, or a new network?** Put the robot on its own hotspot (Settings → Radio → Access Point on its screen), and
  join its `AIM-…` Wi-Fi on your Mac (the password is on the robot's screen). Then open the control panel's Wi-Fi menu,
  pick your network and press **Switch…**. While the Mac is on the robot's hotspot it has no internet, so your AI
  assistant can't reply, but the panel keeps working.

![The Wi-Fi networks menu, with nearby networks found by a scan](docs/images/wifi.png)

[docs/setup.md](docs/setup.md) has the details: classrooms with many robots, competitions at other venues, and what
macOS asks permission for.

## First steps

Try asking your assistant:

- "Connect to the robot and tell me its battery."
- "Open the control panel."
- "What can you see?"
- "Put my name on the robot's screen in blue."
- "The robot is on the floor with space around it. Scan around and tell me what's there."
- "Fetch the ball and score in the blue goal."

The assistant only moves the robot after you tell it the robot is on the floor with space around it.

## The control panel

Ask your assistant to open it, or run it on its own with `vex-aim-panel --host 192.168.1.50`. It shows:

- the camera, with or without the robot's AI drawn over it;
- a map;
- three tabs, **Explore**, **Teams** and **Strategy**;
- along the top: the robot's Wi-Fi, battery, team, match timer, motion lock and a STOP button.

Its buttons and log use your app's name, e.g. **Point out to ChatGPT**. [docs/control-panel.md](docs/control-panel.md)
walks through it.

## The assistant's tools

About forty tools, in groups: see, real time, move, games, plays, explore and strategy, teams, express and connection.
[docs/tools.md](docs/tools.md) lists them, with the conventions the assistant follows (directions, bearings,
headings) and how it uses what you do in the panel.

## Safety

- **Motion starts locked.** The assistant unlocks it only after you confirm the robot is on the floor with space
  around it. You can also unlock it in the panel.
- **Caps:** speed is capped at 60% and a single move at 1 m, by default.
- **Every move is bounded** by a distance or angle and a timeout, and stops on a bump. This matters, because the robot
  doesn't reliably stop by itself if Wi-Fi drops mid-move ([VEX issue #20](https://github.com/VEX-Robotics/AIM_Websocket_Library/issues/20)).
- **STOP** in the panel, or asking the assistant to stop, ends any move, play or experiment.
- **Driver mode** in the panel stops the assistant's movement tools while you drive.
- **No password:** anyone on the same Wi-Fi can control an AIM robot. Use a network just for the robots where you can.
  In HTTP mode, anyone with the server's secret address can too: keep it private.
- **One program at a time:** ask the assistant to disconnect before using VEXcode or another program with the robot.

## Try it without a robot

```bash
uvx --from git+https://github.com/dbbudd/VEX-AIM-MCP vex-aim-sim --world arena
```

This simulates a robot on a 1.2 × 2.4 m soccer pitch, with goals, AprilTags and a ball that rolls and bounces. Set
`AIM_HOST` to `127.0.0.1:8899` to use it. `--world goals` is a simpler fixed scene, and `vex-aim-arena --robots 4`
(run the same way) puts four robots on one pitch.

## Settings

Set these in the `env` part of your app's configuration:

| Variable | Default | Meaning |
|---|---|---|
| `AIM_HOST` | `192.168.4.1` | The robot's address (the default is its own hotspot) |
| `AIM_MAX_SPEED_PERCENT` | `60` | Cap on driving and turning speed (100% is 200 mm/s or 180°/s) |
| `AIM_MAX_MOVE_MM` | `1000` | Longest single move |
| `AIM_IDLE_DISCONNECT_MIN` | `10` | Let go of the robot after this many idle minutes (0 = never) |
| `AIM_LIVE_VIEW_PORT` | `8765` | The control panel's port |
| `AIM_YOLO_MODEL` | `yolo26n.pt` | YOLO weights (downloaded on first use) |
| `AIM_PANEL_SETUP` | see below | Where the panel saves its set-up |
| `AIM_HTTP_PORT` | `8000` | HTTP mode: the port |
| `AIM_HTTP_SECRET` | random | HTTP mode: the secret part of the address |

The panel saves measurements, AprilTag meanings, the field, the team list and remembered network names (never
passwords) in `~/Library/Application Support/VEX AIM Panel/` on a Mac. Remembered Wi-Fi passwords are kept in the
Mac's keychain.

## Troubleshooting

- **"Host is down" or it can't connect:** the robot sleeps after a few idle minutes. Tap its screen to wake it, and
  check it's on the same network as your computer.
- **Its address changed:** the router gives it one, and it can change. Check Settings → Radio → Station on the robot,
  or use 🔍 Find robots in the panel's Teams tab.
- **The tools don't appear in your app:** check the path to `uvx`, then run the `--version` command above to see any
  error.
- **Another program is using the robot:** only one program can drive it. Ask the assistant to disconnect first.

## Contributing

[AGENTS.md](AGENTS.md) explains the code, how to run the tests, and the rules for changing it. It's written for people
and for AI coding agents (Codex, Copilot, Cursor, Claude Code and others) alike. [CLAUDE.md](CLAUDE.md) adds notes
for Claude Code.

## Credits

- The robot's protocol follows VEX's [AIM WebSocket library](https://github.com/VEX-Robotics/AIM_Websocket_Library)
  and [documentation](https://api.vex.com/aim/home/websocket/index.html).
- Ideas from earlier projects: [flashzdw/VEX-AIM-MCP](https://github.com/flashzdw/VEX-AIM-MCP),
  [touretzkyds/vex-aim-tools](https://github.com/touretzkyds/vex-aim-tools) and
  [robotmcp/ros-mcp-server](https://github.com/robotmcp/ros-mcp-server).

This project isn't affiliated with or endorsed by VEX Robotics. VEX and VEX AIM are trademarks of Innovation First, Inc.

## License

[MIT](LICENSE)
