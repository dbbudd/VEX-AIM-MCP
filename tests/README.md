# Tests

Every test runs against the simulators (`vex_aim_mcp.mock_robot` and `vex_aim_mcp.mock_arena`), never a real robot.
Each starts its own simulators and panel on its own ports, and uses a scratch set-up file, so it doesn't touch yours.

Run them from the repository root, with the package installed (see [AGENTS.md](../AGENTS.md#setting-up-to-develop)):

```bash
python tests/panel_test.py          # Claude's tools + the control panel, over MCP (simulator :8870, panel :8750)
python tests/tags_test.py           # AprilTags, field markers, saving the set-up (:8871, :8751)
python tests/routines_test.py       # kick and speed tests, scan, explore, STOP, player name (:8872, :8752)
python tests/strategy_mcp_test.py   # the explore/strategy/react tools over MCP, Driver-mode refusals (:8873, :8753)
python tests/driver_test.py         # Driver mode: hold-to-drive, stop on release, matches, letting go of the robot (:8874, :8754)
python tests/network_test.py        # Wi-Fi and pairing: find, pair, scan, remember, switch, follow, hotspot (:8875-8876, :8879, :8868, :8755)
python tests/strategy_test.py       # strategy.decide and its helpers, no robot (unit tests)
python tests/plays_test.py          # the soccer plays in the arena world, about 4 minutes (:8880-8885, :8780-8784)
python tests/fleet_test.py          # the team list and several simulated robots (:8890-8892, :8895)
python tests/team_panel_test.py     # the team list in the control panel: add, label, show, save (:8877-8878, :8756)
```

Or all of them (about 10 minutes):

```bash
for t in tests/*_test.py; do python "$t" || echo "FAILED: $t"; done
```

- Each prints PASS/FAIL lines and ends with ALL PASSED (or the failures) and a matching exit code.
- Each test has its own ports, so none collide with another test or with port 8765, a real robot's control panel.
  Still, run them one at a time.
- `network_test.py` keeps its made-up venue passwords in its own keychain entry ("VEX AIM venue Wi-Fi (test)") and
  removes them at the end. It stands in for the Wi-Fi scan and the Mac's saved passwords, so it never triggers a
  macOS permission prompt.
