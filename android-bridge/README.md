# Bastion Android Bridge

Minimal Android app that handles `bastion://connect?...` links, asks for confirmation, copies the generated command, then invokes GitHub/F-Droid Termux through the official `RUN_COMMAND` intent.

## What it does

- Registers `bastion://connect?...`
- Parses Bastion link parameters
- Builds the same SSH/tmux command as the web panel
- Shows a confirmation screen
- Copies the full command to clipboard
- Calls Termux `RUN_COMMAND`
- Opens the Termux UI afterwards

## Requirements

- GitHub or F-Droid Termux, package name `com.termux`
- In Termux: `~/.termux/termux.properties` must contain:
  - `allow-external-apps = true`
- In Android settings:
  - Grant this app the additional permission `Run commands in Termux environment`
- Recommended for best UX:
  - In Termux app info, allow `Draw over other apps`

## Build

This repo does not include a Gradle wrapper jar. Open `android-bridge/` in Android Studio and let it sync, or add a wrapper locally if you prefer CLI builds.

## Notes

- The app is marked `excludeFromRecents` and finishes quickly after handoff.
- No accessibility service or background keepalive is used.
