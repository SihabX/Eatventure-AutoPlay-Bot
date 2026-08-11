# Eatventure-AutoPlay-Bot

Eatventure Autobot is a Python-powered automation tool designed for the popular mobile game *Eatventure*. By leveraging advanced computer vision, state-machine logic, and adaptive AI learning, the bot autonomously manages restaurant completions with high precision and human-like interaction patterns.

## Bot Description

The Eatventure Autobot is a sophisticated screen automation tool that interacts with an Android device via `scrcpy`. It uses OpenCV-based image recognition to identify game assets—such as Station Unlocks (Red Icons), upgrade stations, and gift boxes—and sends touch events straight to the device over ADB to progress through the game. Vision reads the mirrored window's own surface rather than the screen region it covers, and input never travels through the PC cursor, so the bot keeps running while the window sits behind your other applications and you carry on using the computer. The bot is designed to be resilient, featuring a robust state machine that handles everything from basic gameplay to complex level transitions and reward collection.

## Features

### State Handlers

The bot's intelligence is built upon a formal **Finite State Machine (FSM)**. Every action is encapsulated within dedicated handlers that manage transitions based on real-time visual feedback:

* **FIND_RED_ICONS**: Scans the screen for actionable red icons.
* **CLICK_RED_ICON**: Executes precise clicks on detected targets with sub-pixel refinement.
* **SEARCH_UPGRADE_STATION**: Locates the active cooking station to apply upgrades.
* **HOLD_UPGRADE_STATION**: Simulates a "long-press" to rapidly purchase upgrades.
* **OPEN_BOXES**: Automatically detects and collects gift box rewards.
* **UPGRADE_STATS**: Manages the secondary stat-boost menu to maximize efficiency.
* **SCROLL**: Executes intelligent, oscillating search patterns when no targets are visible.
* **CHECK_NEW_LEVEL / TRANSITION_LEVEL**: Detects restaurant completion and handles the travel sequence to the next city.

### Priority and Interrupts

The bot gives level transitions priority during normal state processing. Before it commits to most upgrade, box, and red-icon actions, it re-checks for the large **New Level** button or the bottom **Level Complete** indicator so completed restaurants are handled before the next search cycle continues. Box actions use one fresh frame per click so a UI mutation cannot leave a queue of stale coordinates.

### Better Computer Vision

The vision system is built around masked OpenCV template matching with a few practical safeguards:

* **Occlusion-Proof Window Capture**: Frames come from `PrintWindow`, which asks the mirrored window to render its own client area offscreen. Detection is unaffected by windows stacked on top of it, and the window never needs focus or to be raised.
* **Masked Template Matching**: Uses transparent PNG masks so icon shape matching stays stable.
* **Multi-Template Consensus**: Red icons are only trusted after enough template variants agree on roughly the same location.
* **HSV Gate Validation**: Red icons, upgrade stations, and boxes use HSV range gates to reject color-inconsistent candidates.
* **Continuous ByteTrack Asset Tracking**: A background tracker maps red icons, upgrade stations, and boxes while the bot continues moving and acting. It captures through its own private frame source, so it reads the same window the state machine does.

### Background Operation

The bot is built to run while the computer is in use:

* **Covered Window Is Fine**: Other applications may sit fully on top of the scrcpy window; capture still reads the game.
* **Focus Stays Where You Put It**: Starting the bot does not raise or activate the window when occlusion-proof capture is available, and touches go to the device over ADB rather than through the cursor.
* **Minimized Window Pauses, It Does Not Stop**: A minimized window has no surface to render, so the bot releases any held touch, logs the reason once, keeps its workers alive, and resumes the search cycle automatically when the window comes back.
* **Selectable Backend**: `CAPTURE_BACKEND` in `config.py` chooses between `"auto"` (occlusion-proof when possible, otherwise screen capture), `"printwindow"` (occlusion-proof or fail closed), and `"mss"` (always screen capture, which requires an unobstructed window).

### Direct Device Touch Input

All input is injected into the Android device over ADB rather than by moving the PC mouse:

* **Cursor-Free Operation**: Taps, holds, and scroll drags are delivered with `input tap`, `input motionevent`, and `input swipe`, so the physical pointer never moves and you can keep using the computer while the bot runs.
* **Persistent Shell**: A single long-lived `adb shell` handles every command, which removes the per-command round trip; the shell restarts automatically if it dies.
* **Batched Tap Bursts**: Rapid stats-upgrade tapping dispatches taps in background batches so the device overlaps their startup cost and the burst cadence is preserved.
* **Automatic Coordinate Scaling**: Config coordinates stay in scrcpy-window space and are scaled to device pixels at send time, so window resizing does not invalidate them.
* **Device Auto-Detection**: The attached device is selected automatically when exactly one is present; zero or multiple attached devices fail closed unless `ADB_SERIAL` names one.

### Adaptive and Historical Learning

The bot features a self-optimizing AI layer that adapts to your device's performance:

* **Adaptive Tuner**: Automatically monitors success rates and adjusts `CLICK_DELAY` in real-time. If actions are missing, it slows down; if successful, it speeds up to find the "sweet spot" of efficiency.
* **Historical Learner**: Records the time taken for every restaurant completion. Over time, it identifies the most efficient timing profiles and applies them as the "Global Best" configuration, learning the optimal cadence for your specific game progress.

### Better Logging System

A comprehensive logging system tracks every decision the bot makes. It includes:

* **Structured Tracebacks**: Detailed exception handling to prevent crashes.
* **State Persistence**: Historical learning state is saved to JSON files, allowing the bot to retain its "knowledge" even after a restart.
* **Performance Metrics**: Logs completion times and AI "confidence" levels for debugging.

### Safety Boundaries

* The configured title must identify exactly one live window; partial-title and duplicate-title matches are rejected.
* Exactly one ADB device must resolve at startup, and the persistent shell must open before the bot is marked as running.
* Every action revalidates that the target window still exists and is not minimized; a minimized or missing window pauses the run instead of ending it, and the reason is logged.
* Coordinates outside the window bounds and inside forbidden zones are rejected before any touch is sent; scroll drags are additionally sampled along their whole path.
* Stop requests block new input and conservatively release a touch that may still be held down, and every stop is logged.
* Tracker and learner workers must report healthy startup before the bot is marked as running.
* Restarting clears pending targets and returns the finite-state machine to `FIND_RED_ICONS`; so does resuming after a window pause, because targets found before the pause are stale.
* Runtime paths resolve from the project directory, not the shell's current directory.

### Forbidden Zone Configuration

The bot utilizes a refactored **Forbidden Zone Handling** system. Zones are defined in `config.py` using relative coordinates. The bot automatically:

1. Filters out any detections located inside these zones.
2. If a critical asset (like an Upgrade Station) is trapped in a forbidden zone, the bot triggers an **Oscillating Scroll** to move the asset into a safe, clickable area.
3. Prioritizes previously successful red-icon rows so the search tends to revisit productive regions first.

## Requirements

* **Operating System**: Windows or Linux with an X11/XWayland desktop session. Occlusion-proof capture uses the Win32 `PrintWindow` API through `pywin32`, so it is Windows-only; elsewhere the bot logs a warning and falls back to screen-region capture, which requires the window to stay unobstructed.
* **Python**: Use a version supported by the pinned packages in `requirements.txt`; the project has been verified locally with Python 3.11.
* **Android Device**: Connected via USB or Wireless ADB, with **Developer Options** and **USB Debugging** enabled.
* **ADB (Android Debug Bridge)**: The platform-tools package must be installed and `adb` must be on your PATH, or set `ADB_PATH` in `config.py` to the full path to the adb executable.

## Installation Instructions

### Step 1: Install Dependencies

Open your terminal in the project directory and run:

```bash
pip install -r requirements.txt
```

The pins are coupled and should be upgraded together: `supervision` pulls in `scipy`, which requires `numpy>=1.26.4,<2.7.0`, while `opencv-python` 4.13 requires `numpy>=2`. An older NumPy in the environment shows up as `UserWarning: A NumPy version >=1.26.4 and <2.7.0 is required for this version of SciPy` and silently disables ByteTrack asset tracking. If you see that warning, reinstall from this file. Note that `2.7.0` there is SciPy's exclusive upper bound, not a release to install.

### Step 2: Configure scrcpy

1. Download **scrcpy**: [https://github.com/Genymobile/scrcpy](https://github.com/Genymobile/scrcpy)
2. Extract the files and add the executable directory to your `PATH`.
3. Connect your Android device and ensure it is recognized (`adb devices`).
4. Run scrcpy with the specific title used in `config.py`:

```bash
scrcpy --window-title "EatventureAuto"
```

*(Note: Ensure the window title matches the `WINDOW_TITLE` variable in `config.py`)*

Leave the scrcpy window open, but you do not have to keep it in front. With the default `CAPTURE_BACKEND = "auto"` on Windows, the bot renders the window's own client area, so other applications may cover it and it never needs keyboard focus. Only minimizing it pauses the run, because a minimized window has nothing left to render; restoring it resumes automatically.

The window is resized to `WINDOW_WIDTH` x `WINDOW_HEIGHT` at startup, but window borders and the title bar are not part of that space. The bot logs the real client size on startup, for example `client area is 342x733 while configuration assumes 360x780`. Capture and touch coordinates both use the client area, so the two always agree, but coordinates measured against the full window size land a few percent off. Either accept the offset or measure your positions against the logged client size.

The window and capture settings in `config.py`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `WINDOW_TITLE` | `"EatventureAuto"` | Exact scrcpy window title; partial and duplicate matches are rejected. |
| `WINDOW_WIDTH` / `WINDOW_HEIGHT` | `360` / `780` | Requested window size and the coordinate space config positions are written in. |
| `CAPTURE_BACKEND` | `"auto"` | Frame source. `"auto"` uses occlusion-proof `PrintWindow` rendering when the window supports it and falls back to screen capture otherwise; `"printwindow"` requires it and fails closed; `"mss"` always grabs the screen region and needs an unobstructed window. |

### Step 3: Verify Device Input (Optional)

Confirm that exactly one device is attached and that it accepts injected touches:

```bash
adb devices
```

If more than one device is listed, set `ADB_SERIAL` in `config.py` to the serial you want to drive; otherwise startup fails closed rather than guessing. Leave `DEVICE_WIDTH` and `DEVICE_HEIGHT` at `0` to let the bot query the resolution with `wm size`, or pin them explicitly if your device reports an unexpected value.

The remaining touch settings in `config.py`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `ADB_PATH` | `""` | Full path to `adb`; empty resolves it from `PATH`. |
| `ADB_SERIAL` | `""` | Target device serial; empty auto-selects the single attached device. |
| `DEVICE_WIDTH` / `DEVICE_HEIGHT` | `0` | Device resolution; zero queries the device automatically. |
| `TOUCH_DOWN_DURATION` | `0.0` | Seconds to keep a tap pressed. Zero uses the faster single `input tap`; raise it if the game misses taps. |
| `TOUCH_TAP_BATCH_SIZE` | `10` | Taps dispatched per batch during stats bursts. Higher overlaps more device-side startup cost; `1` disables batching. |

### Step 4: Run the Bot

```bash
python main.py
```

The global hotkeys:

| Hotkey | Action |
| --- | --- |
| `Z` | Start or stop the bot |
| `X` | Log the connected device serial and resolution |
| `C` | Wipe learning memory (only while the bot is stopped) |
| `Ctrl+P` | Exit the program |

Exit is the one deliberately awkward binding, because it ends the program. The plain letters only fire when Ctrl is *not* held, so `Ctrl+C`, `Ctrl+X`, and `Ctrl+Z` in your terminal never reach the bot. Startup fails closed if configuration, templates, the exact target window, the ADB device connection, or enabled workers are unavailable.

## Telegram Notification

### Step 1: Create a Telegram Bot

1. Search for `@BotFather` on Telegram.
2. Send `/newbot` and follow the instructions to name your bot.
3. Copy the provided **API Token**.

### Step 2: Get Chat ID

1. Start a chat with your new bot and send any message.
2. Visit `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in your browser.
3. Look for the `"chat":{"id":...}` field and copy the number.
4. Set `TELEGRAM_ENABLED = True` in `config.py`.
5. Set the static credential values in `config.py` before starting the bot:

```python
TELEGRAM_BOT_TOKEN = "replace-with-token"
TELEGRAM_CHAT_ID = "replace-with-chat-id"
```

Telegram's HTTP session does not inherit proxy settings from the process environment.

## Disclaimer

This bot is developed for **educational purposes only**. Using automation tools or scripts may violate the game's Terms of Service and could result in account suspension or banning. Use this software at your own risk. The developers are not responsible for any consequences resulting from the use of this bot.

## License

Eatventure Autobot is open-source software. It is free to use, modify, and distribute for personal and educational use.
