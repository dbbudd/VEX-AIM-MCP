# Tools

This page lists the tools VEX AIM MCP gives your AI assistant, and the conventions behind them.

You don't need to learn the tools' names. Ask in plain English, and the assistant picks the tools. They're the same in
every AI app that supports MCP. Installing and settings are in the [README](../README.md).

The assistant only moves the robot after you tell it the robot is on the floor with space around it. See
[Safety](#safety).

## What to ask

| You might say | The assistant might use |
|---|---|
| "Connect to the robot and tell me its battery." | `connect_robot` |
| "What can you see?" | `look` |
| "The robot is on the floor with space around it. Scan around and tell me what's there." | `enable_motion`, then `scan_surroundings` |
| "Fetch the ball and score in the blue goal." | `score_goal`, or `fetch_ball` then `shoot_at_goal` |
| "Tell me when the ball comes into view." | `wait_for` |
| "Show a happy face and play a tune." | `show_emoji`, `play_notes` |
| "Should we pass or shoot?" | `advise` |

## The tools

Each tool has a name, which the assistant uses, and a title, which your AI app may show when it asks for your
permission. The tools come in nine groups.

### See

| Tool | Title | What it does |
|---|---|---|
| `robot_status` | Robot status | Battery, heading, position, tilt, whether it's moving or playing a sound, what the kicker seems to hold, and what the robot's onboard AI sees right now. Also what you've set up in the control panel. |
| `look` | Look through the camera | Takes a photo with the front camera, for the assistant to see. Draws a bearing ruler, and boxes around what the onboard AI recognises. Can add YOLO's labels too. |
| `detect_objects` | Detect objects | Lists what the robot can see, with bearings, without a photo. From the onboard AI, YOLO, or both. Can switch on AprilTag detection first. |
| `control_panel` | Control panel | Opens or closes the [control panel](control-panel.md), a web page on your computer at http://127.0.0.1:8765. |

YOLO is a bigger object detector that runs on your computer and knows 80 kinds of everyday object (person, cup,
bottle, chair…). It's an install option: see [Options](../README.md#options) in the README.

### Real time

| Tool | Title | What it does |
|---|---|---|
| `wait_for` | Wait for something to happen | Watches the robot about 10 times a second and returns the moment something happens: an object comes into view, leaves or gets close, someone touches the screen, the robot is bumped, or you do something in the control panel. Gives up after 30 s by default (300 s at most). |
| `teach_colour` | Teach a colour | Teaches the robot's onboard AI a colour, from a box on a `look` photo, with a name such as "red cup". The robot then tracks it by itself. It holds up to 7 colours, like the colour signatures in VEXcode's AI Vision Utility. |

### Move

Every tool here except `enable_motion`, `stop` and `reset_position` needs motion unlocked.

| Tool | Title | What it does |
|---|---|---|
| `enable_motion` | Enable motion | Unlocks motion until the robot disconnects. The assistant only calls it after you confirm the robot is on the floor with space around it. |
| `move` | Move | Drives a set distance in any direction, then stops. It slides sideways without turning. Up to 1000 mm at a time by default, and it stops early on a bump. |
| `turn` | Turn | Spins on the spot by an angle. |
| `turn_to_heading` | Turn to heading | Spins on the spot to face a heading. |
| `face_object` | Face an object | Finds an object with the onboard AI and turns to face it. If it's not in view, turns in 45° steps (up to a full circle) to look for it. |
| `scan_surroundings` | Scan surroundings | Turns a full circle, noting the heading of every object the onboard AI recognises and where each goal is centred, then faces the starting direction again. |
| `approach_object` | Approach an object | Drives up to an object it can see, re-aiming before every short step, until the object is in the kicker. Gives up after driving 400 mm in total, by default. |
| `kick` | Kick | Fires the kicker (`soft`, `medium` or `hard`), then backs away 80 mm. |
| `stop` | Stop | Stops all motion at once, including a play, exploration or experiment. Always allowed, even while motion is locked. |
| `reset_position` | Reset position | Makes the current heading 0° and the current position (0, 0). It doesn't move the robot. |

### Games

| Tool | Title | What it does |
|---|---|---|
| `set_team` | Set team | Chooses the robot's team, blue or orange. A team scores in the goal of its own colour. |
| `shoot_at_goal` | Shoot at goal | Scores with the ball the robot is holding, from where it stands: finds the goal, aims between the posts, kicks, backs away and returns a photo. |

### Plays

Plays are short jobs the robot does by itself, planned with the control panel's map. All of them except `advise` move
the robot.

| Tool | Title | What it does |
|---|---|---|
| `fetch_ball` | Fetch the ball | Gets the ball into the kicker by itself. Unlike `approach_object`, the ball needn't be in view: it checks the map, or turns to look around. |
| `score_goal` | Score a goal | Scores from anywhere: fetches the ball, finds the goal, dribbles closer if needed, lines up between the posts and kicks. |
| `pass_ball` | Pass the ball to a spot | Kicks the ball to a spot on the field with the gentlest measured kick that gets there. Refuses if something on the map is in the ball's way. |
| `go_to` | Go to a spot | Drives to a spot around obstacles and anything on the map, finishing within a few cm. It can face a heading at the end. |
| `guard_goal` | Guard the goal | Plays goalkeeper for 30 s by default (120 s at most): stands 25 cm in front of the goal and slides sideways to stay between the ball and the goal's centre. |
| `advise` | What should the robot do? | Advice without moving: fetch, shoot, pass or dribble, and why, with the numbers behind it. |

### Explore and strategy

| Tool | Title | What it does |
|---|---|---|
| `explore_arena` | Explore the arena | Maps the arena: scans a full circle, then drives a grid across the field chosen in the control panel, scanning at each spot. Takes a few minutes. |
| `test_kick` | Test a kick | An experiment: kicks the ball straight ahead and watches it roll, to measure how far that strength sends it. |
| `record_kick_distance` | Record a kick distance | Saves a kick distance someone measured, for example after the ball rolled out of the camera's view. |
| `test_speed` | Test driving speed | An experiment: drives straight ahead a measured distance (500 mm by default) and times it, to find how fast the robot drives. |

### Teams

| Tool | Title | What it does |
|---|---|---|
| `team_list` | Team list | Every robot on the team list: its player's name, team, address, whether it's connected, battery, and where it is on the field. Also which robots the selected one can see, and whether each is a teammate, an opponent or unknown. |
| `select_player` | Choose which robot to control | Makes another player's robot the one the tools and the control panel act on. |
| `add_player` | Add a robot to the team list | Adds another AIM robot by its player name (up to 16 characters) and address, optionally with a team. |

### Express

| Tool | Title | What it does |
|---|---|---|
| `react` | React on the screen | Shows the player card on the robot's screen: the player's name in the team colour, and a small face that celebrates or sulks (happy, excited, sad, wink, surprised or focused). Its lights match the team colour. After 5 s, by default, it goes back to the happy face. |
| `set_lights` | Set lights | Sets one or all of the robot's six lights to a colour name or a hex code like #FF8800. |
| `show_emoji` | Show emoji | Shows one of the robot's built-in animated faces, looking forward, left or right. |
| `show_text` | Show text | Writes short text on the robot's 240×240 screen, in four sizes. Long lines wrap. |
| `play_sound` | Play sound | Plays one of the robot's built-in sounds, such as `tada` or `cheer`. |
| `play_notes` | Play notes | Plays a tune, such as `C5:400 E5:400 G5:800` (notes and their lengths in ms). Up to 30 seconds. |
| `say` | Say | Speaks through the robot's speaker, using the Mac's text-to-speech. About 15 seconds at most. Mac only. |

### Connection

| Tool | Title | What it does |
|---|---|---|
| `connect_robot` | Connect to the robot | Connects, or reconnects. The other tools connect by themselves, so this is only needed to switch robots or to recover after the connection dropped. |
| `disconnect_robot` | Disconnect from the robot | Lets go of the robot and closes the control panel, so VEXcode or another program can use the robot. The tools reconnect automatically. |

There's no Wi-Fi tool. The assistant can't change the robot's network or see a Wi-Fi password: only the control panel
and the Mac's keychain handle them ([setup.md](setup.md)).

## Conventions

The server describes these to the assistant, in its instructions and in each tool's description.

- **Move direction:** relative to the robot's front: 0 forward, 90 right, 180 back, 270 left. Its omni-wheels let it
  slide sideways without turning.
- **Turns:** positive is clockwise (right), negative is anticlockwise (left).
- **Heading:** 0 to 360°, clockwise. 0 is the way the robot faced when it connected, or at `reset_position`.
- **Bearings:** reported in degrees left (−) or right (+) of straight ahead; the camera sees about ±34°. Turning by an
  object's bearing faces it. `look` draws a bearing ruler on the photo so the assistant can turn toward anything it
  sees, not just what the robot's onboard AI recognises.
- **Position:** in millimetres. y is forward and x is to the right of the heading-0 direction.
- **On a field:** when a field is chosen in the control panel, positions are field coordinates. (0, 0) is the
  field's centre, x is to the right and y is up the field.
- **Speed:** a percentage. 100% is 200 mm/s when driving, and 180°/s when turning.
- **Reconnecting** resets the heading and position to 0. The tool's answer tells the assistant when that happened.
- **Objects:** the onboard AI recognises sports balls, blue and orange barrels, other AIM robots and AprilTags.
  - Tools that look for something take a kind (`sports_ball`, `blue_barrel`, `orange_barrel`, `aim_robot`,
    `apriltag` or `any`), or the `label` of something you named in the control panel or a taught colour.
  - AprilTags only show up once tag detection is on. Tools that look for a tag switch it on themselves.
  - A bigger box usually means closer.

## Fetching and kicking a ball or barrel

1. `scan_surroundings`, then `turn_to_heading` to the object. (`face_object` also finds it and turns to it.)
2. `approach_object`, which re-aims before each short step until the object is in the kicker.
3. `look` to confirm it's held. In dim light the onboard AI often loses a ball once it's right in front, so the photo
   is the reliable check. If the onboard AI can't confirm it, `approach_object` returns a photo itself.
4. `kick`.

The kicker holds balls and barrels with a magnet, so `kick` backs the robot away 80 mm straight afterwards. Otherwise
a ball that rolls or bounces back gets caught again. Aim kicks at open space, and use `soft` on a table.

For the ball, `fetch_ball` does steps 1 and 2 by itself, even when the ball isn't in view.

## Goal games

- A goal is two barrels of the same colour, and the ball has to pass between them.
- Choose the robot's team with `set_team`, or in the control panel. As with VEX alliances, a team scores in the goal
  of its own colour. So the blue team scores in the blue goal, and guards the orange one.
- `scan_surroundings` lists every barrel and where each goal is centred.
- **To shoot from where the robot stands:** fetch the ball (`approach_object`, then `look`), then call
  `shoot_at_goal`. It finds the goal, aims at the real midpoint between the posts, kicks, backs away and returns a
  photo.
- **To score from anywhere:** `score_goal` fetches the ball, dribbles closer if no measured kick would score from
  there, lines up and kicks with the gentlest measured strength that scores. It only uses kick strengths measured
  with `test_kick`. With none, it dribbles to 30 cm and kicks soft.
- **To defend:** `guard_goal` stands in front of the goal the other team scores in, and never slides past a post. It
  ends early if it catches the ball.

Try: "We're on the blue team. Fetch the ball and score."

## Plays, exploring and experiments

- **One at a time.** Plays, exploring and the experiments run by themselves for a while. If the robot is already busy
  with one, a new one is refused until the first finishes or is stopped.
- **The map.** The control panel builds the map while it runs, so a play or exploration opens the panel if it's
  closed. `explore_arena` maps the field: it drives around obstacles and anything already on the map, and stops on a
  bump. Without a field chosen in the panel, it only scans where it is.
- **Experiments** measure what the robot can do:
  - `test_kick` measures how far each kick strength sends the ball. If the ball rolls out of view, the assistant asks
    you to measure where it stopped, then calls `record_kick_distance`.
  - `test_speed` times a drive. It needs clear floor straight ahead for the whole distance, and by default drives
    back to the start.
- **Using the results.** The panel saves the results, and `robot_status` reports them. `advise`, `score_goal` and
  `pass_ball` use them. A kicked ball is usually far faster than the robot can drive.
- **Spots on the field.** `pass_ball` and `go_to` take a spot in mm (see [Conventions](#conventions)). `go_to`
  refuses spots off the field or within 10 cm of its walls.

Try: "Explore the field and tell me where the goals are." Or: "Measure how far a soft kick goes."

## Safety

**The assistant only unlocks motion after you confirm the robot is on the floor with space around it.** Robots often
sit on a table or a charger.

- **Motion lock.** Motion starts locked on every connection, and on each robot in a team. The tools that drive the
  wheels or the kicker refuse to run until the assistant calls `enable_motion`. The server tells the assistant to
  only do that after you confirm the robot is on the floor with space around it, and never on its own initiative. You
  can also unlock motion in the control panel.
- **Tools that need motion unlocked:** `move`, `turn`, `turn_to_heading`, `face_object`, `scan_surroundings`,
  `approach_object`, `kick`, `shoot_at_goal`, the plays (`fetch_ball`, `score_goal`, `pass_ball`, `go_to`,
  `guard_goal`), `explore_arena`, `test_kick` and `test_speed`.
- **Stop always works.** `stop`, or STOP in the control panel, works even while motion is locked. It ends any move,
  play, exploration or experiment, whoever started it.
- **Caps.** Speed is capped at 60% and each move at 1000 mm, by default. You can change both in the
  [settings](../README.md#settings).
- **Bounded moves.** Every move and turn has a fixed distance or angle and waits until it finishes. The server sends a
  stop if the move takes much longer than expected, bumps into something, is cancelled, or the server shuts down.
  - This matters because the robot does not reliably stop by itself if Wi-Fi drops mid-move
    ([VEX issue #20](https://github.com/VEX-Robotics/AIM_Websocket_Library/issues/20)).
- **Longer jobs** (plays, exploring, kick and speed tests) also need motion unlocked, stop on a bump, and run one at a
  time.
- **Small steps.** The server tells the assistant to explore in small steps (about 300 mm or less) and to `look`
  before driving into unseen space.
- **Kicks.** Aim at open space, never toward people or fragile things, and use `soft` on a table.
- **Driver mode.** While you drive in the control panel's Driver mode, the assistant's movement tools refuse to run.
  Switch back to Auto to let the assistant drive.
- **Permission prompts.** Many AI apps ask before each tool call, unless you choose to always allow a tool. A sensible
  classroom setting is to allow the read-only tools and keep asking for the rest.
  - The read-only tools only look and report: `robot_status`, `look`, `detect_objects`, `wait_for`, `team_list` and
    `advise`.
  - The server also tells your AI app which tools these are. It marks them as read-only, and marks the tools that
    drive the wheels or the kicker with MCP's "destructive" hint. Apps that read these hints can treat them
    differently.
- **No password.** The robot's control connection has no password, so anyone on the same Wi-Fi can control it. Use a
  dedicated network for classroom robots ([setup.md](setup.md)).
- **One program at a time.** Ask the assistant to disconnect (`disconnect_robot`) before you use VEXcode, a script or
  a standalone control panel with the robot.

## How the assistant uses the control panel

The control panel is for you: [control-panel.md](control-panel.md) walks through it. What you do there is shared with
the assistant.

- **`robot_status`** has a `panel` part with your team, labels, taught colours, the last thing you pointed out, and
  the map.
  - On a field, positions are field coordinates and it gives the robot's own position.
  - It also lists AprilTags with their roles and what you said each one means, and obstacles with their distance and
    bearing from the robot.
  - It has the player's name and team, and what the experiments measured.
- **Labels and colour names** work as the `label` argument of `approach_object`, `face_object` and `wait_for`, e.g.
  "approach the red cup".
- **`wait_for`** watches about 10 times a second and returns the moment an object appears, leaves, gets close, the
  screen is touched, the robot is bumped, or you do something in the panel. That lets the assistant ask "point at the
  one you want" and react as soon as you click.
- **`teach_colour`** lets the assistant teach the robot a colour from something it spotted in a `look` photo. You can
  also teach one in the panel, by dragging a box over the object.
- **The log.** Every tool call the assistant makes appears in the panel's log.
- **The team list.** When you show another robot in the panel, the assistant's tools follow it, as with
  `select_player`.

Try: "Wait until I point at something in the control panel, then face it."

## See also

- [README](../README.md): installing, settings, and trying it without a robot.
- [setup.md](setup.md): getting robots onto Wi-Fi.
- [control-panel.md](control-panel.md): the control panel, screen by screen.
