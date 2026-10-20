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
from time import sleep
from copy import deepcopy
from threading import Thread, RLock, Event

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
# Steps per beat, used to mark the downbeats in the grid.
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

# Discrete velocity levels offered by the brush, ordered LOW -> HIGH.
# A new note is created at the selected level.
SS_VELOCITY_VALUES = [25, 50, 75, 100, 127]
SS_VELOCITY_DEFAULT = 2  # index into SS_VELOCITY_VALUES

# Playhead polling interval (seconds). The zynseq play position is polled rather
# than taken from a JACK MIDI clock tap so this driver has no hard jack/python-jack
# dependency and no extra child process to manage.
SS_PLAYHEAD_INTERVAL = 0.015


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
        return self._seqs.get(seq, [0, SS_VELOCITY_DEFAULT])

    def set_selection_by_sequence(self, seq, pad, brush):
        self._seqs[seq] = [pad, brush]


# ------------------------------------------------------------------------------------------------------------------
# Note player used to audition steps and velocity levels.
#
# Sends into zynseq's MIDI input so the sound comes out of the chain being
# edited. Runs in its own thread so a long note does not block the MIDI handler.
# ------------------------------------------------------------------------------------------------------------------
class NotePlayer(Thread):

    def __init__(self, libseq):
        super().__init__()
        self._libseq = libseq
        self._ready = Event()
        self._pending = []
        self._lock = RLock()
        self.daemon = True
        self.start()

    def play(self, note, velocity, duration=0.5, channel=0):
        with self._lock:
            self._pending = [(note, velocity, duration, channel)]
        self._ready.set()

    def end(self):
        self.running = False
        self._ready.set()

    def _cycle_ms(self, cycles):
        # 24 MIDI clock events per beat (quarter note)
        bpm = self._libseq.getTempo()
        return max(1, round(60 / bpm / 24 * 1000 * cycles))

    def run(self):
        self.running = True
        while self.running:
            self._ready.wait()
            with self._lock:
                if not self._pending:
                    self._ready.clear()
                    continue
                note, velocity, duration, channel = self._pending.pop(0)
            try:
                self._send_note("on", note, channel, velocity)
                time.sleep(self._cycle_ms(duration * 24) / 1000)
                self._send_note("off", note, channel, 0)
            except Exception as ex:
                logging.error(f"Launchpad X note player error: {ex}")
            with self._lock:
                if not self._pending:
                    self._ready.clear()

    def _send_note(self, mode, note, channel, velocity=0):
        status = 0x90 if mode == "on" else 0x80
        status |= (channel & 0x0F)
        self._libseq.sendMidiCommand(status, note & 0x7F, velocity & 0x7F)


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
        self._note_player = NotePlayer(self._libseq)
        self._playhead = PlayheadMonitor(self._update_playhead)

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
        self._playhead_step = None
        self._is_playing = False

    def end(self):
        self._playhead.end()
        self._playhead.set_enabled(False)
        self._note_player.end()

    def set_active(self, active):
        super().set_active(active)
        if active:
            self._load_current_selection()
            self._playhead.set_enabled(True)
            self.refresh()
        else:
            self._playhead.set_enabled(False)
            self._leds.all_off()

    # ----- state ---------------------------------------------------------

    def get_state(self):
        return {"stepseq": self._saved_state.save()}

    def set_state(self, state):
        state = state.get("stepseq")
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
        self._row_offset = 0
        self._step_offset = 0
        self._selected_row = 0
        self._selected_step = 0
        self._load_keymap()

    def _save_selection(self):
        self._saved_state.get_chain(self._chain_id)["brush"] = self._brush

    def _load_keymap(self):
        """Read the chain's keymap from the pattern editor.

        The keymap is the screen's own list of the notes this chain responds
        to, and it is rebuilt whenever the pattern editor loads a pattern. It
        lives on the GUI rather than in libseq, so it is read from there - the
        same access ModeHandlerBase already uses.
        """
        pe = self._pattern_editor()
        keymap = []
        if pe is not None:
            try:
                keymap = list(pe.keymap or [])
            except Exception as ex:
                logging.debug(f"Launchpad X could not read the keymap: {ex}")
        self._keymap = keymap
        logging.debug(f"Launchpad X keymap -> {len(self._keymap)} rows")
        if self._keymap:
            self._selected_row = max(0, min(self._selected_row, len(self._keymap) - 1))
        self._scroll_row_window()

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
        col, row = cell
        step = self._step_offset + col
        index = self._keymap_index(row)
        if step >= self._pattern_length() or not 0 <= index < len(self._keymap):
            return False
        # Keyed by the pad note, and carrying the step and keymap row resolved
        # at press time: the step window can move under a held pad, and the
        # release must still act on the cell that was pressed.
        self._pressed_cells[note] = (time.time(), step, index)
        return True

    def note_off(self, note, shifted_override=None):
        cell = self._pad_to_cell(note)
        if cell is None:
            return False
        pressed = self._pressed_cells.pop(note, None)
        if pressed is None:
            return True
        pressed_at, step, index = pressed
        if step >= self._pattern_length() or not 0 <= index < len(self._keymap):
            return True
        if time.time() - pressed_at >= SS_LONG_PRESS_TIME:
            # A long press selects the note instead of toggling it.
            self._select_cell(step, index)
        else:
            self._toggle_note(step, index)
        return True

    def cc_change(self, ccnum, ccval):
        if ccval <= 0:
            return True
        # Arrow cluster: Up/Down move the cursor between instruments, and the
        # window follows it. Left/Right move the step window. BACK lives on
        # Capture MIDI because the arrows are needed for navigation; sending
        # BACK flips the screen, which flips the driver back to launcher mode.
        if ccnum == 0x5B:
            self._select_row(self._selected_row + 1)
            return True
        if ccnum == 0x5C:
            self._select_row(self._selected_row - 1)
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
        if self._libseq.getNoteStart(step, note) == -1:
            self._libseq.addNote(step, note, self._brush_velocity, 1.0, 0)
            self._note_player.play(
                note, self._brush_velocity, 1.0, self._chain_midi_chan())
        else:
            self._libseq.removeNote(step, note)
        self._mark_pattern_changed()
        self._render_grid()

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

    def update_seq_state(self, phrase, chan, state=None, mode=None, group=None):
        if state is not None:
            self._is_playing = state != zynseq.SEQ_STOPPED

    def on_screen_change(self, screen):
        self._current_screen = screen

    # ----- rendering -----------------------------------------------------

    def refresh(self):
        self.control_off()
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
        length = self._pattern_length()
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
                # The playhead is a column, drawn over everything in it, the
                # way the touchscreen draws its cursor.
                if step == self._playhead_step:
                    self._leds.pad_on(pad, COLORS.PLAYHEAD, 2)
                    continue
                if self._libseq.getNoteStart(step, note) == -1:
                    # An empty cell on a downbeat is marked, so the beat grid
                    # is readable at a glance.
                    if step % SS_BEAT_DIVISION == 0:
                        self._leds.pad_on(pad, COLORS.BEAT_MARKER)
                    else:
                        self._leds.pad_off(pad)
                    continue
                level = self._step_velocity_level(step, note)
                # The selected instrument's row pulses, so the cursor is
                # obvious without hiding the note colours.
                self._leds.pad_on(
                    pad, COLORS.VELOCITY_STEP[level], 2 if selected else 0)

    def _render_controls(self):
        """Draw the control strip: transport and the velocity brush."""
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
            if self._libseq.getPlayState(
                    self._zynseq.scene,
                    self._selected_seq[0],
                    self._selected_seq[1]) == zynseq.SEQ_STOPPED:
                new_step = None
            else:
                clocks_per_step = self._libseq.getClocksPerStep()
                if clocks_per_step <= 0:
                    new_step = None
                else:
                    position = self._libseq.getSequencePlayPosition(
                        self._zynseq.scene,
                        self._selected_seq[0],
                        self._selected_seq[1])
                    index = int(position) // clocks_per_step
                    new_step = index if index < self._pattern_length() else None
            if new_step != self._playhead_step:
                self._playhead_step = new_step
                # The window follows the playhead a page at a time, so within
                # a page only the playhead column moves and the grid is still.
                # When it stops, the window stays where the playhead left it.
                if new_step is not None:
                    page = (new_step // SS_NUM_COLS) * SS_NUM_COLS
                    if page != self._step_offset:
                        self._step_offset = max(
                            0, min(page, self._max_step_offset()))
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
        """The on-screen Pattern Editor is the single source of truth for the mode."""
        if screen == "pattern_editor":
            if self._mode == MODE_STEPSEQ and self._stepseq is not None:
                # Already in stepseq mode, so set_mode() returns early. The player
                # can switch patterns without ever leaving the Pattern Editor, so
                # reload the keymap and repaint rather than showing the previous
                # pattern's instruments.
                self._stepseq._load_keymap()
                self._stepseq.refresh()
            else:
                self.set_mode(MODE_STEPSEQ)
        elif self._mode == MODE_STEPSEQ:
            self.set_mode(MODE_LAUNCHER)

    def set_mode(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        if mode == MODE_STEPSEQ:
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
            self._stepseq.set_state(state)

    # ----- signal routing -------------------------------------------------

    def refresh(self):
        if self._mode == MODE_STEPSEQ and self._stepseq is not None:
            # Only the base refresh (chain list); skip the launcher repaint.
            zynthian_ctrldev_base.refresh(self)
            self._stepseq.refresh()
            return
        super().refresh()

    def update_seq_state(self, phrase, chan):
        if self._mode == MODE_STEPSEQ and self._stepseq is not None:
            self._stepseq.update_seq_state(phrase, chan)
            return
        super().update_seq_state(phrase, chan)

    def update_pad(self, row, col, pad_info):
        if self._mode == MODE_STEPSEQ:
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
            logging.debug("Layout button pressed - restoring Session layout")
            self._restore_session_layout()
            return True
        if self._mode == MODE_STEPSEQ and self._stepseq is not None:
            return self._stepseq_midi_event(ev, evtype)
        # Note ON => launch/stop sequence
        if evtype == 0x9:
            note = ev[1] & 0x7F
            vel = ev[2] & 0x7F
            if vel > 0:
                col, row = self.get_note_xy(note)
                midi_chan = self.get_filtered_midi_chan_by_index(col)
                if midi_chan is not None:
                    phrase = row + self.scroll_v
                    try:
                        self.zynseq.libseq.togglePlayState(self.zynseq.scene, phrase, midi_chan)
                    except:
                        pass
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