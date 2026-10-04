"""Per-version Responsa GUI configs.

Every control ID, dialog title, and class name the automation layer relies
on is specific to one installed build of RESPONSA.exe. Keeping that data
in a dedicated module per version (rather than inline in automation.py)
means supporting a future Responsa version should only require adding a
new `vNN.py` here with its own (likely mostly-similar) values -- the
automation logic in automation.py should not need to change.
"""
