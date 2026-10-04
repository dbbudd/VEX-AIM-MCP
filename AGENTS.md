# AGENTS.md

How this project is built and how to change it, for people and AI coding agents alike. The [README](README.md) is
the user guide. [docs/](docs/) has the control panel, tools, robot set-up and test status.

## The project in one picture

```
AI app ──MCP (stdio, or HTTP)──▶ server.py ──▶ aim_client.AimRobot ──4 WebSockets over Wi-Fi──▶ VEX AIM robot
                                     └── panel.ControlPanel at http://127.0.0.1:8765, in the same process
```

- `server.py` creates one `AimRobot`, one `world.PanelState`, one `ControlPanel` and one `YoloDetector`.
- The control panel runs in-process (the `control_panel` tool) and shares the robot connection and the `PanelState`,
  so the assistant sees what the person does there. It also runs on its own (`vex-aim-panel`), with its own connection
  and state.
- `PanelState` is the shared state. Part of it is saved to `panel_setup.json` in the data folder (`paths.data_dir()`:
  `~/Library/Application Support/VEX AIM Panel` on macOS; env `AIM_PANEL_SETUP` overrides): measurements, tag roles,
  pins, meanings and labels, the field, abilities, player, team, team list, remembered network names and recent
  battery readings. The map is not saved.

## Code map

Everything is in `src/vex_aim_mcp/`. Modules import each other relatively (`from .world import PanelState`). Lowest
layer first, and a module only imports from layers below it: `aim_client`; `world`, `vision`, `screen`;
`routines`, `fleet`; `strategy`; `plays`; `network`; `panel`; `server`. Only `server.py` imports `mcp`, because the
panel has to run without it.

| File | Owns |
|---|---|
| `server.py` | The MCP tools, the `tool` decorator, `require_motion()`, the speed and move caps, and `INSTRUCTIONS` (Claude's guide to the tools) |
| `aim_client.py` | `AimRobot`: connecting, the status loop, one command at a time, bounded moves, `drive_vector`, the camera stream, audio. Also `Detection`, `parse_detections`, `is_held`, `bearing_deg`, `AimError` |
| `world.py` | `PanelState` and its set-up file; distance constants (`FOCAL_PX`, `SIZE_K`), `FIELDS`, `approx_distance_mm`, `cut_off`, `teach_colour` |
| `routines.py` | Jobs the robot does by itself (`scan_here`, `explore_field`, `kick_test`, `speed_test`) and their plumbing: `run`, `cancel`, `progress`, `Stopped`, `speeds`, `plan_route`, `drive_to` |
| `screen.py` | The player card on the robot's screen: name, team colour and a drawn face; `react` |
| `fleet.py` | The team list: several robots, one connection each, teams, field positions, hardware addresses, teammate or opponent |
| `strategy.py`, `plays.py` | Soccer decisions (`decide`: fetch, shoot, pass or dribble; no robot I/O) and the plays that carry them out (`fetch_ball`, `shoot`, `pass_to`, `go_to`, `guard_goal`) |
| `network.py` | Finding robots (a port-80 scan for the AIM set-up page), switching them to another network or their own hotspot, the networks this Mac has joined, scanning nearby networks, and remembered networks (keychain) |
| `panel.py`, `panel.html` | `ControlPanel`: an HTTP server on 127.0.0.1 (the page, `/state.json`, `/stream.mjpg`, `POST /api/<action>` with the page's token), the map (`_update_map`), driver control, matches, pairing and Wi-Fi. The page draws its own overlays |
| `vision.py`, `yolo_worker.py` | The photo's ruler and boxes; `YoloDetector`, which runs YOLO in a subprocess (length-prefixed JPEG in, JSON out) |
| `paths.py` | `data_dir()`: where saved things live, never inside the installed package |
| `mock_robot.py` | The simulator: the same four WebSockets and set-up page, with worlds `barrel`, `goals` and `arena` |
| `mock_arena.py` | Several simulated robots on one pitch (ports from 8899 up) |
| `wifiscan/` | The Swift source of the "AIM Wi-Fi Scan" helper app (see Wi-Fi below) |

Commands (`[project.scripts]` in `pyproject.toml`): `vex-aim-mcp` (the server; `--version`, `--http`), `vex-aim-panel`,
`vex-aim-sim` and `vex-aim-arena`. `python -m vex_aim_mcp` also runs the server.

## Apps and transports

The server isn't tied to one AI app: it's plain MCP, and [docs/clients.md](docs/clients.md) shows how each app adds it.

- **stdio** (the default) is for apps that start the server themselves: Claude Code, Claude Desktop, Codex, VS Code,
  Cursor, Gemini CLI.
- **`--http`** serves streamable HTTP at `http://127.0.0.1:<port>/<secret>/mcp`, for apps that connect to a URL
  (ChatGPT, through a tunnel). It only listens on 127.0.0.1. The secret path (`--secret` / `AIM_HTTP_SECRET`, else
  random each start) is the protection, so the SDK's Host-header check is off: through a tunnel the Host is the
  tunnel's name.
- **The assistant's name:** the `NoteClient` middleware in `server.py` reads the app's `clientInfo.name` from its
  `initialize` request and sets `state.assistant` (Claude, ChatGPT, Codex, Copilot, Cursor, Gemini, Windsurf; else
  "your AI"). The page's `AI()` helper uses it on buttons, hints and the log. Never hard-code an app's name in the
  panel, the tools' descriptions or `INSTRUCTIONS`: write "the assistant", and don't assume a built-in browser
  (`control_panel` has `open_browser` for apps without one).

## Setting up to develop

```bash
uv venv ~/.venvs/vex-aim --python 3.13
uv pip install --python ~/.venvs/vex-aim/bin/python -e ".[yolo]"
```

- Keep the virtual environment **outside** iCloud Drive, Dropbox and similar. iCloud marks dot-folders like `.venv`
  as hidden, and Python 3.13 silently ignores hidden `.pth` files, which breaks the editable install. In a folder
  that isn't synced, `uv sync --extra yolo` works too.
- The editable install means code changes apply the next time a process starts. A running MCP server or panel
  keeps the code it started with.
- To develop with Claude, point a project's `.mcp.json` at the editable install, e.g.
  `"command": "~/.venvs/vex-aim/bin/vex-aim-mcp"` (written out in full), with `AIM_HOST` in `env`.

## Running things

- **Simulator:** `vex-aim-sim --world arena` listens on 127.0.0.1:8899 (`--port` changes it). Worlds: `barrel` (the
  default: one barrel), `goals` (objects at fixed bearings that never get closer: control flow only) and `arena` (a
  1.2 × 2.4 m pitch with real positions, for exploring, kick tests and plays; `--ball-in-kicker` starts holding the
  ball). Point the server at it with `AIM_HOST=127.0.0.1:8899`, or a panel with `--host 127.0.0.1:8899`.
- **Standalone panel:** `vex-aim-panel --host <robot or simulator> --port 8766 --no-browser --no-yolo`. It never
  lets go of the robot by itself; the MCP server does after `AIM_IDLE_DISCONNECT_MIN` idle minutes.
- **Ports:** 8765 is the live panel, for a real robot: never use it for tests or dev panels. 8899 is the simulator's
  default. Each test has its own ports (see [tests/README.md](tests/README.md)); 8766 is free for a dev panel.

## Tests

Simulator only, never a real robot. From the repository root, with the package installed:

```bash
for t in tests/*_test.py; do python "$t" || echo "FAILED: $t"; done
```

- Each test starts its own simulators and panel on its own ports, prints PASS/FAIL lines and exits non-zero on a
  failure. The whole set (eleven files) takes about 10 minutes; `plays_test.py` alone takes about 4.
  `http_test.py` checks HTTP mode the way ChatGPT uses it.
- Tests use scratch set-up files (env `AIM_PANEL_SETUP`) and a separate keychain entry ("VEX AIM venue Wi-Fi (test)"),
  so they never touch a person's saved set-up or passwords.
- Test-only environment overrides: `AIM_SCAN_HOSTS`, `AIM_KNOWN_NETWORKS`, `AIM_KEYCHAIN_SERVICE`,
  `AIM_HOTSPOT_HOST`, `AIM_TEST_MACS`, `AIM_TEST_SCAN` and `AIM_TEST_MAC_PASSWORDS`.
- Check the page's JavaScript after editing `panel.html`:
  `sed -n '/<script>/,/<\/script>/{/<\/*script>/d;p;}' src/vex_aim_mcp/panel.html | sed 's/__RULER__/[]/; s/__FIELDS__/{}/' | node --check -`
  (error line numbers count from the `<script>` tag). Then look at it in a browser against the simulator.

## Conventions

- asyncio throughout. Never block the event loop: use `asyncio.to_thread` for Pillow work and a subprocess for
  anything heavy (YOLO runs in one, because loading PyTorch in-process stalled the robot connection).
- Modules start with `from __future__ import annotations`.
- Every string a person or Claude reads is friendly plain English: say what happened and what to do next, in the
  tone of the existing `AimError` messages. The panel's explanations live in "?" tooltips (`<span class="help"
  data-tip="…">?</span>`) rather than paragraphs; buttons have short labels.
- User-visible problems raise `AimError`. The `tool` decorator turns it into a `ToolError` and logs it. Tools may
  raise `ToolError` directly for bad arguments.
- Tools use `@tool("Title", read_only=..., physical=...)` (`physical=True` if it drives the wheels or the kicker) and
  return plain text, or `[text, Image]` with a photo. Start with `await robot.ensure_connected()`, call
  `require_motion()` before any motion, and end a move with `where_now()`. Keep `INSTRUCTIONS` and
  [docs/tools.md](docs/tools.md) in step.
- One routine at a time: write a job as `async def job(robot, state, ...) -> str` and run it with
  `routines.run(state, name, job(...))`. STOP is `routines.cancel(state)`, which surfaces as `routines.Stopped`.
  Check the motion lock before starting, not halfway through. Report with `routines.progress`, and stop on a bump
  (the `_move` and `drive_to` helpers raise `Stopped`).
- Units are mm and degrees. Speeds are a % of 200 mm/s or 180°/s, capped by `AIM_MAX_SPEED_PERCENT` (use
  `routines.speeds`). Headings are clockwise. Bearings are − for left and + for right.
- The panel log is `state.log(who, text)`, where who is `assistant`, `you` or `robot`. The person's panel actions go
  through `state.action(...)`, which wakes `wait_for("panel")`. Diagnostics go to `logging` (stderr), never stdout:
  stdout is the MCP protocol.
- A new saved setting goes in both `PanelState._load_setup` and `save_setup`. Call `state.save_setup()` after
  changing it.
- A new panel action is a branch in `ControlPanel._do`. An `AimError` there becomes a 409 whose message the person
  sees; a `ValueError` or `KeyError` becomes a 400.
- `panel.html`'s placeholders `__PANEL_TOKEN__`, `__RULER__` and `__FIELDS__` are filled in when the page is served.

## Safety rules (for agents especially)

- **Never send movement or kicker commands to a real robot** unless the person has confirmed, in the current
  conversation, that it's on the floor with clear space around it. Robots get left on tables and on chargers.
  Lights, screen, sound and camera are fine.
- Motion starts locked on each connection. Call `enable_motion` only after that confirmation, never on your own
  initiative.
- `AimRobot._require_motion()` guards every move, turn, `drive_vector` and kick. In the panel's Driver mode, Claude's
  movement tools must refuse: every tool that moves the robot calls `server.require_motion()`, which checks both the
  lock and the mode.
- Every motion is bounded by a distance or angle and a timeout. Continuous driving needs a dead-man stop: the
  panel's drive loop stops the robot 0.35 s after the page stops sending.
- Exploring, experiments and plays need motion unlocked. STOP (the panel's button, or the `stop` tool) cancels them,
  whoever started them. `stop` always works, even while motion is locked.
- Try new movement code on the simulator first, then on a real robot only with the person watching.
- Only one program should drive a robot at a time.
- Don't restart a panel or server that's connected to a moving robot: check `/state.json` (`routine`, `match`,
  `released`) first. Cutting the connection mid-move isn't safe (VEX issue #20).

## Privacy rules

- A robot's set-up page shows its Wi-Fi network **and password** in plain text. Never save its HTML, and never print,
  log, store or commit a Wi-Fi password. `network.py` reads the password only to copy it straight into the keychain
  (`remember_robot_network`), and Claude's tools never handle Wi-Fi passwords: only the panel does.
- `panel_setup.json` holds a robot's address, hardware addresses and network names. It's in `.gitignore`; keep it out
  of the repository, along with real IP addresses, network names and photos of people's homes or classrooms.
- Use example addresses like 192.168.1.50 in docs and tests.

## The robot's protocol and hardware (verified on a real robot)

- Four WebSockets on port 80, with no authentication. `aim_client.py` follows VEX's library
  ([AIM_Websocket_Library](https://github.com/VEX-Robotics/AIM_Websocket_Library), `vex/aim.py`) but doesn't import
  it: that library prints to stdout, calls `sys.exit` and interrupts the main thread.
  - `ws_cmd`: JSON commands as binary frames. Every reply is `{"cmd_id", "status": "complete"}`, and it comes back
    straight away, before a move finishes.
  - `ws_status`: send 0x01 to get a JSON snapshot, about 10 per second: battery %, odometry, IMU, touch and flags.
    Values are strings, and `flags` is hex; 0x400 means a program is connected.
  - `ws_img`: 0x01/0x00 starts/stops a 640×480 JPEG stream of about 7 fps, about 4 fps with AprilTag detection on.
    The client stops streaming about 5 s after the last viewer leaves.
  - `ws_audio`: a 64-byte header plus a WAV or MP3 of at most 255 KB.
  - Connecting sends `program_init`, which tells the robot a remote program has started.
- There's no charging or plugged-in flag, so the panel infers charging from the battery trend. Streaming video can
  drain the battery even on USB power.
- The robot serves its Wi-Fi set-up page at its address in either mode, with no authentication:
  `GET /wifi?flags=1&s1=SSID&p1=PASSWORD` joins a network (Station mode); `GET /wifi?flags=0&a1=0.0.0.0` starts its
  own hotspot (0.0.0.0 means the usual 192.168.4.1).
  - Its form has ids but no names; its script copies each id into the name. Disabled fields aren't sent: Station
    sends only flags, s1 and p1; Access Point sends flags and a1. There's no `save` parameter. It can't scan for
    networks.
  - The robot's own menus and Drive mode only work with no program connected: the panel's `release` and `resume`
    actions let go of it and reconnect.
- The AIM One Stick controller (Bluetooth to the robot) only reaches the robot's built-in Drive mode and VEXcode
  projects running on the robot. Over Wi-Fi, `status.controller` stays all zeros even while it's linked, and VEX's
  library has no controller support. So the panel hands the robot over ("Drive with it…"). Game controllers on the
  computer (the browser's Gamepad API) do work.
- Motion:
  - Bounded moves (`drive_for`, `turn_for`, `turn_to`) wait for the busy flags (`_wait_motion`), with a timeout and a
    stop on a bump. Measured: distance accurate to about 3%, turns to 1-2.5°.
  - Odometry: y is forward and x is right of heading 0, which is the way the robot faced when it connected.
  - The robot does NOT reliably stop by itself if Wi-Fi drops mid-move
    ([VEX issue #20](https://github.com/VEX-Robotics/AIM_Websocket_Library/issues/20)).
  - Continuous (driver) driving uses `spin_wheels` with the omni-wheel mixing of VEX's `move_with_vectors`: x and y
    are % × 2, r is % × 1.8; w1 = 0.5x + 0.866y + r, w2 = 0.5x − 0.866y + r, w3 = r − x (`AimRobot.drive_vector`).
- Kicker: a magnet holds the ball, so kicks back away about 80 mm afterwards. Otherwise a ball that rolls back gets
  caught again.
- Screen: 240×240 behind a round window. The built-in emoji fill the whole screen, so the player card draws its own
  small faces. Mono fonts are assumed to be half as wide as their size (as on the V5 brain).

## Vision and mapping

- The onboard AI uses 320×240 coordinates. The camera sees about ±34°, so the focal length is about 234 px.
- Distance ≈ K / width_px, where K = 234 × the real width (`SIZE_K`). The ball's K is 8700 (calibrated), barrels
  about 9000 (an estimate), robots 234 × 140 mm. AprilTags need measuring in the panel, and one measurement covers
  all tags. Skip boxes cut off at the frame's edge (`cut_off`): their width is wrong.
- A held object sits low and centred (`is_held`).
- The onboard AI often loses the ball in the last ~10 cm, misses backlit barrels, and reports blue things in the
  background as BlueBarrel. Confirm with a photo.
- Detections lag motion. After moving, wait about 0.4 s, or for `robot.fresh_status()`, before trusting them.
- AprilTags (Circle21h7, ids 0-36) only appear once tag detection is on (`robot.set_detection(apriltags=True)`).
- The map is in the robot's odometry frame. The panel's `_update_map` builds it, so it only grows while a panel runs.
  Field coordinates = odometry + a translation-only offset (`state.on_field`), set by "Place robot" or a pinned
  AprilTag marker in view; this assumes the robot started facing up the field.
- The ball and each tag id are unique on the map. Objects need 3 sightings (`MAP_CONFIRM`) to appear. Moving things
  (the ball, robots) get a velocity from a least-squares fit over about 1.5 s.
- A new connection resets heading, position, the motion lock and AprilTag detection, and the panel clears the map
  and the field offset.

## Teams and Wi-Fi

- The panel owns a `Fleet` (`ControlPanel.fleet`); its selected player is the robot the panel shows. The MCP server
  follows it through `panel.on_select`, which rebinds the server's global `robot`. `state.player` and `state.team`
  mirror the selected player. The roster is saved as `fleet` in the set-up file.
- Pairing: Find robots is `net_find`; Pair is `player_pair` (adds, connects, greets on the robot's screen).
- Each player keeps its radio's hardware address (`Player.mac`). `_match_found` recognises a robot at a new address
  with `network.same_radio`, which also matches its hotspot address (one more than its Wi-Fi one), and moves the
  player there. After a switch, `_follow` looks every 6 s for 5 minutes (`state.json`'s `looking_for`).
- macOS hides nearby networks' names from programs without Location Services. `scan_wifi` runs the helper app built
  from `wifiscan/` (Swift: CoreLocation permission, then a CoreWLAN scan) with `open -W -n -g … --args --out <file>`,
  so macOS attributes the permission to the helper, not to Python. `build_scanner` builds it with Xcode's tools into
  the data folder, ad-hoc signed; a rebuild means macOS asks again. `--check` reports the permission without asking.
- `networksetup -listpreferredwirelessnetworks en0` lists the networks this Mac has joined, unredacted, the current
  one usually first.
- `net_remember` with `from_mac` reads the Mac's saved password (System keychain, "AirPort network password"); macOS
  shows its own admin dialog first. Remembered passwords go in the login keychain under "VEX AIM venue Wi-Fi".

## Releasing

1. Bump the version in `pyproject.toml` and `src/vex_aim_mcp/__init__.py`.
2. Run the tests.
3. Commit, tag (`git tag v0.2.0`) and push with tags. People who pin `@vX.Y.Z` in their install pick it up when they
   change the tag; unpinned installs update with `uvx --refresh`.
