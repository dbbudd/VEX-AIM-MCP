# CLAUDE.md

@AGENTS.md

## Notes for Claude Code

- This session may also have the `vex-aim` MCP server connected (tools like `robot_status` and `look`). Those tools
  run the code as it was when the server started. After changing the server, it needs restarting (a new session,
  or reconnecting the server) before the tools change.
- Look at the control panel in the browser pane. For a dev panel, start a simulator and a panel as background Bash
  jobs with the longest timeout (Claude Code stops background jobs at their timeout), then open
  http://127.0.0.1:8766:
  `vex-aim-sim --world arena` and `vex-aim-panel --host 127.0.0.1:8899 --port 8766 --no-browser --no-yolo`.
  Port 8765 belongs to the live panel for a real robot.
- The person may be using the same browser tab while you test. Log entries you didn't cause are probably theirs.
- Ask before anything that reaches outside the computer or can't be undone: pushing, switching a real robot's
  Wi-Fi network, or changing someone's Claude configuration.
- Never move a real robot without the person's confirmation in this conversation (see Safety rules above), and never
  trigger the macOS Location or keychain prompts on the person's behalf: let them press **📡 Scan** or
  **🔑 From this Mac** themselves.
