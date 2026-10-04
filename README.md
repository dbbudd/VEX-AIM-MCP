# VEX AIM MCP

Let Claude drive a [VEX AIM](https://www.vexrobotics.com/aim) coding robot. Ask in plain English, and Claude drives
it over Wi-Fi, sees through its camera, uses its lights, screen, speaker and kicker, and plays soccer. A live
control panel in your browser shows everything the robot sees and lets you drive, set up teams and run matches.

![The control panel: live camera with the robot's AI overlaid, the Explore/Teams/Strategy tabs and the map](docs/images/panel.png)

```
Claude ──MCP──▶ vex-aim-mcp ──Wi-Fi──▶ VEX AIM robot
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
- Claude Code (in the Claude desktop app's Code tab, or the `claude` command) or Claude Desktop.
- [uv](https://docs.astral.sh/uv/), which installs and runs the server. In Terminal:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- A Mac is best. The Wi-Fi tools and speech use macOS. The rest should work on Windows and Linux, but isn't tested there.

## Install

First check it installs, and find where `uvx` lives (Claude may not find it on its own):

```bash
uvx --from git+https://github.com/dbbudd/VEX-AIM-MCP vex-aim-mcp --version
```

```bash
which uvx
```

In the examples below, replace `/Users/you/.local/bin/uvx` with what `which uvx` printed, and `192.168.1.50` with your
robot's address.

### Claude Code: the desktop app's Code tab

Put a file called `.mcp.json` in your project folder:

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

### Claude Code: the `claude` command

```bash
claude mcp add vex-aim --scope user --env AIM_HOST=192.168.1.50 -- uvx --from git+https://github.com/dbbudd/VEX-AIM-MCP vex-aim-mcp
```

`--scope user` makes it available in every project. Leave it out to add it to the current project only.

### Claude Desktop

In Claude Desktop, open Settings → Developer → Edit Config, and add the same server to `claude_desktop_config.json`:

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

Then quit and reopen Claude Desktop.

### Options

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
  pick your network and press **Switch**. While the Mac is on the robot's hotspot it has no internet, so Claude can't
  reply, but the panel keeps working.

[docs/setup.md](docs/setup.md) has the details: classrooms with many robots, competitions at other venues, and what
macOS asks permission for.

## First steps

Try asking Claude:

- "Connect to the robot and tell me its battery."
- "Open the control panel." In the desktop app, ask Claude to show it in the browser pane.
- "What can you see?"
- "Put my name on the robot's screen in blue."
- "The robot is on the floor with space around it. Scan around and tell me what's there."
- "Fetch the ball and score in the blue goal."

Claude only moves the robot after you tell it the robot is on the floor with space around it.

## The control panel

![The Wi-Fi networks menu, with nearby networks found by a scan](docs/images/wifi.png)

Ask Claude to open it, or run it on its own with `vex-aim-panel --host 192.168.1.50`. It shows:

- the camera, with or without the robot's AI drawn over it;
- a map;
- three tabs, **Explore**, **Teams** and **Strategy**;
- along the top: the robot's Wi-Fi, battery, team, match timer, motion lock and a STOP button.

[docs/control-panel.md](docs/control-panel.md) walks through it.

## Claude's tools

About forty tools, in groups: see, real time, move, games, plays, explore and strategy, teams, express and connection.
[docs/tools.md](docs/tools.md) lists them, with the conventions Claude follows (directions, bearings, headings)
and how Claude uses what you do in the panel.

## Safety

- **Motion starts locked.** Claude unlocks it only after you confirm the robot is on the floor with space around it.
  You can also unlock it in the panel.
- **Caps:** speed is capped at 60% and a single move at 1 m, by default.
- **Every move is bounded** by a distance or angle and a timeout, and stops on a bump. This matters, because the robot
  doesn't reliably stop by itself if Wi-Fi drops mid-move ([VEX issue #20](https://github.com/VEX-Robotics/AIM_Websocket_Library/issues/20)).
- **STOP** in the panel, or asking Claude to stop, ends any move, play or experiment.
- **Driver mode** in the panel pauses Claude's movement tools while you drive.
- **No password:** anyone on the same Wi-Fi can control an AIM robot. Use a network just for the robots where you can.
- **One program at a time:** ask Claude to disconnect before using VEXcode or another program with the robot.

## Try it without a robot

```bash
uvx --from git+https://github.com/dbbudd/VEX-AIM-MCP vex-aim-sim --world arena
```

This simulates a robot on a 1.2 × 2.4 m soccer pitch, with goals, AprilTags and a ball that rolls and bounces. Set
`AIM_HOST` to `127.0.0.1:8899` to use it. `--world goals` is a simpler fixed scene, and `vex-aim-arena --robots 4`
(run the same way) puts four robots on one pitch.

## Settings

Set these in the `env` part of the configuration:

| Variable | Default | Meaning |
|---|---|---|
| `AIM_HOST` | `192.168.4.1` | The robot's address (the default is its own hotspot) |
| `AIM_MAX_SPEED_PERCENT` | `60` | Cap on driving and turning speed (100% is 200 mm/s or 180°/s) |
| `AIM_MAX_MOVE_MM` | `1000` | Longest single move |
| `AIM_IDLE_DISCONNECT_MIN` | `10` | Let go of the robot after this many idle minutes (0 = never) |
| `AIM_LIVE_VIEW_PORT` | `8765` | The control panel's port |
| `AIM_YOLO_MODEL` | `yolo26n.pt` | YOLO weights (downloaded on first use) |
| `AIM_PANEL_SETUP` | see below | Where the panel saves its set-up |

The panel saves measurements, AprilTag meanings, the field, the team list and remembered network names (never
passwords) in `~/Library/Application Support/VEX AIM Panel/` on a Mac. Remembered Wi-Fi passwords are kept in the
Mac's keychain.

## Troubleshooting

- **"Host is down" or it can't connect:** the robot sleeps after a few idle minutes. Tap its screen to wake it, and
  check it's on the same network as your computer.
- **Its address changed:** the router gives it one, and it can change. Check Settings → Radio → Station on the robot,
  or use 🔍 Find robots in the panel's Teams tab.
- **The tools don't appear in Claude:** check the path to `uvx`, then run the `--version` command above to see any error.
- **Another program is using the robot:** only one program can drive it. Ask Claude to disconnect first.

## Contributing

[AGENTS.md](AGENTS.md) explains the code, how to run the tests, and the rules for changing it. It's written for
people and for AI coding agents alike. [CLAUDE.md](CLAUDE.md) adds notes for Claude Code.

## Credits

- The robot's protocol follows VEX's [AIM WebSocket library](https://github.com/VEX-Robotics/AIM_Websocket_Library)
  and [documentation](https://api.vex.com/aim/home/websocket/index.html).
- Ideas from earlier projects: [flashzdw/VEX-AIM-MCP](https://github.com/flashzdw/VEX-AIM-MCP),
  [touretzkyds/vex-aim-tools](https://github.com/touretzkyds/vex-aim-tools) and
  [robotmcp/ros-mcp-server](https://github.com/robotmcp/ros-mcp-server).

This project isn't affiliated with or endorsed by VEX Robotics. VEX and VEX AIM are trademarks of Innovation First, Inc.

## License

[MIT](LICENSE)
