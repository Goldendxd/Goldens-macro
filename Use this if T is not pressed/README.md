# Golden's Slayers 2 Fishing Macro

This folder contains the Windows macro and its configuration.

## First-time setup

1. Make sure Windows has Python 3.10 or newer installed from [python.org](https://www.python.org/downloads/windows/).
2. Double-click **setup.exe**.
3. Wait for setup to finish.
4. Double-click **Launch.bat** to start the macro.

Setup creates a private `.venv` folder in this directory and installs the packages listed in `requirements.txt`. It does not change your system-wide Python packages.

## Using the macro

- **F6** starts or stops the macro.
- **F7** sets the cast point.
- **F8** closes the macro.
- Keep Roblox visible on the primary monitor.
- Use the calibration controls in the macro window if your screen resolution or UI layout differs.

The existing `config.json` is kept beside `slayers2_fisher.py` intentionally. Do not rename or move either file.

## Discord webhook

The included configuration uses blank webhook and user ID placeholders. Add your own values in `config.json` locally; do not commit real webhook URLs or user IDs.

## Troubleshooting

- If setup says Python cannot be found, install Python and enable **Add Python to PATH**, then run `setup.exe` again.
- If the macro reports a missing package, run `setup.exe` again with an internet connection.
- If Roblox is not detected correctly, use F7 and recalibrate the boxes in the GUI.
- If the macro detects the T prompt but Roblox does not react, check the macro log
  for `keyboard input was rejected by Windows`. If that message appears, run
  Roblox and the macro at the same privilege level and close other keyboard
  automation or overlay programs.
- Close any older copy of the macro before launching this one.

This package does not include Roblox or modify Roblox files. It only sends the configured keyboard and mouse input while it is running.
enojy
