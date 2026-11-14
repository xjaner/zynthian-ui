#!/usr/bin/python3
# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Control Device Driver
#
# Zynthian Control Device Driver for "Novation Launchpad X"
#
# Copyright (C) 2015-2025 Fernando Moyano <jofemodo@zynthian.org>
#                         Brian Walton <brian@riban.co.uk>
#
# ******************************************************************************
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of
# the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# For a full copy of the GNU General Public License see the LICENSE.txt file.
#
# ******************************************************************************

import logging
import time
from math import ceil
from time import sleep
from copy import deepcopy
from threading import Thread, RLock

# Zynthian specific modules
from zynlibs.zynseq import zynseq
from zyncoder.zyncore import lib_zyncore
from zyngine.zynthian_signal_manager import zynsigman
from zyngine.zynthian_chain_manager import MAX_NUM_MIDI_CHANS
from zyngine.ctrldev.zynthian_ctrldev_base import zynthian_ctrldev_base, zynthian_ctrldev_zynpad
from zyngine.ctrldev.zynthian_ctrldev_base_ui import ModeHandlerBase
from zyngui import zynthian_gui_config

# ------------------------------------------------------------------------------------------------------------------
# Novation Launchpad X
# ------------------------------------------------------------------------------------------------------------------

# Driver modes
MODE_LAUNCHER = "launcher"
MODE_STEPSEQ = "stepseq"

# Top-row buttons, measured on hardware in Session layout:
#   91-94 arrows, 95 Session/Mixer, 96 Note, 97 Custom, 98 Capture MIDI.
# The arrows are already handled in the CC branch. These three switch the
# device to a layout this driver does not paint for.
CC_SESSION = 95
CC_NOTE = 96
CC_CUSTOM = 97
LAYOUT_BUTTON_CCS = (CC_SESSION, CC_NOTE, CC_CUSTOM)
# Capture MIDI. Not a layout button, so it is not caught by the guard above.
# In stepseq mode it is BACK - the up arrow is needed for step paging.
CC_CAPTURE = 98

# Step sequencer screen layout.
#
# The grid is a piano roll, laid out like the Zynthian's own pattern editor:
#
#     every ROW is an instrument (a row of the chain's keymap)
#     every COLUMN is a step
#
# so 8 instruments x 8 steps are visible at once. The column of buttons to the
# right of the grid is the control strip:
#
#     button 1     play / stop
#     buttons 2-6  velocity brush
#     buttons 7-8  spare
#
# Row indices here are DRIVER rows, which run opposite to physical rows.
# get_note_from_xy() maps note = 10 * (8 - row) + col + 1, therefore:
#
#     row 0 -> notes 81-88 -> the PHYSICAL TOP row
#     row 7 -> notes 11-18 -> the PHYSICAL BOTTOM row
#
# Verified on hardware: sending notes 11-18 lights the bottom row and
# notes 81-88 lights the top row. These constants are therefore counted
# from the physical top, which means LOW driver row numbers.
#
# The Zynthian's piano roll is the other way up: keymap row 0 is the BOTTOM
# (lowest note) and higher rows are higher up. _keymap_index() reconciles the
# two, so the pad shows the same instruments in the same order as the screen.
SS_NUM_ROWS = 8        # Instrument rows visible at once
SS_NUM_COLS = 8        # Step columns visible at once
SS_NUM_VELOCITIES = 5  # Velocity levels offered by the brush
# Steps per beat used to mark the downbeats when the pattern reports none.
SS_BEAT_DIVISION = 4

# The column of buttons to the right of the grid, as CC numbers, in driver
# row order. The launcher mode uses these to trigger phrases; in stepsequencer
# mode they are the control strip.
SS_RIGHT_COL_CCS = (89, 79, 69, 59, 49, 39, 29, 19)
CC_TRANSPORT = SS_RIGHT_COL_CCS[0]
SS_VELOCITY_CCS = SS_RIGHT_COL_CCS[1:6]

# Note-off happens before this many seconds elapse => plain toggle.
# Above it => long press, which selects the note instead of toggling it.
SS_LONG_PRESS_TIME = 0.5

# A long press followed by a pad press within this many seconds is read as a
# drag: the note is extended to cover the new step. A deliberate press of a
# second pad is far slower than this, so it stays a separate press.
SS_EXTEND_WINDOW = 0.25

# Discrete velocity levels offered by the brush, ordered LOW -> HIGH.
# A new note is created at the selected level.
SS_VELOCITY_VALUES = [25, 50, 75, 100, 127]
SS_VELOCITY_DEFAULT = 2  # index into SS_VELOCITY_VALUES

# Playhead polling interval (seconds). The zynseq play position is polled rather
# than taken from a JACK MIDI clock tap so this driver has no hard jack/python-jack
# dependency and no extra child process to manage.
SS_PLAYHEAD_INTERVAL = 0.015

# Every this many playhead polls, the driver also checks whether the screen has
# changed the pattern, the keymap or the sequence under it, and repaints. This
# catches touchscreen edits made while the pattern is stopped.
SS_SYNC_EVERY = 7

# Audition length for a note entered from the pad, in milliseconds per step,
# matching the pattern editor's own play_note().
SS_AUDITION_MS_PER_STEP = 200


class COLORS:
    """Launchpad X palette indices, measured on hardware.

    The palette is organised in groups of four per hue:
        X+0 = the secondary variant, X+1 = full, X+2 = half, X+3 = dim
    Verified by sweeping all 128 velocity values across the grid, e.g.
    5/6/7 red full/half/dim, 9/10/11 orange, 13/14/15 yellow, 17/18/19 green.

    Consequence: there are only THREE usable brightness levels per hue, not five.
    A five-step velocity ramp therefore has to combine brightness and hue into a
    heat scale, which is what VELOCITY_STEP does.
    """

    # Velocity ramp, SOFT -> MAX. Monotonic in perceived intensity.
    #   19 dim green, 18 half green, 17 full green, 13 full yellow, 3 white
    VELOCITY_STEP = [19, 18, 17, 13, 3]
    # Dim marker for the first step of each beat, and for unused pads.
    BEAT_MARKER = 1
    OFF = 0
    # Bright overlay for the column currently under the playhead.
    PLAYHEAD = 3
    # The steps a long note runs through after its first. A dimmer, neutral
    # tone, so a note that spans several steps reads as one note rather than
    # several - and stays distinct from the velocity ramp, which is all green
    # to white.
    NOTE_TAIL = 2
    # Velocity brush buttons (right column)
    VELOCITY_LANE = 18
    VELOCITY_LANE_SELECTED = 3
    # Transport buttons (right column)
    TRANSPORT = 37
    TRANSPORT_ACTIVE = 21


def _migrate_extras(chain):
    """Normalise saved instrument storage to the current ordered form.

    Earlier revisions stored MIDI-learned instruments in a dict keyed by the slot
    they occupied on the top row, and later revisions stored them as an ordered
    "extras" list. The piano roll layout takes its instruments from the chain's
    keymap instead, so there is nothing to keep: the list is dropped.
    """
    chain.pop("pads", None)
    chain.pop("extras", None)
    return chain


# ------------------------------------------------------------------------------------------------------------------
# Marshalling of saved state (stored in the Zynthian snapshot)
# ------------------------------------------------------------------------------------------------------------------
class StepSeqState:

    def __init__(self):
        self._chains = {}
        self._seqs = {}

    def load(self, state):
        state = deepcopy(state)
        self._chains = state.get("chains", {})
        for chain in self._chains.values():
            _migrate_extras(chain)
            chain.setdefault("brush", SS_VELOCITY_DEFAULT)
        for seq, value in state.get("seqs", {}).items():
            self._seqs[int(seq)] = value

    def save(self):
        return {"seqs": self._seqs, "chains": self._chains}

    def get_chain(self, chain_id):
        key = str(chain_id) if chain_id is not None else "default"
        chain = self._chains.get(key)
        if chain is None:
            chain = {"brush": SS_VELOCITY_DEFAULT}
            self._chains[key] = chain
        return chain

    def get_selection_by_sequence(self, seq):
        """The saved cursor position and brush for a sequence.

        The cursor row is remembered per sequence so that re-entering the
        pattern editor lands where it was left. For a melodic chain, whose
        keymap runs to well over a hundred rows, having to scroll back to the
        same place every time is worse than the editing itself.
        """
        value = self._seqs.get(seq)
        if isinstance(value, dict):
            return value
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            # Older revisions stored [instrument_column, brush].
            return {"row": value[0], "brush": value[1], "offset": 0}
        return {"row": 0, "brush": SS_VELOCITY_DEFAULT, "offset": 0}

    def set_selection_by_sequence(self, seq, row, brush, offset=0):
        self._seqs[seq] = {"row": row, "brush": brush, "offset": offset}


# ------------------------------------------------------------------------------------------------------------------
# Playhead monitor. Polls the sequence play position so the moving step can be
# highlighted without requiring MIDI clock access.
# ------------------------------------------------------------------------------------------------------------------
class PlayheadMonitor(Thread):

    def __init__(self, callback, interval=SS_PLAYHEAD_INTERVAL):
        super().__init__()
        self._callback = callback
        self._interval = interval
        self._enabled = False
        self._lock = RLock()
        self.daemon = True
        self.start()

    def set_enabled(self, enabled):
        with self._lock:
            self._enabled = enabled

    def end(self):
        self.running = False

    def run(self):
        self.running = True
        while self.running:
            try:
                with self._lock:
                    enabled = self._enabled
                if enabled:
                    self._callback()
            except Exception as ex:
                logging.error(f"Launchpad X playhead monitor error: {ex}")
            time.sleep(self._interval)


class ArrowRepeater(Thread):
    """Repeats an arrow key while it is held, accelerating as it goes.

    The Launchpad sends one CC when a button is pressed and another when it is
    released, so holding an arrow down produces no further events. A player
    scrolling through thirty instruments would otherwise have to press and
    release for every one, so the repeat is generated here.
    """

    def __init__(self, callback, delay=0.4, interval=0.12, fast_interval=0.04):
        super().__init__()
        self._callback = callback
        self._delay = delay
        self._interval = interval
        self._fast_interval = fast_interval
        self._direction = 0
        self._next = 0.0
        self._current = interval
        self._lock = RLock()
        self.daemon = True
        self.start()

    def start_repeat(self, direction):
        with self._lock:
            if direction and direction == self._direction:
                return
            self._direction = direction
            self._next = 0.0
            self._current = self._interval

    def stop_repeat(self):
        with self._lock:
            self._direction = 0
            self._next = 0.0

    def end(self):
        self.running = False

    def run(self):
        self.running = True
        while self.running:
            time.sleep(0.02)
            with self._lock:
                direction = self._direction
                if not direction:
                    self._next = 0.0
                    continue
                now = time.time()
                if not self._next:
                    # First repeat only after the key has been held a while.
                    self._next = now + self._delay
                    continue
                if now < self._next:
                    continue
                self._next = now + self._current
                if self._current > self._fast_interval:
                    self._current = max(self._fast_interval, self._current * 0.8)
            try:
                self._callback(direction)
            except Exception as ex:
                logging.error(f"Launchpad X arrow repeat error: {ex}")


# ------------------------------------------------------------------------------------------------------------------
# LED output with change tracking.
#
# The Launchpad X keeps a lighting canvas of 81 slots; sending a colour updates
# it. Tracking the desired state locally lets the playhead overlay and restore
# without clearing and repainting the whole surface on every tick.
#
# On the DAW interface the MIDI channel selects the lighting behaviour:
#   channel 0 (0x90/0xB0) => static
#   channel 1 (0x91/0xB1) => flashing
#   channel 2 (0x92/0xB2) => pulsing
# ------------------------------------------------------------------------------------------------------------------
class FeedbackLEDs:

    def __init__(self, driver):
        self._driver = driver
        self._pads = {}
        self._ccs = {}
        self._lock = RLock()

    def pad_on(self, note, colour, channel=0):
        with self._lock:
            if self._pads.get(note) == (colour, channel):
                return
            self._pads[note] = (colour, channel)
        self._driver.send_led_note(note, colour, channel)

    def pad_off(self, note):
        with self._lock:
            if self._pads.get(note) == (COLORS.OFF, 0):
                return
            self._pads[note] = (COLORS.OFF, 0)
        self._driver.send_led_note(note, COLORS.OFF, 0)

    def cc_on(self, ccnum, colour, channel=0):
        with self._lock:
            if self._ccs.get(ccnum) == (colour, channel):
                return
            self._ccs[ccnum] = (colour, channel)
        self._driver.send_led_cc(ccnum, colour, channel)

    def cc_off(self, ccnum):
        self.cc_on(ccnum, COLORS.OFF, 0)

    def force_off(self, notes=(), ccs=()):
        """Turn pads and CCs off unconditionally, ignoring the tracked state.

        The tracked state only knows what THIS layer lit. Launcher mode paints
        through lib_zyncore, so the device can be showing pads that the cache
        believes are already off - and the ordinary pad_off() would then
        suppress the send and leave the launcher's colours lit.
        """
        with self._lock:
            for note in notes:
                self._pads[note] = (COLORS.OFF, 0)
            for ccnum in ccs:
                self._ccs[ccnum] = (COLORS.OFF, 0)
        for note in notes:
            self._driver.send_led_note(note, COLORS.OFF, 0)
        for ccnum in ccs:
            self._driver.send_led_cc(ccnum, COLORS.OFF, 0)

    def all_off(self):
        with self._lock:
            pads, ccs = list(self._pads), list(self._ccs)
            self._pads.clear()
            self._ccs.clear()
        for note in pads:
            self._driver.send_led_note(note, COLORS.OFF, 0)
        for ccnum in ccs:
            self._driver.send_led_cc(ccnum, COLORS.OFF, 0)


# ------------------------------------------------------------------------------------------------------------------
# Step sequencer mode handler
# ------------------------------------------------------------------------------------------------------------------
class StepSeqHandler(ModeHandlerBase):
    """Piano-roll step sequencer.

    The grid mirrors the Zynthian's own pattern editor: every row is an
    instrument (a row of the chain's keymap) and every column is a step, so
    eight instruments and eight steps are visible at once. The column of
    buttons to the right of the grid is the control strip: play/stop, the
    velocity brush, and two spare buttons.

    The instrument rows come from the chain's keymap, which is the authoritative
    list of the notes its patch responds to. Earlier revisions of this driver
    had to guess that list - by scanning the pattern for the notes it held, and
    falling back to a default bank of note numbers - because there was nowhere
    else to get it. Reading the keymap removes the guesswork entirely, and
    makes the pad and the screen agree about which instrument a row is.
    """

    def __init__(self, state_manager, driver, idev_in):
        super().__init__(state_manager)
        self._driver = driver
        self._own_device_id = idev_in
        self._libseq = self._zynseq.libseq
        self._leds = FeedbackLEDs(driver)
        self._saved_state = StepSeqState()
        # Serialises painting between the MIDI thread, the playhead thread and
        # the GUI thread. Only painting and polling take it; nothing that
        # calls into Tk does, since Tk calls from other threads wait on the
        # GUI thread, which may itself be waiting for this lock.
        self._render_lock = RLock()
        self._playhead = PlayheadMonitor(self._update_playhead)
        self._repeater = ArrowRepeater(self._repeat_arrow)

        self._selected_seq = (0, 0)
        self._chain_id = None
        self._brush = SS_VELOCITY_DEFAULT

        # The chain's keymap: the notes the patch responds to, in screen order.
        self._keymap = []
        # Index of the first keymap row shown on the pad. The piano roll is
        # bottom-up and the pad is top-down, so the pad's top row shows the
        # highest keymap row of the window.
        self._row_offset = 0
        # The keymap row and the step the cursor is on.
        self._selected_row = 0
        self._selected_step = 0
        # First step shown in the grid. The window follows the playhead while
        # the pattern plays.
        self._step_offset = 0

        # Presses in flight, keyed by the pad note so that a window moving
        # under a held pad cannot make the release land on the wrong cell.
        self._pressed_cells = {}
        # The note being dragged, set by a long press. None when no drag is in
        # progress.
        self._drag = None
        self._playhead_step = None
        self._is_playing = False
        # What the grid was built from, so that a change made on the screen -
        # another pattern, another scale, another length - is noticed.
        self._keymap_src = None
        self._pattern_index = None
        self._pattern_steps = None
        self._sync_count = 0

    def end(self):
        self._playhead.end()
        self._playhead.set_enabled(False)
        self._repeater.end()

    def set_active(self, active):
        if active:
            super().set_active(True)
            with self._render_lock:
                self._load_current_selection()
                self._refresh_play_state()
                self.refresh(force=True)
            self._playhead.set_enabled(True)
        else:
            # Order matters: once inactive, a render already queued on another
            # thread returns without painting, so nothing lands on top of the
            # launcher after the LEDs below are cleared.
            super().set_active(False)
            self._playhead.set_enabled(False)
            self._repeater.stop_repeat()
            self._pressed_cells.clear()
            self._drag = None
            with self._render_lock:
                # Leaving the editor: remember the cursor, so that coming back
                # to this pattern lands in the same place.
                self._save_selection()
                self._leds.all_off()

    # ----- state ---------------------------------------------------------

    def get_state(self):
        return self._saved_state.save()

    def set_state(self, state):
        if state is not None:
            self._saved_state.load(state)

    def _load_current_selection(self):
        """Bind to whatever the Zynthian screen currently has selected."""
        try:
            seq = (self._zynseq.phrase, self._zynseq.chan)
        except Exception:
            seq = (0, 0)
        self._select_sequence(*seq)

    def _select_sequence(self, phrase, chan):
        self._selected_seq = (phrase, chan)
        self._chain_id = self._get_chain_id_by_sequence(phrase, chan)
        chain = self._saved_state.get_chain(self._chain_id)
        brush = chain.get("brush", SS_VELOCITY_DEFAULT)
        self._brush = brush if 0 <= brush < SS_NUM_VELOCITIES else SS_VELOCITY_DEFAULT
        # The cursor position and the window are remembered per sequence, so
        # re-entering the pattern editor lands where it was left.
        selection = self._saved_state.get_selection_by_sequence(
            phrase * 256 + chan)
        self._selected_row = selection.get("row", 0)
        self._row_offset = selection.get("offset", 0)
        self._step_offset = 0
        self._selected_step = 0
        self._load_keymap()

    def _save_selection(self):
        self._saved_state.get_chain(self._chain_id)["brush"] = self._brush
        self._saved_state.set_selection_by_sequence(
            self._selected_seq[0] * 256 + self._selected_seq[1],
            self._selected_row, self._brush, self._row_offset)

    def _load_keymap(self):
        """Read the chain's keymap from the pattern editor.

        The keymap is the screen's own list of the notes this chain responds
        to, and it is rebuilt whenever the pattern editor loads a pattern. It
        lives on the GUI rather than in libseq, so it is read from there - the
        same access ModeHandlerBase already uses.
        """
        pe = self._pattern_editor()
        keymap = []
        self._keymap_src = None
        if pe is not None:
            try:
                self._keymap_src = pe.keymap
                keymap = list(pe.keymap or [])
            except Exception as ex:
                logging.debug(f"Launchpad X could not read the keymap: {ex}")
        self._keymap = keymap
        self._pattern_index = self._current_pattern_index()
        self._pattern_steps = self._pattern_length()
        self._step_offset = max(0, min(self._step_offset, self._max_step_offset()))
        logging.debug(f"Launchpad X keymap -> {len(self._keymap)} rows")
        if self._keymap:
            self._selected_row = max(0, min(self._selected_row, len(self._keymap) - 1))
        self._scroll_row_window()

    def _sync_with_screen(self):
        """Follow changes made on the screen while the editor stays open.

        The screen-change signal only fires when the screen itself changes,
        so switching patterns, scales or the pattern length inside the editor
        goes unnoticed without this. Returns True if the grid was rebuilt.
        """
        try:
            seq = (self._zynseq.phrase, self._zynseq.chan)
        except Exception:
            seq = self._selected_seq
        if seq != self._selected_seq:
            self._save_selection()
            self._select_sequence(*seq)
            return True
        pe = self._pattern_editor()
        keymap_src = getattr(pe, "keymap", None) if pe is not None else None
        if (keymap_src is not self._keymap_src
                or self._current_pattern_index() != self._pattern_index
                or self._pattern_length() != self._pattern_steps):
            self._load_keymap()
            return True
        return False

    def _current_pattern_index(self):
        try:
            return self._libseq.getPatternIndex()
        except Exception:
            return None

    def _steps_per_beat(self):
        try:
            steps = self._libseq.getStepsPerBeat()
        except Exception:
            steps = 0
        return steps if steps > 0 else SS_BEAT_DIVISION

    @property
    def _brush_velocity(self):
        return SS_VELOCITY_VALUES[self._brush]

    # ----- geometry ------------------------------------------------------

    def _keymap_index(self, visible_row):
        """The keymap row shown in a visible row of the grid.

        The piano roll is bottom-up - keymap row 0 is the lowest note and
        appears at the bottom - while the pad's driver row 0 is the physical
        top. The two are reconciled here, so the pad shows the instruments in
        the same order as the screen.
        """
        return self._row_offset + (SS_NUM_ROWS - 1 - visible_row)

    def _visible_row(self, keymap_index):
        """The visible row of the grid showing a keymap row, or None."""
        row = self._row_offset + SS_NUM_ROWS - 1 - keymap_index
        return row if 0 <= row < SS_NUM_ROWS else None

    def _cell_pad(self, col, row):
        """The grid pad for a step column and a visible row."""
        return self._driver.get_note_from_xy(col, row)

    def _pad_to_cell(self, note):
        """The (column, visible row) a grid pad represents, or None.

        The whole grid is the piano roll now, so any grid pad is a cell.
        """
        col, row = self._driver.get_note_xy(note)
        if col < 0 or col >= SS_NUM_COLS or row < 0 or row >= SS_NUM_ROWS:
            return None
        return col, row

    def _pattern_length(self):
        """Total steps in the pattern, uncapped. 0 when the pattern is empty."""
        try:
            return max(0, self._libseq.getSteps())
        except Exception:
            return 0

    # ----- windows -------------------------------------------------------

    def _max_row_offset(self):
        """Highest row offset that still shows a keymap row."""
        return max(0, len(self._keymap) - SS_NUM_ROWS)

    def _scroll_row_window(self):
        """Keep the selected row inside the visible window."""
        if not self._keymap:
            self._row_offset = 0
            return
        if self._selected_row < self._row_offset:
            self._row_offset = self._selected_row
        elif self._selected_row >= self._row_offset + SS_NUM_ROWS:
            self._row_offset = self._selected_row - SS_NUM_ROWS + 1
        self._row_offset = max(0, min(self._row_offset, self._max_row_offset()))

    def _max_step_offset(self):
        """Highest step offset that still shows a step.

        The window is page-aligned: it shows SS_NUM_COLS steps at a time and
        moves a whole page at a time, so the grid only repaints at a page
        boundary and the playhead sweeps across within a page. Sliding the
        window continuously was tried and was worse - everything in the grid
        was in motion, which made it hard to follow.
        """
        length = self._pattern_length()
        if length <= SS_NUM_COLS:
            return 0
        return ((length - 1) // SS_NUM_COLS) * SS_NUM_COLS

    def _set_step_offset(self, offset):
        """Move the step window. Clamped, so the arrows are safe to mash."""
        self._step_offset = max(0, min(offset, self._max_step_offset()))
        self._render_grid()

    def _select_row(self, row):
        """Move the cursor to a keymap row, scrolling the window and the screen."""
        if not self._keymap:
            return
        self._selected_row = max(0, min(row, len(self._keymap) - 1))
        self._scroll_row_window()
        self._sync_screen()
        # Remember where the cursor is, so coming back to this pattern lands
        # here rather than at the top.
        self._save_selection()
        self._render_grid()

    def _sync_screen(self):
        """Point the on-screen pattern editor at the same cell as the pad.

        The editor owns the scroll position, so the pad's window is pushed to
        it as well and the two show the same instruments.
        """
        pe = self._pattern_editor()
        if pe is None:
            return
        try:
            view_rows = getattr(pe, "view_rows", SS_NUM_ROWS) or SS_NUM_ROWS
            screen_max = max(0, len(self._keymap) - view_rows)
            pe.set_keymap_offset(min(self._row_offset, screen_max))
            pe.select_cell(self._selected_step, self._selected_row)
        except Exception as ex:
            logging.debug(f"Launchpad X could not sync the pattern editor: {ex}")

    # ----- input ---------------------------------------------------------

    def note_on(self, note, velocity, shifted_override=None):
        cell = self._pad_to_cell(note)
        if cell is None:
            return False
        if not self._is_active:
            return True
        col, row = cell
        step = self._step_offset + col
        index = self._keymap_index(row)
        if step >= self._pattern_length() or not 0 <= index < len(self._keymap):
            self._drag = None
            return False
        now = time.time()
        # Hold and tap: while one pad of a row is held, pressing another pad
        # of the same row stretches the held pad's note to reach it. This does
        # not depend on which of the two events arrives first.
        anchor = self._held_press_in_row(index, exclude=note)
        if anchor is not None:
            if anchor["drag"] is None:
                anchor["drag"] = self._begin_drag(anchor["step"], index)
            self._extend_note(anchor["drag"], step)
            anchor["drag"]["step"] = step
            self._pressed_cells[note] = {
                "time": now, "step": step, "index": index,
                "drag": None, "consumed": True}
            return True
        # A drag continues while the finger slides along the row after a long
        # press is released: each new pad it reaches extends the note. The
        # gesture has to be quick - a deliberate press of a second pad is much
        # slower than that, and stays a separate press.
        if self._drag is not None:
            drag = self._drag
            if drag["index"] == index and drag["step"] != step \
                    and 0 <= now - drag["time"] <= SS_EXTEND_WINDOW:
                self._extend_note(drag, step)
                drag["step"] = step
                drag["time"] = now
                return True
            self._drag = None
        # Keyed by the pad note, and carrying the step and keymap row resolved
        # at press time: the step window can move under a held pad, and the
        # release must still act on the cell that was pressed.
        self._pressed_cells[note] = {
            "time": now, "step": step, "index": index,
            "drag": None, "consumed": False}
        return True

    def note_off(self, note, shifted_override=None):
        cell = self._pad_to_cell(note)
        if cell is None:
            return False
        pressed = self._pressed_cells.pop(note, None)
        if pressed is None or pressed["consumed"]:
            # Either not ours, or the tap end of a hold and tap, which the
            # held pad has already acted on.
            return True
        step, index = pressed["step"], pressed["index"]
        if step >= self._pattern_length() or not 0 <= index < len(self._keymap):
            return True
        now = time.time()
        if pressed["drag"] is not None:
            # The held end of a hold and tap. Keep the drag alive briefly so
            # the finger can carry on sliding along the row.
            self._drag = pressed["drag"]
            self._drag["time"] = now
            self._select_cell(pressed["drag"]["start"], index)
        elif now - pressed["time"] >= SS_LONG_PRESS_TIME:
            # A long press begins a drag: the note is created if the cell is
            # empty, or selected if there is one already, and it is then
            # stretched by moving along the row.
            self._drag = self._begin_drag(step, index)
            self._select_cell(step, index)
        else:
            # A short press. While a drag is in progress it is part of the
            # gesture - the finger moving off a pad - and must not toggle the
            # note it is dragging.
            if self._drag is not None:
                drag = self._drag
                if (drag["index"] == index
                        and 0 <= now - drag["time"] <= SS_EXTEND_WINDOW):
                    drag["time"] = now
                    return True
                self._drag = None
            self._toggle_note(step, index)
        return True

    def _held_press_in_row(self, index, exclude):
        """The earliest pad still held on a keymap row, or None."""
        held = [p for n, p in self._pressed_cells.items()
                if n != exclude and p["index"] == index and not p["consumed"]]
        return min(held, key=lambda p: p["time"]) if held else None

    def _begin_drag(self, step, index):
        """Start stretching the note in a cell, creating it if it is empty."""
        note_number = self._keymap[index]["note"]
        start = self._libseq.getNoteStart(step, note_number)
        if start == -1:
            # Nothing here yet - start the note now, so that the stretch
            # which follows lengthens it rather than replacing it.
            self._add_note(step, note_number, 1.0)
            self._mark_pattern_changed()
            start = step
        return {
            "start": start,
            "note": note_number,
            "index": index,
            "step": step,
            "time": time.time(),
        }

    def cc_change(self, ccnum, ccval):
        # Arrow cluster: Up/Down move the cursor between instruments, and the
        # window follows it. Left/Right move the step window. BACK lives on
        # Capture MIDI because the arrows are needed for navigation; sending
        # BACK flips the screen, which flips the driver back to launcher mode.
        #
        # Holding an arrow repeats it, so the cursor can be run through a long
        # list of instruments without pressing and releasing for every one.
        if ccnum in (0x5B, 0x5C):
            direction = 1 if ccnum == 0x5B else -1
            if ccval > 0:
                self._select_row(self._selected_row + direction)
                self._repeater.start_repeat(direction)
            else:
                self._repeater.stop_repeat()
            return True
        # Everything below acts on the press only: the device sends a CC
        # with value 0 on release, which would otherwise act a second time.
        if ccval <= 0:
            return True
        if ccnum == 0x5D:
            self._set_step_offset(self._step_offset - SS_NUM_COLS)
            return True
        if ccnum == 0x5E:
            self._set_step_offset(self._step_offset + SS_NUM_COLS)
            return True
        if ccnum == CC_CAPTURE:
            self._state_manager.send_cuia("BACK")
            return True
        # The control strip down the right-hand side.
        if ccnum == CC_TRANSPORT:
            self._toggle_play()
            return True
        for index, brush_cc in enumerate(SS_VELOCITY_CCS):
            if ccnum == brush_cc:
                self._set_brush(index)
                return True
        return True

    def _repeat_arrow(self, direction):
        """Called by the repeater thread while an arrow is held."""
        if not self._is_active:
            self._repeater.stop_repeat()
            return
        self._select_row(self._selected_row + direction)

    def _set_brush(self, level):
        self._brush = level
        self._save_selection()
        self._render_controls()

    def _select_cell(self, step, index):
        """Move the cursor to a cell and show it on the touchscreen too."""
        self._selected_step = step
        self._selected_row = index
        self._scroll_row_window()
        self._sync_screen()
        self._render_grid()

    # ----- on-screen pattern editor ---------------------------------------
    #
    # The pad and the screen are two views of the same piano roll, so the
    # driver only has to tell the screen where the cursor is. This reaches
    # into the GUI directly, which the Zynthian codebase already does from
    # ModeHandlerBase (_get_selected_step) and from the Launchpad Pro MK2
    # driver. Every call is best-effort: the driver must keep working if the
    # screen is elsewhere or if the UI changes shape.

    def _pattern_editor(self):
        """The on-screen pattern editor, or None when it is not the current screen."""
        try:
            zyngui = zynthian_gui_config.zyngui
            if zyngui is None or zyngui.current_screen != "pattern_editor":
                return None
            return zyngui.screens.get("pattern_editor")
        except Exception as ex:
            logging.debug(f"Launchpad X pattern editor unavailable: {ex}")
            return None

    def _mark_pattern_changed(self):
        """Tell the pattern editor its pattern was edited, so undo can see it.

        The pattern editor keeps libseq's undo stack by flagging itself changed
        and letting refresh_status() push a snapshot on its own timer. The
        driver writes notes straight through libseq, bypassing that flag, so
        without this its edits never reach the snapshot stack: undo appears to
        do nothing, and redo has nothing to restore. Setting the flag is enough
        - the GUI batches its own snapshots by SAVE_SNAPSHOT_DELAY, and ours
        follow the same rhythm, so a burst of pad presses undoes as one step.

        The reference APC driver has this same gap, with savePatternSnapshot()
        commented out.
        """
        try:
            zyngui = zynthian_gui_config.zyngui
            if zyngui is not None:
                patted = zyngui.screens.get("pattern_editor")
                if patted is not None:
                    patted.changed = True
        except Exception as ex:
            logging.debug(f"Launchpad X could not flag the pattern as changed: {ex}")

    # ----- sequencer operations -----------------------------------------

    def _toggle_note(self, step, index):
        """Create or remove the note in a cell."""
        note = self._keymap[index]["note"]
        start = self._libseq.getNoteStart(step, note)
        if start == -1:
            duration = self._add_note(step, note, self._default_duration())
            self._audition(note, duration)
        else:
            # A note that is longer than a step covers several cells, so it is
            # removed from where it starts - removing by the cell that was
            # tapped would find nothing and leave the note in place.
            self._libseq.removeNote(start, note)
        self._mark_pattern_changed()
        self._render_grid()

    def _default_duration(self):
        """The note length set in the on-screen editor, in steps."""
        pe = self._pattern_editor()
        try:
            duration = float(getattr(pe, "duration", 1.0)) if pe is not None else 1.0
        except (TypeError, ValueError):
            duration = 1.0
        return duration if duration > 0 else 1.0

    def _clamp_duration(self, start, note, duration):
        """Shorten a note so it ends inside the pattern and before the next
        note of the same pitch, as the on-screen editor does."""
        duration = min(duration, max(1, self._pattern_length() - start))
        for step in range(start + 1, start + ceil(duration)):
            if self._libseq.getNoteStart(step, note) == step:
                return float(step - start)
        return float(duration)

    def _add_note(self, step, note, duration):
        """Add a note at the brush velocity, with the editor's default note
        data (play chance, stutter...), so that a note entered on the pad is
        the same as one entered on the screen. Returns the duration used."""
        duration = self._clamp_duration(step, note, duration)
        velocity = self._brush_velocity
        self._libseq.addNote(step, note, velocity, duration, 0)
        pe = self._pattern_editor()
        if pe is not None:
            try:
                evdata = pe.get_default_note_evdata()
                # setNoteData overwrites the velocity; keep the brush's.
                evdata.val2_start = velocity
                self._libseq.setNoteData(step, note, evdata)
            except Exception as ex:
                logging.debug(f"Launchpad X could not set the note data: {ex}")
        return duration

    def _audition(self, note, duration):
        """Play a newly entered note, but only while the pattern is stopped,
        like the on-screen editor - otherwise it doubles the sequence."""
        try:
            if self._is_sequence_playing():
                return
            self._libseq.playNote(
                note, self._brush_velocity, self._chain_midi_chan() & 0x0F,
                int(SS_AUDITION_MS_PER_STEP * duration))
        except Exception as ex:
            logging.debug(f"Launchpad X could not audition a note: {ex}")

    def _extend_note(self, pending, new_step):
        """Lengthen or shorten a note so that it reaches `new_step`.

        The note is removed and re-added with the new duration, because libseq
        has no call to change a note's length in place. Its velocity, offset
        and note data (stutter, play chance...) are carried over.
        """
        start = pending["start"]
        note = pending["note"]
        duration = self._clamp_duration(start, note, max(1, new_step - start + 1))
        if duration <= 0:
            return False
        velocity = self._libseq.getNoteVelocity(start, note)
        offset = self._libseq.getNoteOffset(start, note)
        evdata = self._zynseq.get_note_data(start, note)
        self._libseq.removeNote(start, note)
        self._libseq.addNote(start, note, velocity, duration, offset)
        if evdata is not None:
            self._libseq.setNoteData(start, note, evdata)
        self._mark_pattern_changed()
        self._render_grid()
        return True

    def _toggle_play(self):
        self._libseq.togglePlayState(
            self._zynseq.scene, self._selected_seq[0], self._selected_seq[1])

    def _chain_midi_chan(self):
        try:
            return self._libseq.getChannel(
                self._zynseq.scene, self._selected_seq[0], self._selected_seq[1], 0)
        except Exception:
            return 0

    def _is_sequence_playing(self):
        return self._libseq.getPlayState(
            self._zynseq.scene, self._selected_seq[0], self._selected_seq[1]) != zynseq.SEQ_STOPPED

    # ----- external state ------------------------------------------------

    def update_seq_state(self, phrase, chan):
        """A sequence changed play state (zynpad signal)."""
        if (phrase, chan) == self._selected_seq:
            self._refresh_play_state()

    def _refresh_play_state(self):
        """Read the play state of the edited sequence, and redraw the
        transport button if it changed. Returns True if the sequence plays."""
        try:
            playing = self._is_sequence_playing()
        except Exception:
            playing = False
        if playing != self._is_playing:
            self._is_playing = playing
            self._render_controls()
        return playing

    # ----- rendering -----------------------------------------------------

    def refresh(self, force=False):
        """Repaint the surface.

        `force` clears every LED first, which is needed once on entering the
        mode because the launcher paints outside the LED cache. Otherwise the
        cache already knows what is lit, and a forced clear would only make
        the grid flicker.
        """
        with self._render_lock:
            if not self._is_active:
                return
            if force:
                self.control_off()
            else:
                for ccnum in (0x5B, 0x5C, 0x5D, 0x5E, CC_CAPTURE):
                    self._leds.cc_off(ccnum)
            self._render_grid()
            self._render_controls()

    def control_off(self):
        # Launcher mode paints the grid straight through lib_zyncore, bypassing
        # this LED layer, so those pads are untracked here and would survive a
        # mode switch. The whole grid has to be cleared explicitly, and
        # unconditionally: the tracked state cannot be trusted to know that
        # they are already off.
        notes = [self._driver.get_note_from_xy(col, row)
                 for row in range(8) for col in range(8)]
        self._leds.force_off(notes, SS_RIGHT_COL_CCS)
        for arrow in (0x5B, 0x5C, 0x5D, 0x5E):
            self._leds.cc_off(arrow)
        self._leds.cc_off(CC_CAPTURE)

    def _step_velocity_level(self, step, note):
        """Map a stored step velocity back onto the nearest brush level."""
        velocity = self._libseq.getNoteVelocity(step, note)
        best, best_delta = 0, None
        for level, value in enumerate(SS_VELOCITY_VALUES):
            delta = abs(value - velocity)
            if best_delta is None or delta < best_delta:
                best, best_delta = level, delta
        return best

    def _render_grid(self):
        """Draw the piano roll: a note per cell, coloured by its velocity."""
        with self._render_lock:
            if not self._is_active:
                return
            length = self._pattern_length()
            steps_per_beat = self._steps_per_beat()
            for row in range(SS_NUM_ROWS):
                index = self._keymap_index(row)
                selected = index == self._selected_row
                for col in range(SS_NUM_COLS):
                    pad = self._cell_pad(col, row)
                    step = self._step_offset + col
                    if not 0 <= index < len(self._keymap) or step >= length:
                        self._leds.pad_off(pad)
                        continue
                    note = self._keymap[index]["note"]
                    # The playhead is a column, drawn over everything in it,
                    # the way the touchscreen draws its cursor.
                    if step == self._playhead_step:
                        self._leds.pad_on(pad, COLORS.PLAYHEAD, 2)
                        continue
                    # getNoteStart returns the step a note covering this cell
                    # starts at, so a note longer than a step shows its first
                    # step lit and the rest as a tail.
                    start = self._libseq.getNoteStart(step, note)
                    if start == -1:
                        # An empty cell on a downbeat is marked, so the beat
                        # grid is readable at a glance.
                        if step % steps_per_beat == 0:
                            self._leds.pad_on(pad, COLORS.BEAT_MARKER)
                        else:
                            self._leds.pad_off(pad)
                        continue
                    # The selected instrument's row pulses, so the cursor is
                    # obvious without hiding the note colours.
                    channel = 2 if selected else 0
                    if start == step:
                        level = self._step_velocity_level(step, note)
                        self._leds.pad_on(pad, COLORS.VELOCITY_STEP[level], channel)
                    else:
                        self._leds.pad_on(pad, COLORS.NOTE_TAIL, channel)

    def _render_controls(self):
        """Draw the control strip: transport and the velocity brush."""
        with self._render_lock:
            if not self._is_active:
                return
            colour = COLORS.TRANSPORT_ACTIVE if self._is_playing else COLORS.TRANSPORT
            self._leds.cc_on(CC_TRANSPORT, colour, 2 if self._is_playing else 0)
            for index, ccnum in enumerate(SS_VELOCITY_CCS):
                if index == self._brush:
                    self._leds.cc_on(ccnum, COLORS.VELOCITY_LANE_SELECTED, 2)
                else:
                    self._leds.cc_on(ccnum, COLORS.VELOCITY_LANE)

    def _update_playhead(self):
        """Called from the playhead monitor thread."""
        try:
            with self._render_lock:
                if not self._is_active:
                    return
                repaint = False
                self._sync_count += 1
                if self._sync_count >= SS_SYNC_EVERY:
                    # Follow the screen, and repaint so that edits made on the
                    # touchscreen appear while the pattern is stopped. The LED
                    # cache drops the sends that would change nothing.
                    self._sync_count = 0
                    if self._sync_with_screen():
                        self._render_controls()
                    repaint = True
                if self._refresh_play_state():
                    # libseq's own playhead for the edited pattern, as the
                    # on-screen editor uses.
                    index = int(self._libseq.getPatternPlayhead())
                    new_step = index if index < self._pattern_length() else None
                else:
                    new_step = None
                if new_step != self._playhead_step:
                    self._playhead_step = new_step
                    # The window follows the playhead a page at a time, so
                    # within a page only the playhead column moves and the
                    # grid is still. When it stops, the window stays where
                    # the playhead left it.
                    if new_step is not None:
                        page = (new_step // SS_NUM_COLS) * SS_NUM_COLS
                        if page != self._step_offset:
                            self._step_offset = max(
                                0, min(page, self._max_step_offset()))
                    repaint = True
                if repaint:
                    self._render_grid()
        except Exception as ex:
            logging.error(f"Launchpad X playhead update error: {ex}")


# ------------------------------------------------------------------------------------------------------------------
class zynthian_ctrldev_launchpad_x(zynthian_ctrldev_zynpad):

    dev_ids = ["Launchpad X IN 1"]
    driver_name = "Launchpad X"
    driver_description = "Launcher + piano roll step sequencer"

    def __init__(self, state_manager, idev_in, idev_out=None):
        super().__init__(state_manager, idev_in, idev_out)
        self._mode = MODE_LAUNCHER
        self._stepseq = None
        # True while the device is in its Note layout and the pad is being
        # played rather than used to launch patterns.
        self._note_mode = False
        # Launcher pad presses in flight, so a long press can be told from a
        # tap. The launcher path used to look at note-on only.
        self._launcher_presses = {}
        # Notes held in note mode, and the MIDI channel each was sent on.
        self._note_mode_held = {}

    def send_sysex(self, data):
        if self.idev_out is not None:
            msg = bytes.fromhex(f"F0 00 20 29 02 0C {data} F7")
            lib_zyncore.dev_send_midi_event(self.idev_out, msg, len(msg))
            sleep(0.05)

    def get_note_xy(self, note):
        row = 8 - (note // 10)
        col = (note % 10) - 1
        return col, row

    def get_note_from_xy(self, col, row):
        return 10 * (8 - row) + col + 1

    # ----- LED output --------------------------------------------------

    def send_led_note(self, note, colour, channel=0):
        if self.idev_out is not None:
            lib_zyncore.dev_send_note_on(self.idev_out, channel, note, colour)

    def send_led_cc(self, ccnum, colour, channel=0):
        if self.idev_out is not None:
            lib_zyncore.dev_send_ccontrol_change(self.idev_out, channel, ccnum, colour)

    # ----- lifecycle ----------------------------------------------------

    def init(self):
        # Awake
        self.sleep_off()
        # self.send_sysex_universal_inquiry()
        # Enter DAW session mode
        self.send_sysex("10 01")
        # Select session layout (session = 0x00, faders = 0x0D)
        self.send_sysex("00 00")
        self._stepseq = StepSeqHandler(self.state_manager, self, self.idev)
        super().init()
        zynsigman.register(zynsigman.S_GUI, zynsigman.SS_GUI_SHOW_SCREEN, self._on_gui_show_screen)

    def end(self):
        if self._stepseq is not None:
            self._stepseq.end()
        zynsigman.unregister(zynsigman.S_GUI, zynsigman.SS_GUI_SHOW_SCREEN, self._on_gui_show_screen)
        super().end()
        # Exit DAW session mode
        self.send_sysex("10 00")
        # Select Keys layout (drums = 0x04, keys = 0x05, user = 0x06, prog = 0x7F)
        self.send_sysex("00 05")

    # ----- mode switching -------------------------------------------------

    def _on_gui_show_screen(self, screen):
        """The on-screen Pattern Editor is the single source of truth for the mode.

        This signal only fires when the screen changes. A pattern, scale or
        length change made while the editor stays open is picked up by the
        handler's own poll instead.
        """
        if screen == "pattern_editor":
            self.set_mode(MODE_STEPSEQ)
        elif self._mode == MODE_STEPSEQ:
            self.set_mode(MODE_LAUNCHER)

    def set_mode(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        if mode == MODE_STEPSEQ:
            if self._note_mode:
                # The step sequencer paints Session-layout note numbers, so
                # the device must leave its Note layout first.
                self._exit_note_mode(refresh=False)
            self._stepseq.set_active(True)
        else:
            self._stepseq.set_active(False)
            # The handler cleared its own LEDs; wipe the device state and repaint
            # the launcher, otherwise the grid would stay dark until the next event.
            self.light_off()
            self.refresh()

    def get_mode(self):
        return self._mode

    def get_state(self):
        state = super().get_state()
        state["mode"] = self._mode
        if self._stepseq is not None:
            state["stepseq"] = self._stepseq.get_state()
        return state

    def set_state(self, state):
        super().set_state(state)
        if self._stepseq is not None and "stepseq" in state:
            stepseq = state["stepseq"]
            # Snapshots saved by earlier builds of this branch nest it twice.
            if isinstance(stepseq, dict) and isinstance(stepseq.get("stepseq"), dict):
                stepseq = stepseq["stepseq"]
            self._stepseq.set_state(stepseq)

    # ----- signal routing -------------------------------------------------

    def refresh(self):
        if self._mode == MODE_STEPSEQ and self._stepseq is not None:
            # Only the base refresh (chain list); skip the launcher repaint.
            zynthian_ctrldev_base.refresh(self)
            self._stepseq.refresh()
            return
        if self._note_mode:
            # The device shows its own Note layout, which the driver must not
            # paint over.
            return
        super().refresh()

    def update_seq_state(self, phrase, chan):
        if self._mode == MODE_STEPSEQ and self._stepseq is not None:
            self._stepseq.update_seq_state(phrase, chan)
            return
        if self._note_mode:
            return
        super().update_seq_state(phrase, chan)

    def update_pad(self, row, col, pad_info):
        if self._mode == MODE_STEPSEQ or self._note_mode:
            return
        midi_chan = 0
        color = 0
        try:
            state = pad_info["state"]
            if col == self.cols:
                group = 32
            else:
                group = pad_info["group"]
            if pad_info["repeat"] == 0 or group > MAX_NUM_MIDI_CHANS:
                pass
            elif state == zynseq.SEQ_STOPPED:
                if not pad_info["empty"]:
                    color = zynthian_gui_config.LAUNCHER_COLOUR[group]["launchpad"]
            elif state in (zynseq.SEQ_PLAYING, zynseq.SEQ_CHILD_PLAYING):
                midi_chan = 2
                if col == self.cols:
                    color = zynthian_gui_config.LAUNCHER_STARTING_COLOUR["launchpad"]
                else:
                    color = zynthian_gui_config.LAUNCHER_COLOUR[group]["launchpad"]
            elif state in (zynseq.SEQ_STOPPING, zynseq.SEQ_STOPPING_SYNC, zynseq.SEQ_FORCED_STOP, zynseq.SEQ_CHILD_STOPPING):
                midi_chan = 1
                color = zynthian_gui_config.LAUNCHER_STOPPING_COLOUR["launchpad"]
            elif state == zynseq.SEQ_STARTING:
                midi_chan = 1
                color = zynthian_gui_config.LAUNCHER_STARTING_COLOUR["launchpad"]
        except:
            pass
        # Send MIDI event to controller
        if col < self.cols:
            note = 10 * (8 - row) + col + 1
            lib_zyncore.dev_send_note_on(self.idev_out, midi_chan, note, color)
        elif col == self.cols:
            ccnum = 89 - 10 * row
            lib_zyncore.dev_send_ccontrol_change(self.idev_out, midi_chan, ccnum, color)

    # Light-Off the pad specified with chan & phrase (column & row)
    def pad_off(self, col, row):
        if col < zynseq.PHRASE_CHANNEL:
            note = 10 * (8 - row) + col + 1
            lib_zyncore.dev_send_note_on(self.idev_out, 0, note, 0)
        elif col == zynseq.PHRASE_CHANNEL:
            ccnum = 89 - 10 * row
            lib_zyncore.dev_send_ccontrol_change(self.idev_out, 0, ccnum, 0)

    # ----- MIDI input ------------------------------------------------------

    def _restore_session_layout(self):
        """Undo a user-initiated layout change.

        Novation documents that the Session/Note/Custom buttons "still send the
        corresponding CC messages on interaction, allowing the DAW to catch
        relevant events". So the device really does switch layout even in DAW
        mode, and this driver really was painting Session-layout note numbers
        into a Note or Custom layout - which scrambles the grid and turns pad
        presses into sequence launches. Force Session back and repaint.
        """
        if self.idev_out is None:
            return
        self.send_sysex("00 00")
        self.refresh()

    # ----- the device's own layout buttons --------------------------------

    def _handle_layout_button(self, ccnum):
        """The device's layout buttons, which double as mode switches.

        Novation's Session, Note and Custom buttons switch the device's layout
        and also send CCs, so the driver has to catch them before its own
        dispatch - otherwise a pad press in the wrong layout means something
        entirely different.

        In the pattern editor, Session means "go back to the launcher". In the
        launcher, Note means "play the instrument", and the device's own Note
        layout is left to show itself. Custom is not a layout this driver
        paints, so it always falls back to Session.
        """
        if self._mode == MODE_STEPSEQ:
            if ccnum == CC_SESSION:
                self._restore_session_layout()
                # Back to the launcher, with the device in its Session layout.
                self.state_manager.send_cuia("SCREEN_ZYNPAD")
            else:
                self._restore_session_layout()
            return True
        if self._note_mode:
            # Only Session leaves note mode; the others are not layouts the
            # driver paints.
            if ccnum == CC_SESSION:
                self._exit_note_mode()
            else:
                self._restore_session_layout()
            return True
        if ccnum == CC_NOTE:
            self._enter_note_mode()
            return True
        self._restore_session_layout()
        return True

    def _enter_note_mode(self):
        """Switch the device to its Note layout and play the active chain.

        The device's Note layout is left to show itself - the driver stops
        painting the grid - and the pad's notes are forwarded to the chain, so
        the pad becomes a keyboard for whatever is loaded.
        """
        if self.idev_out is None:
            return False
        self._note_mode = True
        # Layout 0x01 is the device's Note layout.
        self.send_sysex("00 01")
        logging.info("Launchpad X note mode on")
        return True

    def _exit_note_mode(self, refresh=True):
        self._note_mode = False
        self._release_held_notes()
        if refresh:
            self._restore_session_layout()
        elif self.idev_out is not None:
            self.send_sysex("00 00")
        logging.info("Launchpad X note mode off")

    def _release_held_notes(self):
        """Send a note-off for every pad still held in note mode."""
        held, self._note_mode_held = self._note_mode_held, {}
        for note, chan in held.items():
            try:
                self.zynseq.libseq.sendMidiCommand(0x80 | (chan & 0x0F), note, 0)
            except Exception as ex:
                logging.error(f"Launchpad X could not release a note: {ex}")

    def _note_mode_channel(self):
        """The MIDI channel of the chain to play, or None if there is none."""
        try:
            chain = self.chain_manager.get_active_chain()
            if chain is not None and chain.midi_chan is not None:
                return chain.midi_chan
        except Exception as ex:
            logging.debug(f"Launchpad X could not find the active chain: {ex}")
        return None

    def _launcher_note_off(self, note):
        """A long press on a launcher pad opens that pattern's editor.

        The launcher grid already says which pattern is under which pad: the
        column is the chain and the row is the phrase. The pattern is also
        launched by the press, so this both starts it and opens it for
        editing, the way a modified click does in a DAW.
        """
        pressed_at = self._launcher_presses.pop(note, None)
        if pressed_at is None:
            return False
        if time.time() - pressed_at < SS_LONG_PRESS_TIME:
            return False
        col, row = self.get_note_xy(note)
        if not 0 <= col < self.cols:
            return False
        midi_chan = self.get_filtered_midi_chan_by_index(col + self.scroll_h)
        if midi_chan is None:
            return False
        self._open_pattern_editor(row + self.scroll_v, midi_chan)
        return True

    def _open_pattern_editor(self, phrase, midi_chan):
        """Open the pattern editor on a given pattern.

        ModeHandlerBase._show_pattern_editor() selects the phrase and the
        chain - which is what sets the sequence the editor will edit - and
        then loads the pattern.
        """
        if self._stepseq is None:
            return False
        try:
            self._stepseq._show_pattern_editor((phrase, midi_chan))
            logging.info(
                f"Launchpad X opening the pattern editor on phrase {phrase}, "
                f"channel {midi_chan}")
            return True
        except Exception as ex:
            logging.error(f"Launchpad X could not open the pattern editor: {ex}")
            return False

    def _note_mode_midi_event(self, ev, evtype):
        """Forward the pad's notes and pressure to the chain, so the pad can
        be played.

        The channel is remembered per note at note-on, so that the note-off
        and the pressure go to the same chain even if the active chain
        changes while the pad is held - otherwise the note would hang.
        """
        if evtype not in (0x9, 0x8, 0xA, 0xD):
            return True
        value1 = ev[1] & 0x7F
        value2 = ev[2] & 0x7F if len(ev) > 2 else 0
        if evtype == 0x9 and value2 == 0:
            evtype = 0x8
        if evtype == 0x9:
            chan = self._note_mode_channel()
            if chan is None:
                return True
            self._note_mode_held[value1] = chan
        elif evtype == 0x8:
            chan = self._note_mode_held.pop(value1, None)
            if chan is None:
                chan = self._note_mode_channel()
        elif evtype == 0xA:
            # Polyphonic pressure follows its note.
            chan = self._note_mode_held.get(value1)
        else:
            # Channel pressure has a single data byte.
            chan = self._note_mode_channel()
            value2 = 0
        if chan is None:
            return True
        status = (evtype << 4) | (chan & 0x0F)
        try:
            self.zynseq.libseq.sendMidiCommand(status, value1, value2)
        except Exception as ex:
            logging.error(f"Launchpad X could not forward a note: {ex}")
        return True

    def midi_event(self, ev):
        # logging.debug(f"Launchpad X MIDI handler => {ev}")
        evtype = (ev[0] >> 4) & 0x0F
        # SysEx
        if ev[0] == 0xF0:
            logging.info(f"Received SysEx => {ev.hex(' ')}")
            return True
        # Layout buttons, caught before mode dispatch so they are handled in
        # launcher mode and stepseq mode alike.
        if evtype == 0xB and (ev[2] & 0x7F) > 0 and (ev[1] & 0x7F) in LAYOUT_BUTTON_CCS:
            logging.debug(f"Layout button {ev[1] & 0x7F} pressed")
            return self._handle_layout_button(ev[1] & 0x7F)
        if self._mode == MODE_STEPSEQ and self._stepseq is not None:
            return self._stepseq_midi_event(ev, evtype)
        if self._note_mode:
            return self._note_mode_midi_event(ev, evtype)
        # Note ON => launch/stop sequence
        if evtype == 0x9:
            note = ev[1] & 0x7F
            vel = ev[2] & 0x7F
            if vel > 0:
                self._launcher_presses[note] = time.time()
                col, row = self.get_note_xy(note)
                midi_chan = None
                if 0 <= col < self.cols:
                    midi_chan = self.get_filtered_midi_chan_by_index(col + self.scroll_h)
                if midi_chan is not None:
                    phrase = row + self.scroll_v
                    try:
                        self.zynseq.libseq.togglePlayState(self.zynseq.scene, phrase, midi_chan)
                    except:
                        pass
            else:
                # Note OFF with no velocity: the pad was released.
                self._launcher_note_off(note)
            return True
        # Note OFF => a long press on a launcher pad opens its pattern editor
        if evtype == 0x8:
            self._launcher_note_off(ev[1] & 0x7F)
            return True
        # CC => arrows & phrases
        elif evtype == 0xB:
            ccnum = ev[1] & 0x7F
            ccval = ev[2] & 0x7F
            if ccval > 0:
                if ccnum == 0x5B:
                    self.state_manager.send_cuia("ARROW_UP")
                elif ccnum == 0x5C:
                    self.state_manager.send_cuia("ARROW_DOWN")
                elif ccnum == 0x5D:
                    self.state_manager.send_cuia("ARROW_LEFT")
                elif ccnum == 0x5E:
                    self.state_manager.send_cuia("ARROW_RIGHT")
                else:
                    col, row = self.get_note_xy(ccnum)
                    if col == 8:
                        try:
                            phrase = row + self.scroll_v
                            self.zynseq.libseq.togglePlayState(self.zynseq.scene, phrase, zynseq.PHRASE_CHANNEL)
                        except:
                            pass
            return True

    def _stepseq_midi_event(self, ev, evtype):
        """Route input to the step sequencer handler."""
        handler = self._stepseq
        if evtype == 0x9:
            note = ev[1] & 0x7F
            vel = ev[2] & 0x7F
            if vel > 0:
                handler.note_on(note, vel)
            else:
                handler.note_off(note)
            return True
        elif evtype == 0xB:
            handler.cc_change(ev[1] & 0x7F, ev[2] & 0x7F)
            return True
        elif evtype == 0x8:
            handler.note_off(ev[1] & 0x7F)
            return True
        return True

    # ----- device state ----------------------------------------------------

    # Light-Off LEDs
    def light_off(self):
        # logging.debug("Lighting Off LEDs Launchpad X")
        # Clean state of notes & CCs
        self.send_sysex("12 01 00 01")

    # Sleep On
    def sleep_on(self):
        # Sleep Mode (0 = sleep, 1 = awake)
        self.send_sysex("09 00")

    # Sleep Off
    def sleep_off(self):
        # Sleep Mode (0 = sleep, 1 = awake)
        self.send_sysex("09 01")

# ------------------------------------------------------------------------------