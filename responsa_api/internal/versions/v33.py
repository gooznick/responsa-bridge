"""GUI map for Responsa Project version 33 (RESPONSA.exe, ProductVersion 33.0.0.0).

Everything here was discovered by hands-on, read-only probing of the live
app (see scripts/inspect_app.py and scripts/dump_tree.py) -- window
class names via win32gui.EnumWindows/EnumChildWindows, control IDs via
win32gui.GetDlgCtrlID, and dialog text via win32gui.GetWindowText. None of
it is documented anywhere; there is no public API/SDK for this app.

If a future version of Responsa needs supporting, copy this file to vNN.py,
re-run the discovery scripts against that version, and update the values
that changed -- automation.py should not need to change, it only reads from
whichever version module is selected.
"""

EXE_PATH = r"C:\Program Files (x86)\ResponsaCD33\RESPONSA.exe"
WORKING_DIR = r"C:\Program Files (x86)\ResponsaCD33"
PROCESS_IMAGE_NAME = "RESPONSA.exe"

# --- Main window -------------------------------------------------------
# MDI app: MAIN_WINDOW_CLASS holds an MDIClient housing one child window
# per open search-results window.
MAIN_WINDOW_CLASS = "ResponsaProject"

# The visible toolbar (Search/Windows/Browse/Tools/... row) is a
# third-party component (ini names it "RespTB33-ToolBarManager") that does
# its own internal mouse hit-testing rather than standard message-based
# click handling. CONFIRMED UNCLICKABLE by every automation technique
# tried: simulated hardware clicks (SetCursorPos+mouse_event, and
# pywinauto's click_input/SendInput), message-based clicks
# (WM_LBUTTONDOWN/UP via both SendMessage and PostMessage, to both the
# button and its parent AfxControlBar110u toolbar), WM_COMMAND sent with
# the button's own GetDlgCtrlID() value, and MSAA accDoDefaultAction (its
# DefaultAction is empty). Do not try to click these buttons -- use
# COMMAND_* below instead, sent as a raw WM_COMMAND to the main window,
# which goes through MFC's real command-routing path regardless of the
# toolbar's own click handling. (UIPI, DPI scaling and sandboxing were
# all ruled out before finding this.)
#
# The main window DOES have a real (if normally hidden) classic Win32
# menu bar underneath the toolbar skin -- win32gui.GetMenu(main_hwnd)
# returns a valid, non-null menu with full submenus, dumped via
# GetMenuItemInfo/GetSubMenu. COMMAND_* values below are real menu item
# command IDs from that dump, confirmed live via WM_COMMAND (both
# COMMAND_OPEN_SEARCH and COMMAND_CLOSE_ALL_WINDOWS were sent to the main
# window and visibly took effect, with no dependency on window focus or
# foreground state).
COMMAND_OPEN_SEARCH = 32857          # menu: חיפוש (top-level item, direct command)
COMMAND_OPEN_BROWSE = 32781          # menu: עיון (top-level item, direct command)
COMMAND_CLOSE_ALL_WINDOWS = 32854    # menu: חלון -> סגור הכל (Ctrl+X)
COMMAND_EXPANDED_RESULTS = 32801     # menu: תצוגה -> תוצאות מורחבות (Ctrl+B)
COMMAND_CONDENSED_RESULTS = 32802    # menu: תצוגה -> תוצאות מקוצרות

# --- Search dialogs ------------------------------------------------------
# Sending COMMAND_OPEN_SEARCH reveals whichever search-mode dialog was
# last used. The app actually preloads all four modes simultaneously
# as hidden sibling #32770 dialogs (only one shown at a time); each one
# carries the same set of nav buttons on its right sidebar to switch modes
# without closing the dialog. NAV_ADVANCED is the one we automate against.
SEARCH_DIALOG_TITLES = {
    "free_form": "חיפוש בניסוח חופשי",
    "easy": "חיפוש קל",
    "tabular": "חיפוש טבלאי",
    "advanced": "חיפוש מתקדם",
}

NAV_FREE_FORM_BUTTON_ID = 1189
NAV_EASY_BUTTON_ID = 1207
NAV_TABULAR_BUTTON_ID = 1208
NAV_ADVANCED_BUTTON_ID = 1209
NAV_DATABASES_BUTTON_ID = 1210   # opens the "ניהול המאגרים" book-selection dialog

# Controls inside the "חיפוש מתקדם" (Advanced Search) dialog.
ADVANCED_QUERY_EDIT_ID = 1233
ADVANCED_SEARCH_ALL_DBS_CHECKBOX_ID = 1024   # "חיפוש בכל המאגרים"
ADVANCED_SUBMIT_BUTTON_ID = 1                 # "בצע חיפוש" (standard IDOK)
ADVANCED_CANCEL_BUTTON_ID = 2                 # "ביטול" (standard IDCANCEL)
ADVANCED_CLEAR_BUTTON_ID = 1062               # "ניקוי"

# --- Book/corpus scope presets for search()'s `books` parameter ----------
# Maps each BookScope enum value (see book_scope.py) to the exact tree-node
# text(s) to check in the sources tree (see scripts/sources_tree.txt).
# "shas"/"gemara" intentionally map to the same node, per the user's
# explicit choice that Gemara should just be a synonym for
# Shas rather than also including Yerushalmi. "tora" has no single
# matching node -- תנ"ך's direct children are the individual books, so
# it's the union of the five Torah books' leaf nodes.
BOOK_SCOPE_NODE_TEXTS = {
    "shas": ["תלמוד בבלי"],
    "gemara": ["תלמוד בבלי"],
    "tora": ["בראשית", "שמות", "ויקרא", "במדבר", "דברים"],
    "bible": ['תנ"ך'],
    "mishna": ["משנה"],
    "chazal_literature": ['ספרות חז"ל'],
    "rambam": ['רמב"ם ומפרשיו'],
    "shut": ['(ספרי שאלות ותשובות (שו"ת'],
    "old_shut": ["ספרי שאלות ותשובות - ראשונים"],
    "new_shut": ["ספרי שאלות ותשובות - אחרונים"],
}
# BookScope.ALL is handled specially (via the "search all databases"
# checkbox, ADVANCED_SEARCH_ALL_DBS_CHECKBOX_ID) rather than a tree node.

# If the tree ends up with nothing checked when "אישור" (OK) is clicked,
# Responsa shows this warning instead of just reopening/erroring
# silently. Distinct from the "מידע"/Info dialogs (INFO_DIALOG_HEADING_TEXT
# above) -- this one's heading is "אזהרה" (Warning).
NO_DATABASES_SELECTED_HEADING = "אזהרה"
NO_DATABASES_SELECTED_MARKER = "בחר במאגר אחד לפחות"
NO_DATABASES_SELECTED_DISMISS_BUTTON_ID = 1  # "אישור"

# --- Book / corpus selection ---------------------------------------------
# NAV_DATABASES_BUTTON_ID opens "ניהול המאגרים", which hosts a SysTabControl32
# with (at least) a nested "עץ מקורות" dialog/page containing the checkbox
# tree. The tree dialog is NOT top-level -- it's nested, so finding it
# requires walking descendants, not just EnumWindows' top-level results.
DATABASE_MANAGER_DIALOG_TITLE = "ניהול המאגרים"
SOURCES_TREE_DIALOG_TITLE = "עץ מקורות"
SOURCES_TREE_CONTROL_CLASS = "SysTreeView32"
SOURCES_TREE_CONTROL_ID = 1002

TREE_SELECT_ALL_BUTTON_ID = 1137     # "בחר הכל" -- selects every book
TREE_GROUPS_BUTTON_ID = 1138         # "קבוצות מאגרים" -- named presets (not yet explored)
TREE_START_OVER_BUTTON_ID = 1136     # "התחל שוב"
TREE_CATALOG_BUTTON_ID = 1139        # "קטלוג"
TREE_OK_BUTTON_ID = 1649             # "אישור" -- confirm selection
TREE_CANCEL_BUTTON_ID = 1651         # "ביטול" -- discard selection

# Tree items are lazily populated: a node's .children() returns nothing
# until the node has been sent a TVM_EXPAND message (pywinauto's
# `_treeview_element.expand()`), even though this is a read-only data
# query, not a visual UI action.

# --- Generic "מידע" (Info) message-box dialogs after submitting a search --
# Confirmed live: both the zero-hits and too-many-hits outcomes surface as
# this same generic dialog shape -- EMPTY title, a Static (id 1216, text
# literally "מידע") as heading, a Static (id 1027) with the actual message,
# and Yes/No/Cancel-ish buttons depending on which case it is. Distinguish
# by substring-matching the message text, not by title (there isn't one).
INFO_DIALOG_HEADING_TEXT = "מידע"

# "לא נמצאה כל תוצאה!\nשים לב: החיפוש מוגבל לספרים.\nהאם ברצונך לחפש בכל
# המאגרים?" ("No result found! Note: search is limited to books. Do you
# want to search all databases?") -- clicking "לא" (No) declines
# broadening the search and returns cleanly to the search dialog with
# nothing else to close (no results-summary dialog, no results window).
NO_RESULTS_MARKER = "לא נמצאה כל תוצאה"
NO_RESULTS_DECLINE_BUTTON_ID = 1255  # "לא"

# "נמצאו מעל 32000 תוצאות. נא לפשט את החיפוש או לצמצם צורות" ("Found more
# than 32000 results. Please simplify the search or narrow forms.") --
# clicking "ביטול" (Cancel) aborts back to the search dialog cleanly,
# without ever generating the (enormous, slow) actual result set.
TOO_MANY_RESULTS_MARKER = "נמצאו מעל"
TOO_MANY_RESULTS_ABORT_BUTTON_ID = 1256  # "ביטול"

# --- Invalid query syntax ---------------------------------------------------
# Distinct from the "מידע" (Info) dialogs above -- this one has an actual
# title, "שגיאה בהגדרת השאילתה" (Error in query definition). Confirmed
# live: typing a word containing nikud (vowel points) into
# the query box triggers this, message "מילה יכולה להכיל אותיות עבריות
# וספרות בלבד." ("a word may contain only Hebrew letters and digits").
# Clicking "אישור" (OK) returns cleanly to the search dialog so a
# corrected query can be submitted.
INVALID_QUERY_DIALOG_TITLE = "שגיאה בהגדרת השאילתה"
INVALID_QUERY_DISMISS_BUTTON_ID = 1  # "אישור"

# --- Post-search summary dialog -------------------------------------------
# After a search completes, a dialog titled "<N> תוצאות" (e.g. "8560
# תוצאות") appears with a genuine SysListView32 breakdown of result counts
# per book/database -- this one IS readable via standard ListView APIs,
# unlike the actual results window. Must click OK to proceed to the
# results window itself.
RESULTS_SUMMARY_TITLE_SUFFIX = "תוצאות"
RESULTS_SUMMARY_LISTVIEW_ID = 1052
RESULTS_SUMMARY_LISTVIEW_CLASS = "SysListView32"
RESULTS_SUMMARY_OK_BUTTON_ID = 1

# --- Results window --------------------------------------------------------
# Custom/owner-drawn MDI child, e.g. class
# "Afx:00400000:b:00010003:00000006:000203E0". No standard text control and
# no useful UI Automation tree (confirmed: only generic Pane/ScrollBar/
# TitleBar nodes come through, no text). Title is "נמצאו <N> תוצאות
# <range>" or "נמצאה תוצאה אחת <range>" for a single hit.
RESULTS_WINDOW_CLASS_PREFIX = "Afx:00400000:b"

# --- Browse ("עיון") --------------------------------------------------------
# COMMAND_OPEN_BROWSE (above) opens a MODELESS dialog, so a plain
# SendMessage returns; found live by dumping its controls. It
# closes itself as soon as a text is opened from it. NOTE the leading
# space in the title -- the exact string is " עיון".
BROWSE_DIALOG_TITLE = " עיון"
# A SysTabControl32 with (at least) the tabs "עץ מקורות" (the sources
# tree, the one automated here) and "כתיבת מקורות" (type an exact
# reference -- the input that did not accept a raw citation copied from
# a search result). The tab last used stays selected between openings,
# but it is selected explicitly anyway.
BROWSE_TAB_CONTROL_ID = 12320
# The "עץ מקורות" tab is index 0 (index 1 is the other tab, which has no
# tree). Selected by index because pywinauto can't read this tab
# control's titles (its text query raises "Only 0 tabs available").
BROWSE_SOURCES_TAB_INDEX = 0
# The tree on that tab (the only SysTreeView32 in the dialog). NOT the
# same tree as the search dialog's sources tree: the top-level names
# differ slightly, e.g. '(תנ"ך (החומש מחולק לפרקים' here. It goes down to
# individual verses/pages (Tanakh: book -> chapter -> verse); a node
# with no "+" (TVITEM.cChildren == 0) is a leaf. Children are populated
# lazily on TVM_EXPAND.
BROWSE_TREE_CLASS = "SysTreeView32"
# Double-clicking a leaf (a REAL, hardware-simulated double click -- see
# browse_gui.TreeReader.double_click) opens JUST that leaf's own text
# (e.g. one verse, one Mishnah -- not its containing chapter) in a new
# MDI window of the same class as a results window
# (RESULTS_WINDOW_CLASS_PREFIX). The "הצג טקסט" button (id 1649) is the
# button equivalent, unused -- untested whether it has the same
# real-vs-message-click requirement.
BROWSE_SHOW_TEXT_BUTTON_ID = 1649
BROWSE_START_OVER_BUTTON_ID = 1136

# --- Fetching one result's full text ("Skip to number" + open) ------------
# Confirmed live, via the same read-only classic-menu dump
# COMMAND_OPEN_SEARCH etc. came from: "תצוגה" (View) -> "דלג למספר" ("Skip
# to number") is a real menu command. Nearby on the same menu but not
# used here: 32785 = "דלג לספר" (Skip to book, Ctrl+G), 36155 = "דלג
# למחבר" (Skip to author).
COMMAND_SKIP_TO_NUMBER = 32808

# The "Skip to instance" dialog COMMAND_SKIP_TO_NUMBER opens. Confirmed
# live: entering a number and confirming only repositions the results
# window (its title's visible range shifts, e.g. "1-6" -> "3-6") -- it
# does NOT open the item by itself. An out-of-range number (e.g. beyond
# the search's total hit count) pops a further "מספר המופע חייב להיות
# בין 1 ל-<N>" error dialog on top of this one -- not handled here since
# automation.py validates the range itself first, using the total hit count
# already known from the search, before ever opening this dialog.
SKIP_TO_NUMBER_DIALOG_TITLE = "דילוג למופע"
SKIP_TO_NUMBER_EDIT_ID = 1018
SKIP_TO_NUMBER_OK_BUTTON_ID = 1
SKIP_TO_NUMBER_CANCEL_BUTTON_ID = 2

# Right-click on a results window offers a display-density option;
# "Expanded" (multi-line snippet per hit) persists automatically as the
# default for future result windows once set once, per the user -- so the
# API does not need to manage it via the ini file. Prefer sending
# COMMAND_EXPANDED_RESULTS as a defensive WM_COMMAND before extraction
# (same reasoning as COMMAND_OPEN_SEARCH/COMMAND_CLOSE_ALL_WINDOWS above);
# the Ctrl+B keystroke below is kept only as a documented fallback since it
# was the originally-discovered path (confirmed to work by hand, not yet
# re-verified as a simulated keystroke against this specific command).
EXPANDED_RESULTS_SHORTCUT = "^b"

# Extraction: WM_GETTEXT and UI Automation both dead-end on the results
# window (see above), and Ctrl+A/Ctrl+C only grabs whatever page is
# currently scrolled into view. Ctrl+P -> Print (Microsoft Print to PDF)
# -> Save-As is the only method found that captures the FULL result set
# regardless of scroll position (verified: a 33-result query produced all
# 33 entries across 4 PDF pages). The print dialog is a classic dialog;
# the save dialog is a modern Explorer-style common dialog, which
# automates differently (type the full path into its filename box rather
# than driving classic controls) -- not yet verified live.
PRINT_SHORTCUT = "^p"

# The Print dialog's own title -- confirmed live, literally "Print" in
# English regardless of the app's Hebrew UI (same pattern as
# SAVE_DIALOG_TITLE below). Used to positively identify it rather than
# assuming "whatever new #32770 just appeared" is the Print dialog: a
# results-summary dialog (title ending in RESULTS_SUMMARY_TITLE_SUFFIX)
# can legitimately finish rendering as a new top-level window at almost
# exactly the same moment, purely coincidentally, and would otherwise get
# mistaken for it.
PRINT_DIALOG_TITLE = "Print"

# The Windows common Save dialog that "Microsoft Print to PDF" opens.
# Its title is in English regardless of the app's Hebrew UI (confirmed
# live, consistently). This dialog's top-level window gets destroyed and
# recreated (a NEW hwnd) shortly after first appearing -- confirmed live:
# an hwnd captured when the dialog was first detected turned out stale by
# the time its child controls were searched moments later, while a fresh
# by-title lookup at that same moment found a different (correct, fully
# populated) hwnd. Always re-resolve this dialog by title right before
# acting on it; never hold onto a captured hwnd across a wait/poll.
SAVE_DIALOG_TITLE = "Save Print Output As"
# Its "&Save" button -- a standard Win32 "Button", confirmed live
# (read-only control dump of an open instance). Clicked via
# BM_CLICK instead of pressing Enter, which needs the dialog to hold real
# keyboard focus: confirmed live, while the user's editor kept reclaiming
# the foreground, the dialog visibly flickered between gaining and losing
# focus until the Enter's 15s retry window ran out -- leaving the dialog
# open and blocking every later call.
SAVE_DIALOG_SAVE_BUTTON_ID = 1

# Print dialog page-range controls -- standard Windows common-dialog
# (comdlg32 PrintDlg) IDs, not Responsa-specific. Confirmed live
# by dumping the Print dialog's control tree. Used to cap how
# many pages actually get rendered/printed for a `max_hits`-limited
# search, since printing a genuinely huge result set is what makes export
# slow (see MIN_HITS_PER_PAGE and the time_budget handling in automation.py).
PRINT_RANGE_ALL_RADIO_ID = 1056       # "&All"
PRINT_RANGE_PAGES_RADIO_ID = 1058     # "Pa&ges"
PRINT_RANGE_SELECTION_RADIO_ID = 1057  # "&Selection"
PRINT_RANGE_FROM_EDIT_ID = 1152       # "from:"
PRINT_RANGE_TO_EDIT_ID = 1153         # "to:"

# --- Error dialogs -----------------------------------------------------
# Two distinct dialog families were observed for a missing/faulty license
# dongle, both fail-fast (app self-terminates once dismissed, no
# click-through recovery). A well-behaved launcher should treat ANY
# unexpected dialog appearing between process start and MAIN_WINDOW_CLASS
# showing up as a launch failure, rather than hardcoding just these two --
# there may be others not yet observed.
ERROR_DIALOG_TITLE_MALFUNCTION = "תקלה"          # e.g. Error #5 (bad CWD), Error #9 (I/O, missing dongle)
ERROR_DIALOG_TITLE_APPLICATION_ERROR = "Application Error"  # e.g. "please connect the removable disk"

# Windows' own generic "application not responding" ghost dialog, not a
# Responsa dialog at all -- can appear (and become visible) briefly while
# the app is still starting up, and must not be mistaken for a real
# launch-error dialog. Untitled #32770 with a single Static child of
# exactly this text plus a "Terminate the process" button.
HANG_DETECTOR_TEXT = "Loading. Please wait..."
