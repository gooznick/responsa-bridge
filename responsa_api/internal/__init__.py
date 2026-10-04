"""Implementation details of responsa_api -- not part of the public API.

Everything a caller needs is importable from the package root
(`from responsa_api import ResponsaClient, BookScope, ...`). What lives here
is the GUI automation that makes it work: the Win32 helpers (winutil), the
per-version control-ID map (versions/), the PDF parser (parsing), and the
automation driver itself (automation). Nothing here is a stable interface.
"""
