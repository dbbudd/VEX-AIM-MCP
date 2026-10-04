# Status and known gaps

This page says what has been tested, on the simulators and on a real robot, and what the known gaps are.

For what the project does and how to install it, see the [README](../README.md).

## Automated tests

- There are ten test suites in `tests/`. They run against the simulators only, never a real robot.
- As of version 0.1.0, all ten pass.
- [tests/README.md](../tests/README.md) lists them and shows how to run them.
- Some newer features have only been tested on the simulators so far. They haven't been tried on a real robot yet:
  - driving in Driver mode with a game controller
  - the soccer plays
  - exploring
  - the kick and speed tests
  - pairing a second robot
  - switching a robot's Wi-Fi network

## On the simulators

Every tool has been run end to end over MCP. That covers movement, the motion lock, YOLO, the live view and error
handling.

- **Control panel:** 29 checks passed. They cover teams, labels, pointing things out to the assistant, teaching a
  colour, `wait_for`, STOP, and refusing commands from other websites. Clicking through the page in a browser also
  worked.
- **AprilTags, field markers and the field set-up:** 22 more checks passed.
  - A pinned marker located the robot to within 1 cm of the expected spot.
  - Everything came back after a restart.
- **Exploring and the experiments (the kick and speed tests):** 22 checks passed in the arena world, a simulated
  pitch.
  - A soft kick was measured by the camera. A medium one that rolled out of view was measured by hand.
  - The speed test found 12 cm/s at 60%.
  - STOP ended a scan.
  - Exploring visited all 8 spots on the pitch and mapped both goals.
  - The player name reached the simulated robot's screen.
- **Newer tools for the assistant:** 17 checks passed over MCP, including `stop` ending an exploration midway.

## On a real robot

### Without movement

- 39 of 39 checks passed: status, camera, YOLO, lights, emoji, text, sound, speech, live view and reconnecting.
- The robot's AI read all four of the kit's AprilTag markers (ids 0–3) at once.
- With tag detection on, the camera slowed from about 7 to about 4 fps.
- On home Wi-Fi, the camera streams about 7 fps (about 17 KB per frame). The live view with YOLO runs about 4 fps.

### Movement

Supervised, at 30% speed.

- These all completed:
  - 50 mm moves forward, back, right and left
  - 90° turns each way
  - absolute turns to 45° and 0°
- Distance was within about 3%.
- Turns landed within 1–2.5°.
- Turning stays on the spot, with under 2 mm of drift.
- Position `y` is forward, and `x` is to the right of the heading-0 direction.

### Objects

Supervised, on a desk.

- `scan_surroundings` mapped the ball and barrels. Two sightings of the ball from different headings agreed to
  within 1°.
- `approach_object` put the blue barrel in the kicker in two steps.
- A soft `kick` placed the barrel upright about 15 cm ahead, and the robot backed away.
- `approach_object` on the ball tracked it for two 60 mm steps. Then it drove the last 120 mm on an estimate from
  the ball's size, and seated the ball in the kicker. The photo it returned confirmed this.
  - The onboard AI and YOLO both fail to recognise this ball in roughly the last 10 cm. That's why it drove the
    last part on an estimate.
  - An earlier scripted run showed why `kick` backs away: a plain soft kick let the ball roll back onto the magnet.

### Goal game

Also supervised, on a desk. Two blue and two orange barrels were set up as goals, and the robot scored in the blue
goal. It used separate tools for each step, not the newer soccer plays.

- `scan_surroundings` found both goals, with the blue posts 35° apart.
- `shoot_at_goal` aimed at the blue goal's centre with both posts in view, and kicked soft. The ball rolled between
  the posts.
- A second full cycle also scored: scan, fetch the ball with `approach_object`, turn back to the goal, then
  `shoot_at_goal`.
- On that run, the robot's AI couldn't see the blue barrels against a bright window. So the assistant read their
  positions off the `look` photo's ruler instead. That run was done with Claude Code.

## Known gaps

### Not yet tried on a real robot

The newer features listed under [Automated tests](#automated-tests), and also:

- Stopping on a bump. No collision was attempted.
- `face_object`'s search turn.
- Medium and hard kicks.
- Teaching a colour and `wait_for`. Both passed on the simulators.

### Other known gaps

- **Onboard AI false positives:** blue items in the background were sometimes reported as a blue barrel.
- **Screen text:** `show_text` lays text out using estimated font sizes.
