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
# Row indices here are DRIVER rows, which run opposite to physical rows.
# get_note_from_xy() maps note = 10 * (8 - row) + col + 1, therefore:
#
#     row 0 -> notes 81-88 -> the PHYSICAL TOP row
#     row 7 -> notes 11-18 -> the PHYSICAL BOTTOM row
#
# Verified on hardware: sending notes 11-18 lights the bottom row and
# notes 81-88 lights the top row. These constants are therefore counted
# from the physical top, which means LOW driver row numbers.
SS_ROW_INSTRUMENTS = 0    # Physical top: instrument (note) pads
SS_ROW_STEPS_A = 1        # Steps 1-8
SS_ROW_STEPS_B = 2        # Steps 9-16
SS_ROW_VELOCITY = 3       # Velocity brush pads
SS_NUM_STEPS = 16
SS_NUM_VELOCITIES = 5
# Instruments shown per page on the top row. The last instrument on the last
# page is followed by the "+" slot.
SS_NUM_INSTRUMENTS = 8
# Lowest note offered by the default instrument bank and by the sequential "+"
# assignment, so additions do not land on the very low notes that percussion
# mappings rarely use.
SS_FIRST_OFFERED_NOTE = 36
# Notes offered when the pattern does not supply them. Without this an empty
# pattern leaves the top row with nothing selectable, and no step can be written
# at all - the same failure as having no instrument bound.
SS_DEFAULT_INSTRUMENTS = 16

# Safety valve for the instrument scan. libseq offers no way to list the notes a
# pattern holds, so the notes are discovered by probing getNoteStart() over the
# whole grid: up to 128 notes x this many steps. Only ever run on entering the
# mode or when the pattern changes, never on a repaint.
SS_SCAN_MAX_STEPS = 256

# Note-off happens before this many seconds elapse => plain toggle.
# Above it => long press, which re-strikes the step at the current brush velocity.
SS_LONG_PRESS_TIME = 0.5

# Discrete velocity levels offered on the velocity lane, ordered LOW -> HIGH.
# A selected level is applied to any step pad that is subsequently toggled on.
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
    # Bright overlay for the step currently under the playhead.
    PLAYHEAD = 3
    # Instrument pads
    INSTRUMENT = 21
    INSTRUMENT_SELECTED = 3
    # Instrument slot with no note behind it. Same hue as INSTRUMENT but dim, so
    # an unconfigured grid reads as "nothing here yet" instead of looking ready.
    INSTRUMENT_EMPTY = 23
    # The "+" slot: holds an instrument pad and plays a note when you hold it,
    # to append an instrument that the pattern does not contain yet.
    INSTRUMENT_ADD = 9
    # Velocity lane
    VELOCITY_LANE = 18
    VELOCITY_LANE_SELECTED = 3
    # Transport buttons (right column)
    TRANSPORT = 37
    TRANSPORT_ACTIVE = 21


# ------------------------------------------------------------------------------------------------------------------
# Instrument pad storage. Inherits from dict to stay JSON-serializable.
# ------------------------------------------------------------------------------------------------------------------
class NotePad(dict):

    def __init__(self, note, velocity=100, duration=1.0):
        super().__init__(note=note, velocity=velocity, duration=duration)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


def _migrate_extras(chain):
    """Normalise saved instrument storage to the current ordered form.

    Earlier revisions stored MIDI-learned instruments in a dict keyed by the slot
    they occupied on the top row. Slot positions stopped meaning anything once
    that row became a scrolling window, so those entries become plain additions,
    in slot order, and the old key is dropped. This keeps an instrument learned
    before the upgrade rather than silently losing it.
    """
    if "extras" in chain:
        chain["extras"] = [
            e if isinstance(e, NotePad) else NotePad(**e) for e in chain["extras"]]
    else:
        pads = chain.get("pads", {})
        chain["extras"] = [NotePad(**p) for _, p
                           in sorted(pads.items(), key=lambda kv: int(kv[0]))]
        chain.pop("pads", None)
    return chain["extras"]


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
            chain["extras"] = _migrate_extras(chain)
            chain.setdefault("brush", SS_VELOCITY_DEFAULT)
        for seq, value in state.get("seqs", {}).items():
            self._seqs[int(seq)] = value

    def save(self):
        return {"seqs": self._seqs, "chains": self._chains}

    def get_chain(self, chain_id):
        key = str(chain_id) if chain_id is not None else "default"
        chain = self._chains.get(key)
        if chain is None:
            chain = {"extras": [], "brush": SS_VELOCITY_DEFAULT}
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
        self._selected_note_pad = 0
        self._brush = SS_VELOCITY_DEFAULT
        self._extras = []
        # Notes read out of the pattern itself, ascending. Transient: this is
        # rebuilt from the sequencer and deliberately never saved to the
        # snapshot, so a stale list can never outlive the pattern it came from.
        self._derived_notes = []
        # Cached result of _instrument_list(), rebuilt lazily.
        self._instrument_items = None
        self._instrument_offset = 0
        self._chain_id = None

        self._pressed_steps = {}
        # Absolute step shown at the left edge of the step window. The window is
        # never moved by the playhead - only by the player pressing Up/Down.
        self._step_offset = 0
        self._held_instrument = None
        # The note currently highlighted on the on-screen pattern editor, so the
        # highlight can be withdrawn on release.
        self._ui_note = None
        # Whether the instrument pad being held is the "+" slot, latched on
        # press so a page change during the hold cannot change the answer.
        self._pressed_add_column = False
        # Set when a note is learned during a hold, so the release does not also
        # assign a sequential one.
        self._added_during_hold = False
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
            self._ui_clear_note()
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
        self._extras = chain["extras"]
        pad, brush = self._saved_state.get_selection_by_sequence(phrase * 256 + chan)
        self._selected_note_pad = pad if 0 <= pad < SS_NUM_INSTRUMENTS else 0
        self._brush = brush if 0 <= brush < SS_NUM_VELOCITIES else SS_VELOCITY_DEFAULT
        # The pattern is the default source of instruments, so it has to be read
        # whenever the sequence being edited changes.
        self._step_offset = 0
        self._instrument_offset = 0
        self.rescan_instruments()

    def _save_selection(self):
        self._saved_state.get_chain(self._chain_id)["brush"] = self._brush
        self._saved_state.set_selection_by_sequence(
            self._selected_seq[0] * 256 + self._selected_seq[1],
            self._selected_note_pad, self._brush)

    @property
    def _selected_note(self):
        return self._note_pad_at(self._selected_note_pad)

    def _note_pad_at(self, index):
        """The instrument shown in top-row column `index`, or None if empty.

        `index` is a column on the current page, not an absolute position. When
        that column is the "+" slot it resolves to None as well - callers must
        treat None as "no instrument selected" rather than as note 0.
        """
        items = self._instrument_list()
        position = self._instrument_offset + index
        if 0 <= position < len(items):
            return items[position]
        return None

    def _instrument_list(self):
        """Every instrument reachable, in display order.

        Three sources, merged and de-duplicated by note number:

        1. The pattern's own notes, ascending.
        2. A default bank from SS_FIRST_OFFERED_NOTE, so an empty pattern still
           has something selectable. Notes the pattern already provides are not
           repeated, and the bank neither appears nor disappears as the pattern
           changes, so the row does not collapse under the player mid-edit.
        3. Anything added by hand that neither of the above contains.
        """
        if self._instrument_items is not None:
            return self._instrument_items
        items = [NotePad(note) for note in self._derived_notes]
        seen = set(self._derived_notes)
        for note in range(SS_FIRST_OFFERED_NOTE,
                          SS_FIRST_OFFERED_NOTE + SS_DEFAULT_INSTRUMENTS):
            if note not in seen:
                items.append(NotePad(note))
                seen.add(note)
        for pad in self._extras:
            note = pad["note"]
            if note not in seen:
                items.append(NotePad(note, pad["velocity"], pad["duration"]))
                seen.add(note)
        return items

    def _invalidate_instruments(self):
        """Drop the cached instrument list; call after anything that changes it."""
        self._instrument_items = None

    def _max_instrument_offset(self):
        """Highest page offset worth showing.

        Normally this is the page holding the last instrument. When the count is
        an exact multiple of the page size that page is completely full and has
        no column left for the "+" slot, so one more page is offered - a page
        holding "+" alone. That keeps the slot reachable from an empty pattern,
        where the default bank fills both pages exactly.
        """
        count = len(self._instrument_list())
        if count <= 0:
            return 0
        last = ((count - 1) // SS_NUM_INSTRUMENTS) * SS_NUM_INSTRUMENTS
        if count % SS_NUM_INSTRUMENTS == 0:
            last += SS_NUM_INSTRUMENTS
        return last

    def _set_instrument_page(self, offset):
        """Page the instrument row. Clamped, so the arrows are safe to mash."""
        self._instrument_offset = max(
            0, min(offset, self._max_instrument_offset()))
        self._clamp_selected_instrument()
        self._render_instruments()
        self._render_steps()

    def _clamp_selected_instrument(self):
        """Keep the selection on a column that actually shows an instrument."""
        if self._selected_note is not None:
            return
        for col in range(SS_NUM_INSTRUMENTS):
            if self._note_pad_at(col) is not None:
                self._selected_note_pad = col
                return

    def _is_add_column(self, col):
        """True when this column holds the "+" slot rather than an instrument."""
        return self._instrument_offset + col == len(self._instrument_list())

    def _add_instrument(self, note, velocity=100, duration=1.0):
        """Append an instrument, unless it is already reachable.

        The check covers the pattern as well as previous additions. Returning
        True without changing anything would make note_off believe a note had
        been learned and assign a second, unwanted one on release.
        """
        if note in self._derived_notes:
            return False
        for pad in self._extras:
            if pad["note"] == note:
                return False
        self._extras.append(NotePad(note, velocity, duration))
        self._invalidate_instruments()
        return True

    def _next_free_note(self):
        """The next note to offer when no other device can supply one.

        Sequential from just above everything already in the list, so repeated
        additions are identifiable by their order and never collide with the
        pattern. Returns None only when the whole range is taken.
        """
        used = {pad["note"] for pad in self._instrument_list()}
        if used and max(used) < 127:
            return max(used) + 1
        for note in range(SS_FIRST_OFFERED_NOTE, 128):
            if note not in used:
                return note
        return None

    def _reveal_new_instrument(self):
        """Page to the instrument just appended and select it."""
        position = len(self._instrument_list()) - 1
        self._instrument_offset = (position // SS_NUM_INSTRUMENTS) * SS_NUM_INSTRUMENTS
        self._selected_note_pad = position - self._instrument_offset
        self._save_selection()
        self.refresh()

    def _assign_next_instrument(self):
        """Add the next free note and audition it.

        This is what a tap on "+" does when no note arrived during the hold, so
        an instrument can be added with no second device attached at all. The
        player hears the note immediately and decides whether to keep it.
        """
        note = self._next_free_note()
        if note is None:
            logging.warning("Launchpad X no free MIDI note left for an addition")
            return None
        if not self._add_instrument(note, 100, 1.0):
            return None
        self._reveal_new_instrument()
        self._note_player.play(
            note, self._brush_velocity, 1.0, self._chain_midi_chan())
        return note

    @property
    def _brush_velocity(self):
        return SS_VELOCITY_VALUES[self._brush]

    # ----- geometry ------------------------------------------------------

    def _instrument_pad(self, index):
        return self._driver.get_note_from_xy(index, SS_ROW_INSTRUMENTS)

    def _velocity_pad(self, index):
        return self._driver.get_note_from_xy(index, SS_ROW_VELOCITY)

    def _step_pad(self, index):
        """Pad for a window-relative step, 0-15."""
        row = SS_ROW_STEPS_A if index < 8 else SS_ROW_STEPS_B
        return self._driver.get_note_from_xy(index % 8, row)

    def _note_to_step(self, note):
        """Window-relative step for a pad note, or None if it is not a step pad."""
        col, row = self._driver.get_note_xy(note)
        if row == SS_ROW_STEPS_A:
            return col
        if row == SS_ROW_STEPS_B:
            return 8 + col
        return None

    def _pattern_length(self):
        """Total steps in the pattern, uncapped. 0 when the pattern is empty."""
        try:
            return max(0, self._libseq.getSteps())
        except Exception:
            return 0

    def _max_step_offset(self):
        """Highest window offset that still shows content, snapped to a page."""
        length = self._pattern_length()
        if length <= SS_NUM_STEPS:
            return 0
        return ((length - 1) // SS_NUM_STEPS) * SS_NUM_STEPS

    def _clamp_step_page(self):
        """Keep the window inside the pattern, e.g. after the pattern shortened."""
        self._step_offset = max(0, min(self._step_offset, self._max_step_offset()))

    def _set_step_page(self, offset):
        """Move the step window. Clamped, so the arrows are safe to mash."""
        self._step_offset = max(0, min(offset, self._max_step_offset()))
        self._render_steps()

    def _scan_pattern_notes(self):
        """Distinct notes held by the current pattern, ascending.

        libseq exposes no "list the notes" call, so the notes are discovered by
        probing getNoteStart() across the grid. The loop is note-major so that a
        note which hits early exits its step loop immediately, which keeps the
        common case (a handful of instruments across a short pattern) cheap.
        Bounded by SS_SCAN_MAX_STEPS; a pattern longer than that is scanned only
        in part, and the truncation is logged rather than hidden.
        """
        notes = []
        try:
            steps = self._libseq.getSteps()
            if isinstance(steps, int) and steps > 0:
                if steps > SS_SCAN_MAX_STEPS:
                    logging.warning(
                        f"Launchpad X instrument scan truncated to {SS_SCAN_MAX_STEPS} of {steps} steps")
                    steps = SS_SCAN_MAX_STEPS
                for note in range(128):
                    for step in range(steps):
                        if self._libseq.getNoteStart(step, note) != -1:
                            notes.append(note)
                            break
        except Exception as ex:
            logging.error(f"Launchpad X instrument scan failed: {ex}")
        return notes

    def rescan_instruments(self):
        """Rebuild the instrument row from the current pattern."""
        self._derived_notes = self._scan_pattern_notes()
        self._invalidate_instruments()
        logging.debug(f"Launchpad X instrument scan -> {len(self._derived_notes)} notes")
        # A selection still pointing at a now-empty slot would silently do
        # nothing on a step press, which is precisely the failure this seeding
        # exists to remove. Fall back to the first slot that actually has a note.
        self._instrument_offset = min(self._instrument_offset,
                                      self._max_instrument_offset())
        self._clamp_selected_instrument()
        # The pattern may have been replaced by a shorter one.
        self._clamp_step_page()
        return self._derived_notes

    # ----- input ---------------------------------------------------------

    def note_on(self, note, velocity, shifted_override=None):
        col, row = self._driver.get_note_xy(note)

        if row == SS_ROW_INSTRUMENTS and col < SS_NUM_INSTRUMENTS:
            # The "+" column is a listening slot, not an instrument: holding it
            # and playing a note appends that instrument to the list.
            self._held_instrument = col
            self._pressed_add_column = self._is_add_column(col)
            self._added_during_hold = False
            if self._pressed_add_column:
                self._enable_midi_listening()
                return True
            self._select_instrument(col)
            return True

        if row == SS_ROW_VELOCITY and col < SS_NUM_VELOCITIES:
            self._set_brush(col)
            return True

        index = self._note_to_step(note)
        if index is not None:
            # Keyed by ABSOLUTE step, so changing page while a pad is held
            # cannot make one pad's release land on another.
            step = self._step_offset + index
            if step < self._pattern_length():
                self._pressed_steps[step] = time.time()
                return True

        return False

    def note_off(self, note, shifted_override=None):
        col, row = self._driver.get_note_xy(note)

        if row == SS_ROW_INSTRUMENTS and col < SS_NUM_INSTRUMENTS:
            # Whether this is the "+" column is decided on press. Learning a
            # note during the hold moves the page, so re-checking at release
            # would compare a stale column against a new page and misfire.
            was_add_column = self._pressed_add_column
            self._pressed_add_column = False
            learned = self._added_during_hold
            self._added_during_hold = False
            self._held_instrument = None
            self._ui_clear_note()
            self._disable_midi_listening()
            # Releasing "+" with nothing learned during the hold assigns the
            # next free note, so an instrument can be added with no second
            # device.
            if was_add_column and not learned:
                self._assign_next_instrument()
            return True

        index = self._note_to_step(note)
        if index is not None:
            step = self._step_offset + index
            pressed_at = self._pressed_steps.pop(step, None)
            if pressed_at is not None and step < self._pattern_length():
                if time.time() - pressed_at >= SS_LONG_PRESS_TIME:
                    self._restrike_step(step)
                else:
                    self._toggle_step(step)
            return True

        return False

    def cc_change(self, ccnum, ccval):
        if ccval <= 0:
            return True
        # Arrow cluster: Up/Down move the 16-step window, Left/Right page the
        # instrument row. BACK moved from the up arrow to Capture MIDI because
        # paging needs it; sending BACK flips the screen, which flips the driver
        # back to launcher mode.
        if ccnum == 0x5B:
            self._set_step_page(self._step_offset - SS_NUM_STEPS)
            return True
        if ccnum == 0x5C:
            self._set_step_page(self._step_offset + SS_NUM_STEPS)
            return True
        if ccnum == CC_CAPTURE:
            self._state_manager.send_cuia("BACK")
            return True
        if ccnum == 0x5D:
            self._set_instrument_page(
                self._instrument_offset - SS_NUM_INSTRUMENTS)
            return True
        if ccnum == 0x5E:
            self._set_instrument_page(
                self._instrument_offset + SS_NUM_INSTRUMENTS)
            return True
        # Right column, top button toggles playback.
        col, row = self._driver.get_note_xy(ccnum)
        if col == 8 and row == 0:
            self._toggle_play()
        return True

    # ----- on-screen pattern editor ---------------------------------------
    #
    # Mirrors the pad's instrument selection onto the touchscreen, so the two do
    # not disagree about which note is being edited. This reaches into the GUI
    # directly, which the Zynthian codebase already does from ModeHandlerBase
    # (_get_selected_step) and from the Launchpad Pro MK2 driver. Every call is
    # best-effort: the driver must keep working if the screen is elsewhere, if
    # the note is not in the chain's keymap, or if the UI changes shape.

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

    def _ui_select_note(self, note):
        """Scroll the pattern editor to `note`, select it and highlight it.

        The editor addresses notes by row, resolved through the chain's keymap,
        so a note the patch does not map simply has no row: that is ignored
        rather than treated as a failure.
        """
        pe = self._pattern_editor()
        if pe is None or note is None:
            return False
        try:
            row = pe.get_row_from_note(note)
            if row is None:
                return False
            pe.pianoroll_note_on(note)
            # pianoroll_note_on only moves the selection when the row is off
            # screen, so select explicitly to keep the two in step either way.
            pe.select_cell(None, row)
            self._ui_note = note
            return True
        except Exception as ex:
            logging.error(f"Launchpad X could not update the pattern editor: {ex}")
            return False

    def _ui_clear_note(self):
        """Withdraw the highlight left by _ui_select_note."""
        if self._ui_note is None:
            return False
        note = self._ui_note
        self._ui_note = None
        pe = self._pattern_editor()
        if pe is None:
            return False
        try:
            pe.pianoroll_note_off(note)
            return True
        except Exception as ex:
            logging.error(f"Launchpad X could not clear the pattern editor: {ex}")
            return False

    def _select_instrument(self, index):
        self._selected_note_pad = index
        self._enable_midi_listening()
        # Withdraw any highlight from a previous selection first, so moving
        # between instruments does not leave the old one lit on the screen.
        self._ui_clear_note()
        note_pad = self._selected_note
        if note_pad is not None:
            self._ui_select_note(note_pad["note"])
            self._note_player.play(
                note_pad["note"], self._brush_velocity,
                note_pad["duration"], self._chain_midi_chan())
        self._save_selection()
        self.refresh()

    def _set_brush(self, level):
        self._brush = level
        note_pad = self._selected_note
        if note_pad is not None:
            self._note_player.play(
                note_pad["note"], self._brush_velocity,
                note_pad["duration"], self._chain_midi_chan())
        self._save_selection()
        self.refresh()

    # ----- sequencer operations -----------------------------------------

    def _toggle_step(self, step):
        note_pad = self._selected_note
        if note_pad is None:
            return
        note = note_pad["note"]
        if self._libseq.getNoteStart(step, note) == -1:
            self._libseq.addNote(
                step, note, self._brush_velocity, note_pad["duration"], 0)
            self._note_player.play(
                note, self._brush_velocity, note_pad["duration"],
                self._chain_midi_chan())
        else:
            self._libseq.removeNote(step, note)
        self._render_steps()

    def _restrike_step(self, step):
        note_pad = self._selected_note
        if note_pad is None:
            return
        note = note_pad["note"]
        if self._libseq.getNoteStart(step, note) == -1:
            return
        velocity = self._brush_velocity
        self._libseq.setNoteVelocity(step, note, velocity)
        self._note_player.play(
            note, velocity, note_pad["duration"], self._chain_midi_chan())
        self._render_steps()

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

    # ----- MIDI learn ----------------------------------------------------

    def _enable_midi_listening(self):
        zynsigman.register(zynsigman.S_MIDI, zynsigman.SS_MIDI_NOTE_ON, self._on_midi_note_on)

    def _disable_midi_listening(self):
        zynsigman.unregister(zynsigman.S_MIDI, zynsigman.SS_MIDI_NOTE_ON, self._on_midi_note_on)

    def _on_midi_note_on(self, izmip, chan, note, vel):
        # Capture a note played on another device while an instrument pad is held.
        if self._held_instrument is None or izmip == self._own_device_id:
            return
        if izmip >= self._state_manager.get_zmip_seq_index():
            return
        # Additions go on the end of the list rather than into a fixed slot,
        # because slot positions move once the row pages.
        if not self._add_instrument(note, vel, 1.0):
            # Already known - just move the selection to it.
            self._select_note(note)
            return
        self._added_during_hold = True
        # Additions go on the end of the list rather than into a fixed slot,
        # because slot positions move once the row pages.
        self._reveal_new_instrument()

    def _select_note(self, note):
        """Select a note already present in the list, if it is on the current page."""
        items = self._instrument_list()
        for col in range(SS_NUM_INSTRUMENTS):
            position = self._instrument_offset + col
            if position < len(items) and items[position]["note"] == note:
                self._select_instrument(col)
                return True
        return False

    # ----- external state ------------------------------------------------

    def update_seq_state(self, phrase, chan, state=None, mode=None, group=None):
        if state is not None:
            self._is_playing = state != zynseq.SEQ_STOPPED

    def on_screen_change(self, screen):
        self._current_screen = screen

    # ----- rendering -----------------------------------------------------

    def refresh(self):
        self.control_off()
        self._render_instruments()
        self._render_velocity_lane()
        self._render_steps()
        self._render_transport()

    def control_off(self):
        # Launcher mode paints the grid straight through lib_zyncore, bypassing
        # this LED layer, so those pads are untracked here and would survive a
        # mode switch. The stepsequencer only paints rows 0-3, so anything the
        # launcher left in rows 4-7 has to be cleared explicitly.
        for row in range(8):
            for col in range(8):
                self._leds.pad_off(self._driver.get_note_from_xy(col, row))
        for ccnum in (89, 79, 69, 59, 49, 39, 29, 19):
            self._leds.cc_off(ccnum)
        for arrow in (0x5B, 0x5C, 0x5D, 0x5E):
            self._leds.cc_off(arrow)
        self._leds.cc_off(CC_CAPTURE)

    def _render_instruments(self):
        items = self._instrument_list()
        count = len(items)
        for col in range(SS_NUM_INSTRUMENTS):
            note = self._instrument_pad(col)
            position = self._instrument_offset + col
            if position < count:
                if col == self._selected_note_pad:
                    self._leds.pad_on(note, COLORS.INSTRUMENT_SELECTED, 2)
                else:
                    self._leds.pad_on(note, COLORS.INSTRUMENT)
            elif position == count and self._instrument_offset + SS_NUM_INSTRUMENTS >= count:
                # "+" slot, in the first column after the final instrument.
                self._leds.pad_on(note, COLORS.INSTRUMENT_ADD, 2)
            else:
                # Dim rather than off. An empty slot is information the player
                # needs, and a black pad is indistinguishable from a grid that
                # was never painted.
                self._leds.pad_on(note, COLORS.INSTRUMENT_EMPTY)

    def _render_velocity_lane(self):
        for index in range(SS_NUM_VELOCITIES):
            note = self._velocity_pad(index)
            if index == self._brush:
                self._leds.pad_on(note, COLORS.VELOCITY_LANE_SELECTED, 2)
            else:
                self._leds.pad_off(note)

    def _step_velocity_level(self, step, note):
        """Map a stored step velocity back onto the nearest lane level."""
        velocity = self._libseq.getNoteVelocity(step, note)
        best, best_delta = 0, None
        for level, value in enumerate(SS_VELOCITY_VALUES):
            delta = abs(value - velocity)
            if best_delta is None or delta < best_delta:
                best, best_delta = level, delta
        return best

    def _render_steps(self):
        note_pad = self._selected_note
        length = self._pattern_length()
        for index in range(SS_NUM_STEPS):
            note = self._step_pad(index)
            step = self._step_offset + index
            if step >= length:
                self._leds.pad_off(note)
                continue
            # The playhead wins over everything, including empty steps, so the
            # current position stays visible even with sparse patterns. It is
            # drawn only while it is inside the window: the window never chases
            # the playhead, so on a long pattern it simply leaves the view.
            if step == self._playhead_step:
                self._leds.pad_on(note, COLORS.PLAYHEAD, 2)
                continue
            if note_pad is None or self._libseq.getNoteStart(step, note_pad["note"]) == -1:
                # Absolute step, so beats still line up across a page boundary -
                # 16 is a multiple of 4.
                if step % 4 == 0:
                    self._leds.pad_on(note, COLORS.BEAT_MARKER)
                else:
                    self._leds.pad_off(note)
                continue
            level = self._step_velocity_level(step, note_pad["note"])
            self._leds.pad_on(note, COLORS.VELOCITY_STEP[level])

    def _render_transport(self):
        colour = COLORS.TRANSPORT_ACTIVE if self._is_playing else COLORS.TRANSPORT
        self._leds.cc_on(89, colour, 2 if self._is_playing else 0)

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
                self._render_steps()
        except Exception as ex:
            logging.error(f"Launchpad X playhead update error: {ex}")


# ------------------------------------------------------------------------------------------------------------------
class zynthian_ctrldev_launchpad_x(zynthian_ctrldev_zynpad):

    dev_ids = ["Launchpad X IN 1"]
    driver_name = "Launchpad X"
    driver_description = "Launcher + arrow keys + step sequencer"

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
                # refresh the instruments explicitly rather than showing the
                # previous pattern's.
                self._stepseq.rescan_instruments()
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