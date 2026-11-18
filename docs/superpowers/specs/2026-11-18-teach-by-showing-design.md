# Teach-by-showing — design

**Date:** 2026-11-18
**Status:** approved

## Goal

The user teaches EDITH a new task by doing it once. "Edith, watch me" starts recording. "Edith, done, call it Monday report" stops it. Later, "Edith, do Monday report for March" replays the task, even if windows have moved. The replay can also fill in new values, such as a different month.

No consumer assistant learns a named, reusable, voice-callable skill from a single demonstration. This feature is EDITH's distinguishing edge.

## Scope

In scope: recording mouse and keyboard, compiling the recording into readable steps with Claude, replaying with fallbacks, variables, and list and forget commands.

Out of scope for now: compiling offline, loops or conditionals inside taught skills, and editing a taught skill by voice. The JSON can be edited by hand.

## Components

All of the new code goes in `skills/teach.py`. It adds one dependency, `pynput`.

### 1. Recording

- `start_teaching()` is a skill. It starts `pynput` mouse and keyboard listeners in background threads. If a recording is already running, it says so.
- The recorder captures these events, each with a monotonic timestamp:
  - **click**: button, absolute x/y, the active window title, the window rectangle, and a 120×120 PNG crop centred on the click. The crop is grabbed at the moment of the click.
  - **key**: key name and the modifiers held. Afterwards, `group_keys(events)` merges the keys into `type` events (plain text) and `keys` events (combinations such as `ctrl+s`, `enter`, `tab`).
  - **focus**: emitted when the active window title changes. The title is polled every 0.3 s.
- Events are dropped when they fall on EDITH's own overlay window, or on a window whose title contains `password`, `sign in` or `log in` (best effort). In the second case, typed text is not stored.
- The wake word and speech-to-text keep running, so "done" and "cancel" are heard.
- `cancel_teaching()` stops the listeners and discards the recording.

### 2. Compiling

- `finish_teaching(name: str)` stops the listeners and sends the grouped events, with their crops, to Claude.
- This needs a new method on `ClaudeRemote`: `ask_images(images: list[bytes], prompt: str, max_tokens) -> str`. The existing `describe_image` forces short spoken answers, which does not suit this use.
- Claude returns JSON in this form:
  ```json
  {"description": "Fill and save the weekly sales report",
   "variables": ["month"],
   "steps": [
     {"focus": "Excel"},
     {"click": "Save button", "crop": "3.png", "rel": [0.82, 0.05]},
     {"type": "{month} sales"},
     {"keys": "ctrl+s"},
     {"wait": 2}
   ]}
  ```
  `rel` is the click position as a fraction of the window rectangle, so it still works if the window moves or is resized.
- `parse_compiled(text)` checks the JSON. It accepts only these step types: `focus`, `click`, `type`, `keys`, `wait`. Any other step means the compile fails.
- The result is saved to `data/taught/<slug>/skill.json`, with the crops alongside as `<n>.png`. The slug is the name in lowercase, with characters other than `a-z0-9` replaced by `-`.
- **Offline or Claude error.** EDITH says "I need internet to learn this, sir" and keeps the recording in memory. Saying `finish_teaching` again retries.

### 3. Replay

- `do_taught(name: str, values: str = "")` uses `confirm=True`.
- `values` holds `key=value` pairs separated by `;`, for example `month=March`. If the skill has exactly one variable, a bare value is accepted.
- Every `{var}` in `type` steps is filled in. A missing variable fails the run before any step executes, and EDITH names the variable she needs.
- **focus** uses the existing `screen.focus_window(app)`. If no window matches, the run fails with a spoken reason.
- **type** and **keys** use `pyautogui`, the same way the existing `type_text` and `press_keys` skills do.
- **click** looks for its target in this order:
  1. `pyautogui.locateOnScreen(crop)`, an exact image match. It works offline and is fast.
  2. Claude vision, through `ask_images([screenshot, crop], "Where is this element? Reply x,y")`. The coordinates are scaled back from the thumbnail size.
  3. The window rectangle combined with `rel`.
- **wait** sleeps for the given number of seconds, up to a maximum of 10.
- The `pyautogui.FAILSAFE` setting stays on. Moving the mouse into a screen corner aborts the run, and EDITH reports which step it stopped at.

### 4. Management

- `list_taught()` returns the taught skill names and their descriptions.
- `forget_taught(name)` uses `confirm=True` and deletes `data/taught/<slug>/`.
- On load and after each save or forget, the description in the `do_taught` registry schema is rewritten to list the taught skill names and descriptions. Claude can then map "do my Monday report" to the right skill.

## Data flow

voice → agent → `start_teaching` → listeners collect events → voice "done, call it X" → `finish_teaching` → `group_keys` → Claude `ask_images` → `parse_compiled` → `data/taught/x/skill.json` → registry description refresh.

voice "do X for March" → agent → `do_taught(X, "month=March")` → confirm → steps executed → spoken result.

## Testing

`tests/test_teach.py` covers the following, with no real input hooks or network:
- `group_keys`: plain characters merge into `type`, modifier chords become `keys`, and backspace edits the pending text.
- `parse_compiled`: valid JSON passes, while unknown step types and malformed JSON are rejected.
- Variable filling: `key=value` pairs, a bare single value, and an error for a missing variable.
- Click fallback order, with `locateOnScreen`, vision and the window rectangle faked: an exact match wins, vision is used next, `rel` is the last resort.
- Slug generation and the registry description refresh.
