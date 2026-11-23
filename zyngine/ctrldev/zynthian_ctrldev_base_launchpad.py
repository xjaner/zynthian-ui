#!/usr/bin/python3
# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Control Device Driver
#
# Shared base for the "Novation Launchpad" control device drivers
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

"""Shared base for Novation Launchpad drivers: clip launcher, piano-roll step
sequencer and keyboard.

A model's driver subclasses zynthian_ctrldev_launchpad_base and sets its
device profile (SysEx device ID, layout SysEx, and the CCs of the buttons
that differ between models). The controls below are described for the
Launchpad X, the first model on this base.

The device runs in its DAW mode, Session layout. The driver has three modes.
The on-screen Pattern Editor decides which one is active: the step sequencer
runs while the editor is the current screen, and the launcher runs otherwise.

Launcher
    Grid pads      Tap to launch or stop a sequence: the column is the chain
                   and the row is the phrase, both following the launcher's
                   scroll. The tap acts on release. A long press (0.5 s)
                   opens that pattern in the Pattern Editor instead, without
                   starting it.
    Right column   Launch or stop a whole phrase.
    Arrows         Sent as ARROW_UP/DOWN/LEFT/RIGHT to the Zynthian UI.
    Note           Keyboard (below).

Keyboard
    The grid plays the active chain, with velocity and pressure. The pads
    follow the scale and root note of the pattern last opened in the
    Pattern Editor: each pad to the right is the next note of the scale, and
    each row is a fourth higher. A chromatic pattern, a custom keymap or no
    pattern gives a chromatic layout. Root notes are lit in the chain's
    colour, other notes grey (black keys dim when chromatic), and sounding
    notes green.
    Up / Down      One octave higher / lower.
    Left / Right   One scale step lower / higher.
    Right column   3: drum chains only, drum pads / keyboard (green = drums).
                   4: sustain pedal, while held.
    Session        Back to the launcher, or to the step sequencer.
    A note's chain and pitch are fixed when its pad goes down, so it stops
    properly even if the chain or the octave changes while it is held.

Keyboard over the Pattern Editor (Note pressed in the step sequencer)
    The keyboard plays the edited pattern's chain and can record into it.
    Right column   1: play / stop the pattern.
                   2: record, the editor's own record button (dim red,
                      pulsing bright red while armed).
                   8: tap to undo, long press to redo, as in the step
                      sequencer: recordings made here can be taken back
                      without leaving the keyboard. Lit only over the
                      editor, where there is a pattern to undo.
    While playing, a recorded note goes in at the nearest step, as long as
    it was held, in whole steps. While stopped, notes are entered a step at
    a time at the playhead, as from a MIDI keyboard: a note adds a one-step
    note (or removes the one already there) and moves on a step. While
    sustain is held, notes stack into a chord and lifting it moves on.
    Leaving the editor on the screen goes back to the launcher.

Drum pads
    A chain whose synth has a drum map (Fabla, an SFZ kit with a keymap, or
    a preset with a midnam file, such as the General MIDI kits) gets one pad
    per sound instead of the keyboard, in four 4x4 blocks: bottom left,
    bottom right, top left, top right, lowest note first. Pads are coloured
    by the kind of sound, read from its name: kick red, snare orange, clap
    pink, hi-hat yellow, tom green, cymbal cyan, other percussion purple,
    anything else the chain's colour. A sounding pad lights white.
    Up / Down      The next / previous 16 sounds, for kits with more than 64.

    The device's own Note layout isn't used, because its pads send on the
    MIDI port (IN 2), which goes straight to the chains. In Session layout
    the pads send on the DAW port, with velocity and polyphonic pressure.

Step sequencer (Pattern Editor open)
    The grid mirrors the screen's piano roll. Each row is one note of the
    chain's keymap, with the highest note on the top row, and each column is
    one step, so 8 notes x 8 steps are visible.

    Grid pads      Tap: add a note (using the editor's note length, heard
                   while stopped) or remove the note under the pad.
                   Long press: select the note, or create one there.
                   Hold one pad and tap another in the same row: stretch the
                   held note to reach the tapped pad. Sliding along the row
                   right after a long press stretches the note the same way.
    Up / Down      Move the cursor row; hold to repeat. The screen follows,
                   and while stopped the row's note plays.
    Left / Right   Page through the steps, 8 at a time. Paging by hand
                   stops the page following the playhead.
    Right column   1: play / stop.
                   2-6: velocity brush, 25 to 127, for new notes. Pressed
                        while note pads are held, it also sets those notes'
                        velocity.
                   7: follow the playhead on / off (bright blue when on).
                   8: tap to undo, long press to redo.
    Capture MIDI   BACK.
    Note           The keyboard, over the editor (above).
    Session        Back to the launcher.

    Colours: a note's first step runs from dim green to white with velocity,
    the rest of a long note is grey, and empty first steps of each beat are
    dim grey. In the playhead column, notes light white and empty cells are
    blue. The cursor row's notes pulse and its empty cells are orange. An
    arrow is lit when there is more to scroll to in its direction.

Internals
    StepSeqHandler   The step sequencer. It edits the pattern through libseq
                     and reads the keymap, the note length and the default
                     note data from the Pattern Editor screen. It sets the
                     editor's "changed" flag so that pad edits can be undone.
    FeedbackLEDs     Caches what each LED shows and only sends changes. The
                     launcher paints without the cache, so the grid is
                     cleared in full on entering the step sequencer.
    PlayheadMonitor  Polls libseq every 15 ms for the playhead and the play
                     state. Every 7th poll it also checks for a different
                     pattern, keymap, length or sequence on the screen, and
                     repaints, so touchscreen edits appear on the pad.
    KeysHandler      The keyboard and drum pads. Over the editor it records
                     by writing notes through libseq itself: libseq only
                     records its JACK input, and the notes the driver plays
                     go out of its output. A 100 ms poll follows the
                     editor's record button, the play state and the scale.
    ArrowRepeater    Generates the key repeat for held Up/Down arrows.
    StepSeqState     Saved in the snapshot: the brush per chain, and the
                     cursor row per sequence.
"""

import os
import json
import logging
import time
from math import ceil
from xml.dom import minidom
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
# Novation Launchpad base
# ------------------------------------------------------------------------------------------------------------------

# Driver modes
MODE_LAUNCHER = "launcher"
MODE_STEPSEQ = "stepseq"

# Top-row buttons of the Launchpad X, the defaults of the device profile,
# measured on hardware in Session layout:
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
# Button 7 turns following the playhead on and off; button 8 is undo (tap)
# and redo (long press).
CC_FOLLOW = SS_RIGHT_COL_CCS[6]
CC_UNDO = SS_RIGHT_COL_CCS[7]
# The arrow cluster: Up, Down, Left, Right.
CC_UP, CC_DOWN, CC_LEFT, CC_RIGHT = 0x5B, 0x5C, 0x5D, 0x5E

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

# Keyboard. Each row is a fourth above the one below, like the device's own
# Note layout: 3 scale steps in a scale, 5 semitones when chromatic.
KEYS_ROW_DEGREES = 3
KEYS_ROW_SEMITONES = 5
# The bottom-left pad starts on this note (C3), or on the scale's first note
# from there up.
KEYS_DEFAULT_NOTE = 48
# Right column button 3 switches a drum chain between drum pads and the
# keyboard; button 4 is the sustain pedal.
CC_KEYS_DRUMS = SS_RIGHT_COL_CCS[2]
CC_SUSTAIN = SS_RIGHT_COL_CCS[3]
# Over the Pattern Editor, buttons 1 and 2 play / stop the edited pattern
# and arm recording into it.
CC_KEYS_PLAY = SS_RIGHT_COL_CCS[0]
CC_KEYS_RECORD = SS_RIGHT_COL_CCS[1]
# The keyboard over the editor polls the playhead as often as the step
# sequencer does, so the pads can echo the pattern's notes, and looks at
# the editor's record button, the play state and the pattern's scale every
# KEYS_SYNC_EVERY polls (about every 100 ms).
KEYS_POLL_INTERVAL = SS_PLAYHEAD_INTERVAL
KEYS_SYNC_EVERY = 7
# The pattern editor's data: scales.json, keymaps.json and the midnam drum
# maps. libseq's scale 0 is a custom keymap and 1 is chromatic; scale N > 1
# is entry N - 1 of scales.json.
KEYS_DATA_DIR = "/zynthian/zynthian-data/zynseq"
KEYS_SCALES_FPATH = KEYS_DATA_DIR + "/scales.json"
# Drum pads come in four 4x4 blocks; Up/Down move the kit one block.
KEYS_DRUM_BLOCK = 16
# Drum sound families, matched in this order against the lower-case sound
# name, and the colour each one is lit in.
KEYS_DRUM_FAMILIES = (
    (("kick", "bass drum", "bassdrum"), "DRUM_KICK"),
    (("snare", "rim", "stick"), "DRUM_SNARE"),
    (("clap", "snap"), "DRUM_CLAP"),
    (("hat", "hh"), "DRUM_HAT"),
    (("tom", "roto"), "DRUM_TOM"),
    (("cymbal", "crash", "ride", "splash", "china", "cym"), "DRUM_CYMBAL"),
    (("conga", "bongo", "cowbell", "shaker", "tamb", "clave", "block", "agogo",
      "cabasa", "guiro", "maraca", "whistle", "triangle", "cuica", "timbale",
      "slap", "perc", "bell"), "DRUM_PERC"),
)
# Pitch classes of the black keys, dimmed in the chromatic layout.
KEYS_BLACK_KEYS = (1, 3, 6, 8, 10)


class COLORS:
    """Launchpad palette indices, measured on a Launchpad X.

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
    # The playhead column: notes in it light white as they play, and its
    # empty cells are blue, so the notes stay readable under the playhead.
    PLAYHEAD = 3
    PLAYHEAD_EMPTY = 46
    # Empty cells of the cursor's row, so the cursor shows on a row with no
    # notes. Orange is not used anywhere else in the grid.
    CURSOR_ROW = 11
    CURSOR_ROW_BEAT = 10
    # Arrows, lit when there is more to scroll to, and Capture MIDI (BACK).
    ARROW = 2
    BACK = 1
    # Follow on/off: blue, like the playhead it follows.
    FOLLOW_ON = 45
    FOLLOW_OFF = 47
    # Undo: purple, so it reads as its own function next to the blue follow
    # button and the red record button.
    UNDO = 49
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
    # Keyboard. The root notes are lit in the chain's colour (this one if
    # the chain has none); other notes grey, the chromatic layout's black
    # keys dim, and a sounding note green.
    KEY_ROOT = 41
    KEY = 2
    KEY_BLACK = 1
    KEY_PRESSED = 21
    # A note the edited pattern is playing, on the keyboard or drum pads.
    KEY_ECHO = 45
    SUSTAIN = 11
    SUSTAIN_ON = 9
    # Drum pads, by sound family; other sounds take the chain's colour.
    DRUM_KICK = 5
    DRUM_SNARE = 9
    DRUM_CLAP = 53
    DRUM_HAT = 13
    DRUM_TOM = 17
    DRUM_CYMBAL = 37
    DRUM_PERC = 49
    DRUM_PRESSED = 3
    # Right column button 3 on a drum chain: drum pads or keyboard.
    KEYS_DRUMS_ON = 21
    KEYS_DRUMS_OFF = 2
    # Right column button 2 over the editor: record armed (pulsing) or not.
    # Dim red when idle, full red while armed, so the pulsing is the change.
    RECORD = 7
    RECORD_ON = 5


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
                logging.error(f"Launchpad playhead monitor error: {ex}")
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
                logging.error(f"Launchpad arrow repeat error: {ex}")


# ------------------------------------------------------------------------------------------------------------------
# LED output with change tracking.
#
# The Launchpad keeps a lighting canvas of 81 slots; sending a colour updates
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

    def forget(self):
        """Drop the tracked state without sending anything, after the
        device has been cleared some other way."""
        with self._lock:
            self._pads.clear()
            self._ccs.clear()

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
# ------------------------------------------------------------------------------------------------------------------
# The on-screen Pattern Editor
# ------------------------------------------------------------------------------------------------------------------
#
# The pad and the screen are two views of the same pattern. These reach into
# the GUI directly, which the Zynthian codebase already does from
# ModeHandlerBase (_get_selected_step) and from the Launchpad Pro MK2 driver.
# Every call is best-effort: the driver must keep working if the screen is
# elsewhere or if the UI changes shape.

def _shown_pattern_editor():
    """The on-screen pattern editor, or None when it is not the current screen."""
    try:
        zyngui = zynthian_gui_config.zyngui
        if zyngui is None or zyngui.current_screen != "pattern_editor":
            return None
        return zyngui.screens.get("pattern_editor")
    except Exception as ex:
        logging.debug(f"Launchpad pattern editor unavailable: {ex}")
        return None


def _undo_pattern(redo=False):
    """Step the pattern editor's snapshot stack back (undo) or forward
    (redo), so the pad and the screen share one history.

    Both mode handlers can record into the edited pattern, so both undo
    through here rather than keeping a history of their own. Returns True if
    there was an editor to act on.
    """
    pe = _shown_pattern_editor()
    if pe is None:
        return False
    try:
        if redo:
            pe.redo_pattern()
        else:
            pe.undo_pattern()
    except Exception as ex:
        logging.error(f"Launchpad could not {'redo' if redo else 'undo'}: {ex}")
        return False
    return True


def _flag_pattern_changed():
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
        logging.debug(f"Launchpad could not flag the pattern as changed: {ex}")


def _write_note(libseq, step, note, velocity, duration, offset=0.0):
    """Add a note to the selected pattern with the editor's default note
    data (play chance, stutter...), so that a note entered on the pad is
    the same as one entered on the screen."""
    libseq.addNote(step, note, velocity, duration, offset)
    pe = _shown_pattern_editor()
    if pe is not None:
        try:
            evdata = pe.get_default_note_evdata()
            # setNoteData overwrites the velocity, but not the start,
            # offset or duration; keep the velocity given.
            evdata.val2_start = velocity
            libseq.setNoteData(step, note, evdata)
        except Exception as ex:
            logging.debug(f"Launchpad could not set the note data: {ex}")


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
        # While True, the step window jumps to the playhead's page as the
        # pattern plays. A manual page turns it off; button 7 toggles it.
        self._follow = True
        # Press time of the undo button, to tell undo (tap) from redo.
        self._undo_pressed_at = None
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
            self._follow = True
            self._undo_pressed_at = None
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
                logging.debug(f"Launchpad could not read the keymap: {ex}")
        self._keymap = keymap
        self._pattern_index = self._current_pattern_index()
        self._pattern_steps = self._pattern_length()
        self._step_offset = max(0, min(self._step_offset, self._max_step_offset()))
        logging.debug(f"Launchpad keymap -> {len(self._keymap)} rows")
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
            logging.debug(f"Launchpad could not sync the pattern editor: {ex}")

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
        if ccnum in (CC_UP, CC_DOWN):
            direction = 1 if ccnum == CC_UP else -1
            if ccval > 0:
                self._select_row(self._selected_row + direction)
                # Play the instrument the cursor landed on, so a sound can be
                # found by ear. Only on the press: the repeat would be noise.
                self._audition_selected_row()
                self._repeater.start_repeat(direction)
            else:
                self._repeater.stop_repeat()
            return True
        # Undo and redo act on release, where the press length is known.
        if ccnum == CC_UNDO:
            if ccval > 0:
                self._undo_pressed_at = time.time()
            elif self._undo_pressed_at is not None:
                held = time.time() - self._undo_pressed_at
                self._undo_pressed_at = None
                self._undo(redo=held >= SS_LONG_PRESS_TIME)
            return True
        # Everything below acts on the press only: the device sends a CC
        # with value 0 on release, which would otherwise act a second time.
        if ccval <= 0:
            return True
        if ccnum in (CC_LEFT, CC_RIGHT):
            # Paging by hand means "let me look here": stop following the
            # playhead, or the next step would snap the window back.
            self._set_follow(False)
            delta = SS_NUM_COLS if ccnum == CC_RIGHT else -SS_NUM_COLS
            self._set_step_offset(self._step_offset + delta)
            return True
        if ccnum == self._driver.cc_back:
            self._state_manager.send_cuia("BACK")
            return True
        # The control strip down the right-hand side.
        if ccnum == CC_TRANSPORT:
            self._toggle_play()
            return True
        if ccnum == CC_FOLLOW:
            self._set_follow(not self._follow)
            return True
        for index, brush_cc in enumerate(SS_VELOCITY_CCS):
            if ccnum == brush_cc:
                self._set_brush(index)
                # A brush pressed while note pads are held re-colours those
                # notes, so a velocity can be changed without re-entering.
                self._apply_brush_to_held_notes()
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

    def _apply_brush_to_held_notes(self):
        """Give every note under a held pad the brush's velocity.

        The press is then consumed, so releasing the pad does not toggle the
        note away. A held empty pad is left alone: its release still enters a
        note, now at the new velocity.
        """
        changed = False
        for pressed in self._pressed_cells.values():
            if pressed["consumed"]:
                continue
            index = pressed["index"]
            if not 0 <= index < len(self._keymap):
                continue
            note = self._keymap[index]["note"]
            start = self._libseq.getNoteStart(pressed["step"], note)
            if start == -1:
                continue
            self._libseq.setNoteVelocity(start, note, self._brush_velocity)
            pressed["consumed"] = True
            changed = True
        if changed:
            self._mark_pattern_changed()
            self._render_grid()

    def _audition_selected_row(self):
        if 0 <= self._selected_row < len(self._keymap):
            self._audition(self._keymap[self._selected_row]["note"], 1.0)

    def _set_follow(self, follow):
        if follow == self._follow:
            return
        self._follow = follow
        if follow and self._playhead_step is not None:
            # Catch up with the playhead straight away.
            self._step_offset = max(0, min(
                (self._playhead_step // SS_NUM_COLS) * SS_NUM_COLS,
                self._max_step_offset()))
            self._render_grid()
        self._render_controls()

    def _undo(self, redo=False):
        """Undo or redo through the pattern editor's own snapshot stack, so
        the pad and the screen share one history."""
        if _undo_pattern(redo):
            self._render_grid()

    def _select_cell(self, step, index):
        """Move the cursor to a cell and show it on the touchscreen too."""
        self._selected_step = step
        self._selected_row = index
        self._scroll_row_window()
        self._sync_screen()
        self._render_grid()

    # ----- on-screen pattern editor ---------------------------------------

    def _pattern_editor(self):
        return _shown_pattern_editor()

    def _mark_pattern_changed(self):
        _flag_pattern_changed()

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
        _write_note(self._libseq, step, note, self._brush_velocity, duration)
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
            logging.debug(f"Launchpad could not audition a note: {ex}")

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
        for arrow in (CC_UP, CC_DOWN, CC_LEFT, CC_RIGHT):
            self._leds.cc_off(arrow)
        self._leds.cc_off(self._driver.cc_back)

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
                    # getNoteStart returns the step a note covering this cell
                    # starts at, so a note longer than a step shows its first
                    # step lit and the rest as a tail.
                    start = self._libseq.getNoteStart(step, note)
                    if step == self._playhead_step:
                        # The playhead column: notes light white as they
                        # play, empty cells show the column in blue.
                        if start == -1:
                            self._leds.pad_on(pad, COLORS.PLAYHEAD_EMPTY)
                        else:
                            self._leds.pad_on(pad, COLORS.PLAYHEAD)
                        continue
                    if start == -1:
                        # An empty cell on a downbeat is marked, so the beat
                        # grid is readable at a glance. The cursor's row is
                        # tinted, so the cursor shows on an empty row too.
                        downbeat = step % steps_per_beat == 0
                        if selected:
                            self._leds.pad_on(pad, COLORS.CURSOR_ROW_BEAT
                                              if downbeat else COLORS.CURSOR_ROW)
                        elif downbeat:
                            self._leds.pad_on(pad, COLORS.BEAT_MARKER)
                        else:
                            self._leds.pad_off(pad)
                        continue
                    # The selected instrument's notes pulse, so the cursor is
                    # obvious without hiding the note colours.
                    channel = 2 if selected else 0
                    if start == step:
                        level = self._step_velocity_level(step, note)
                        self._leds.pad_on(pad, COLORS.VELOCITY_STEP[level], channel)
                    else:
                        self._leds.pad_on(pad, COLORS.NOTE_TAIL, channel)
            self._render_arrows()

    def _render_arrows(self):
        """Light each arrow that has somewhere to go.

        Up and Down light while there are rows above or below the window
        (the top of the pad shows the highest rows), Left and Right while
        there is another page of steps.
        """
        lit = {
            CC_UP: self._row_offset < self._max_row_offset(),
            CC_DOWN: self._row_offset > 0,
            CC_LEFT: self._step_offset > 0,
            CC_RIGHT: self._step_offset < self._max_step_offset(),
        }
        for ccnum, on in lit.items():
            if on:
                self._leds.cc_on(ccnum, COLORS.ARROW)
            else:
                self._leds.cc_off(ccnum)

    def _render_controls(self):
        """Draw the control strip: transport, the velocity brush, follow and
        undo, and Capture MIDI as BACK."""
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
            self._leds.cc_on(CC_FOLLOW, COLORS.FOLLOW_ON if self._follow
                             else COLORS.FOLLOW_OFF)
            self._leds.cc_on(CC_UNDO, COLORS.UNDO)
            self._leds.cc_on(self._driver.cc_back, COLORS.BACK)

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
                    if new_step is not None and self._follow:
                        page = (new_step // SS_NUM_COLS) * SS_NUM_COLS
                        if page != self._step_offset:
                            self._step_offset = max(
                                0, min(page, self._max_step_offset()))
                    repaint = True
                if repaint:
                    self._render_grid()
        except Exception as ex:
            logging.error(f"Launchpad playhead update error: {ex}")


# ------------------------------------------------------------------------------------------------------------------
# Keyboard mode handler
# ------------------------------------------------------------------------------------------------------------------


class KeysHandler(ModeHandlerBase):
    """The keyboard: the grid plays the active chain, in key.

    The pads follow the scale and root note of the pattern last opened in
    the Pattern Editor, which is the one libseq has selected. Each pad to
    the right is the next note of the scale and each row is a fourth higher.
    A chromatic or custom-keymap pattern, or none, gives the chromatic
    layout, with the black keys dim.

    A pad's "position" counts from note 0 in scale steps, or in semitones
    when chromatic. The bottom-left pad's position is the origin, which the
    arrows move.

    A chain whose synth has a drum map gets drum pads instead: one pad per
    sound, in four 4x4 blocks (bottom left, bottom right, top left, top
    right), coloured by the kind of sound. The map comes from the synth
    itself (Fabla, SFZ with a keymap) or from a midnam file listed in
    keymaps.json, as in the Pattern Editor. Right column button 3 switches
    such a chain to the keyboard and back.

    Opened over the Pattern Editor, the keyboard plays the edited
    sequence's chain instead, and can record into its pattern: button 1
    plays / stops it and button 2 is the editor's own record button. While
    the pattern plays, a note goes in at the step nearest to where it was
    played, as long as it was held, in whole steps. While it is stopped,
    notes are entered a step at a time at the playhead, like libseq's own
    step capture from a MIDI keyboard: see _step_entry().

    While the edited pattern plays, the pads of the notes sounding at its
    playhead light up (the "echo"), so the pattern can be followed, and
    played along with, on the pads.
    """

    def __init__(self, state_manager, driver):
        super().__init__(state_manager)
        self._driver = driver
        self._libseq = self._zynseq.libseq
        self._leds = FeedbackLEDs(driver)
        # Taken by the MIDI thread and by refreshes from signal handlers.
        self._lock = RLock()
        self._scales = None
        # The scale's semitone offsets from the root, or None when chromatic.
        self._intervals = None
        self._tonic = 0
        # The (scale, tonic) the layout was built from, None before the first.
        self._layout_src = None
        self._origin = KEYS_DEFAULT_NOTE
        # Sounding notes by pad: (channel, note), fixed at note-on so that
        # the note-off reaches the same chain and note even if the chain or
        # the octave changes while the pad is held.
        self._held = {}
        # The channel the sustain pedal went down on, None while it is up.
        self._sustain_chan = None
        # The active chain's drum map, sorted by note, or None.
        self._drums = None
        # What the drum map was found from, so it is only looked up again
        # when the chain or its preset changes.
        self._drum_src = None
        # Index of the drum map entry on the bottom-left pad.
        self._drum_offset = 0
        # Chains switched from drum pads to the keyboard with button 3.
        self._keyboard_chains = set()
        # The edited sequence, (phrase, chan), while the keyboard is open
        # over the Pattern Editor; None over the launcher.
        self._seq = None
        # Notes being recorded while the pattern plays, by pad:
        # (note, velocity, the sequence's play position at note-on in clocks).
        self._takes = {}
        # What the play and record buttons show, read by the poll.
        self._playing = False
        self._recording = False
        # Press time of the undo button, to tell undo (tap) from redo. The
        # keyboard can record into the pattern, so it owns the same undo
        # button as the step sequencer.
        self._undo_pressed_at = None
        # The notes on the grid the edited pattern is playing at its
        # playhead, and the step they were read at (None to read again).
        self._echo = frozenset()
        self._echo_step = None
        self._poll_count = 0
        # Over the editor, follows its record button, the play state, the
        # pattern's scale and the sequence being edited.
        self._poll = PlayheadMonitor(self._poll_editor, KEYS_POLL_INTERVAL)

    def end(self):
        self._poll.set_enabled(False)
        self._poll.end()
        self.release_all()

    def set_active(self, active, seq=None):
        """Show or hide the keyboard. seq is the edited sequence when it is
        opened over the Pattern Editor."""
        with self._lock:
            if active:
                self._seq = seq
                self._undo_pressed_at = None
                super().set_active(True)
                # The device was cleared by the driver, so forget what was lit.
                self._leds.forget()
                self._read_editor_state()
                self.refresh()
            else:
                self.release_all()
                super().set_active(False)
                self._leds.all_off()
                self._seq = None
                self._echo, self._echo_step = frozenset(), None
        self._poll.set_enabled(active and seq is not None)

    # ----- layout ----------------------------------------------------------

    def _scale_intervals(self, scale):
        """The semitone offsets of a libseq scale, or None for chromatic."""
        if scale < 2:
            return None
        if self._scales is None:
            try:
                with open(KEYS_SCALES_FPATH) as fh:
                    self._scales = json.load(fh)
            except Exception as ex:
                logging.error(f"Launchpad can't load the scales: {ex}")
                self._scales = []
        try:
            intervals = sorted({int(i) % 12 for i in self._scales[scale - 1]["scale"]})
        except Exception:
            return None
        if not intervals or len(intervals) == 12:
            return None
        return intervals

    def _load_layout(self):
        """Follow the selected pattern's scale and root, keeping the
        bottom-left pad as close as possible to the note it was on."""
        try:
            scale = self._libseq.getScale()
            tonic = self._libseq.getTonic() % 12
        except Exception:
            scale, tonic = 0, 0
        if (scale, tonic) == self._layout_src:
            return
        bottom = KEYS_DEFAULT_NOTE if self._layout_src is None else self._note_at(self._origin)
        self._layout_src = (scale, tonic)
        self._intervals = self._scale_intervals(scale)
        self._tonic = tonic
        self._origin = self._position_of(bottom)

    def _load_drums(self):
        """Look up the active chain's drum map, if its synth or preset
        changed since the last look."""
        chan = self._chain_chan()
        proc = None
        if chan is not None:
            try:
                proc = self._chain_manager.get_synth_processor(chan)
            except Exception:
                proc = None
        src = None
        if proc is not None:
            try:
                src = (chan, id(proc), proc.get_keymap_name() or proc.get_presetpath())
            except Exception:
                src = (chan, id(proc), None)
        if src == self._drum_src:
            return
        self._drum_src = src
        self._drum_offset = 0
        self._drums = self._find_drum_map(proc) if proc is not None else None

    def _find_drum_map(self, proc):
        """The drum map of a synth processor, sorted by note, or None.

        The same sources as the Pattern Editor's custom keymap: the synth's
        own map, then a midnam file matched to the preset in keymaps.json.
        """
        keymap = None
        try:
            if proc.get_keymap_name():
                keymap = proc.get_keymap()
        except Exception as ex:
            logging.debug(f"Launchpad can't get the synth's keymap: {ex}")
        if not keymap:
            keymap = self._midnam_keymap(proc)
        entries = {}
        for entry in keymap or ():
            try:
                note = int(entry["note"])
            except Exception:
                continue
            if 0 <= note <= 127 and note not in entries:
                entries[note] = {"note": note, "name": str(entry.get("name", ""))}
        return [entries[note] for note in sorted(entries)] or None

    @staticmethod
    def _midnam_keymap(proc):
        try:
            preset_path = proc.get_presetpath()
            with open(f"{KEYS_DATA_DIR}/keymaps.json") as fh:
                patterns = json.load(fh)
        except Exception:
            return None
        if not preset_path:
            return None
        map_name = next((name for pat, name in patterns.items() if pat in preset_path), None)
        fpath = f"{KEYS_DATA_DIR}/{map_name}.midnam"
        if not map_name or not os.path.isfile(fpath):
            return None
        try:
            notes = minidom.parse(fpath).getElementsByTagName("Note")
            return [{"note": int(n.attributes["Number"].value),
                     "name": n.attributes["Name"].value} for n in notes]
        except Exception as ex:
            logging.error(f"Launchpad can't load the drum map '{fpath}': {ex}")
            return None

    def _drums_shown(self):
        """True if the grid shows drum pads rather than the keyboard."""
        if not self._drums:
            return False
        return self._chain_id() not in self._keyboard_chains

    def _drum_index(self, col, prow):
        """The drum map index of a pad: four 4x4 blocks, bottom left first,
        then bottom right, top left and top right."""
        block = (prow // 4) * 2 + col // 4
        return self._drum_offset + block * KEYS_DRUM_BLOCK + (prow % 4) * 4 + col % 4

    def _drum_entry(self, col, prow):
        index = self._drum_index(col, prow)
        if 0 <= index < len(self._drums):
            return self._drums[index]
        return None

    def _max_drum_offset(self):
        extra = len(self._drums or ()) - 4 * KEYS_DRUM_BLOCK
        return max(0, ceil(extra / KEYS_DRUM_BLOCK) * KEYS_DRUM_BLOCK)

    def _shift_drums(self, delta):
        with self._lock:
            offset = self._drum_offset + delta
            if 0 <= offset <= self._max_drum_offset():
                self._drum_offset = offset
                self.refresh()

    def _toggle_drums(self):
        with self._lock:
            chain_id = self._chain_id()
            if not self._drums or chain_id is None:
                return
            # Stop what is sounding: its pads are about to mean other notes.
            self.release_all()
            if chain_id in self._keyboard_chains:
                self._keyboard_chains.discard(chain_id)
            else:
                self._keyboard_chains.add(chain_id)
            self.refresh()

    @staticmethod
    def _drum_family_colour(name):
        name = name.lower()
        for words, colour in KEYS_DRUM_FAMILIES:
            if any(word in name for word in words):
                return getattr(COLORS, colour)
        return None

    def _note_at(self, position):
        if self._intervals is None:
            return position
        n = len(self._intervals)
        return self._tonic + 12 * (position // n) + self._intervals[position % n]

    def _position_of(self, note):
        """The position of the lowest note of the layout at or above note."""
        if self._intervals is None:
            return note
        position = ((note - self._tonic) // 12) * len(self._intervals)
        while self._note_at(position) < note:
            position += 1
        return position

    def _row_step(self):
        return KEYS_ROW_SEMITONES if self._intervals is None else KEYS_ROW_DEGREES

    def _octave_step(self):
        return 12 if self._intervals is None else len(self._intervals)

    def _pad_note(self, col, prow):
        """The note of a pad, by column and row from the bottom, or None if
        it plays nothing."""
        if self._drums_shown():
            entry = self._drum_entry(col, prow)
            return entry["note"] if entry is not None else None
        note = self._note_at(self._origin + col + prow * self._row_step())
        return note if 0 <= note <= 127 else None

    @staticmethod
    def _pad_xy(pad):
        """A grid pad's column and row from the bottom, or None."""
        col, prow = pad % 10 - 1, pad // 10 - 1
        if 0 <= col < 8 and 0 <= prow < 8:
            return col, prow
        return None

    def _can_shift(self, delta):
        """True if the bottom-left pad stays inside the MIDI range."""
        return 0 <= self._note_at(self._origin + delta) <= 127

    def _shift(self, delta):
        with self._lock:
            if self._can_shift(delta):
                self._origin += delta
                self.refresh()

    # ----- playing -----------------------------------------------------------

    def _chain_chan(self):
        """The MIDI channel the keyboard plays: the edited sequence's over
        the Pattern Editor, the active chain's otherwise. None if there is
        none it can be played on."""
        try:
            if self._seq is not None:
                chan = self._libseq.getChannel(self._zynseq.scene, *self._seq, 0)
            else:
                chan = self._chain_manager.get_active_chain().midi_chan
        except Exception:
            return None
        if isinstance(chan, int) and 0 <= chan < MAX_NUM_MIDI_CHANS:
            return chan
        return None

    def _chain_id(self):
        """The id of the chain the keyboard plays, or None."""
        try:
            if self._seq is not None:
                return self._get_chain_id_by_sequence(*self._seq)
            return self._chain_manager.get_active_chain().chain_id
        except Exception:
            return None

    def _send(self, status, value1, value2=0):
        try:
            self._libseq.sendMidiCommand(status, value1, value2)
        except Exception as ex:
            logging.error(f"Launchpad could not send a note: {ex}")

    def note_on(self, pad, velocity, shifted_override=None):
        with self._lock:
            xy = self._pad_xy(pad)
            if xy is None or pad in self._held:
                return
            note = self._pad_note(*xy)
            chan = self._chain_chan()
            if note is None or chan is None:
                return
            self._held[pad] = (chan, note)
            self._send(0x90 | chan, note, velocity)
            if self._seq is not None:
                self._record_note_on(pad, note, velocity)
            self._render_grid()

    def note_off(self, pad, shifted_override=None):
        with self._lock:
            held = self._held.pop(pad, None)
            if held is None:
                return
            chan, note = held
            self._send(0x80 | chan, note, 0)
            self._record_note_off(pad)
            self._render_grid()

    def pressure(self, pad, value):
        """Polyphonic pressure follows its pad's note."""
        held = self._held.get(pad)
        if held is not None:
            self._send(0xA0 | held[0], held[1], value)

    def channel_pressure(self, value):
        chans = {chan for chan, _ in self._held.values()}
        if not chans:
            chan = self._chain_chan()
            chans = {chan} if chan is not None else set()
        for chan in chans:
            self._send(0xD0 | chan, value)

    def _sustain(self, down):
        with self._lock:
            if down:
                if self._sustain_chan is not None:
                    return
                chan = self._chain_chan()
                if chan is None:
                    return
                self._sustain_chan = chan
                self._send(0xB0 | chan, 64, 127)
            else:
                if self._sustain_chan is None:
                    return
                self._send(0xB0 | self._sustain_chan, 64, 0)
                self._sustain_chan = None
                # In step entry, lifting the pedal moves on to the next step.
                if self._is_recording() and not self._seq_playing():
                    self._advance_step()
            self._render_controls()

    def release_all(self):
        """Stop every sounding note, and lift the sustain pedal."""
        with self._lock:
            held, self._held = self._held, {}
            # Notes cut short are not recorded.
            self._takes.clear()
            for chan, note in held.values():
                self._send(0x80 | chan, note, 0)
            if self._sustain_chan is not None:
                self._send(0xB0 | self._sustain_chan, 64, 0)
                self._sustain_chan = None

    def cc_change(self, ccnum, ccval):
        if ccnum == CC_SUSTAIN:
            self._sustain(ccval > 0)
            return
        # Undo and redo act on release, where the press length is known, so
        # this has to come before the press-only handling below.
        if ccnum == CC_UNDO:
            if ccval > 0:
                self._undo_pressed_at = time.time()
            elif self._undo_pressed_at is not None:
                held = time.time() - self._undo_pressed_at
                self._undo_pressed_at = None
                self._undo(redo=held >= SS_LONG_PRESS_TIME)
            return
        if ccval == 0:
            return
        if ccnum in (CC_KEYS_PLAY, CC_KEYS_RECORD):
            if self._seq is not None:
                if ccnum == CC_KEYS_PLAY:
                    self._toggle_play()
                else:
                    self._toggle_record()
            return
        if ccnum == CC_KEYS_DRUMS:
            self._toggle_drums()
        elif self._drums_shown():
            if ccnum == CC_UP:
                self._shift_drums(KEYS_DRUM_BLOCK)
            elif ccnum == CC_DOWN:
                self._shift_drums(-KEYS_DRUM_BLOCK)
        elif ccnum == CC_UP:
            self._shift(self._octave_step())
        elif ccnum == CC_DOWN:
            self._shift(-self._octave_step())
        elif ccnum == CC_RIGHT:
            self._shift(1)
        elif ccnum == CC_LEFT:
            self._shift(-1)

    # ----- over the Pattern Editor: playing and recording --------------------

    def _undo(self, redo=False):
        """Undo or redo what was recorded on the pads, through the pattern
        editor's own snapshot stack."""
        if not _undo_pattern(redo):
            return
        # The pattern changed under the pads: read the echo again, since the
        # step may have lost or gained notes.
        with self._lock:
            self._update_echo(force=True)
            self._render_grid()

    def _seq_playing(self):
        try:
            return self._libseq.getPlayState(self._zynseq.scene, *self._seq) != zynseq.SEQ_STOPPED
        except Exception:
            return False

    def _is_recording(self):
        """True if notes played go into the edited pattern."""
        if self._seq is None:
            return False
        try:
            return bool(self._libseq.isMidiRecord())
        except Exception:
            return False

    def _play_position(self):
        return self._libseq.getSequencePlayPosition(self._zynseq.scene, *self._seq)

    def _pattern_clocks(self):
        """The edited pattern's length in steps and its clocks per step, or
        None if there is no pattern."""
        try:
            steps = self._libseq.getSteps()
            per_step = self._libseq.getClocksPerStep(self._libseq.getPatternIndex())
        except Exception:
            return None
        if steps <= 0 or per_step <= 0:
            return None
        return steps, per_step

    def _toggle_play(self):
        try:
            self._libseq.togglePlayState(self._zynseq.scene, *self._seq)
        except Exception as ex:
            logging.error(f"Launchpad could not play the pattern: {ex}")

    def _toggle_record(self):
        """Press the editor's own record button. It is only sent while the
        editor is on screen: anywhere else the same action records audio.
        The button's light follows libseq, through the poll."""
        if _shown_pattern_editor() is not None:
            self._state_manager.send_cuia("TOGGLE_RECORD")

    def _record_note_on(self, pad, note, velocity):
        if not self._is_recording():
            return
        if self._seq_playing():
            try:
                self._takes[pad] = (note, velocity, self._play_position())
            except Exception as ex:
                logging.error(f"Launchpad could not read the play position: {ex}")
        else:
            self._step_entry(note, velocity)

    def _record_note_off(self, pad):
        """A note recorded while playing goes in at the nearest step, as
        long as it was held, rounded to whole steps (at least one), and
        never past the end of the pattern."""
        take = self._takes.pop(pad, None)
        if take is None:
            return
        clocks = self._pattern_clocks()
        if clocks is None:
            return
        note, velocity, start_clock = take
        steps, per_step = clocks
        start = int(start_clock / per_step + 0.5) % steps
        duration = 1
        if self._seq_playing():
            try:
                # The pattern loops, so the release can come before the press.
                held = (self._play_position() - start_clock) % (steps * per_step)
                duration = max(1, int(held / per_step + 0.5))
            except Exception:
                pass
        duration = min(duration, steps - start)
        _write_note(self._libseq, start, note, velocity, float(duration))
        _flag_pattern_changed()

    def _step_entry(self, note, velocity):
        """Enter a note at the playhead while the pattern is stopped.

        This does what libseq does with a MIDI keyboard's notes, so both
        keyboards enter notes the same way, at the playhead the editor shows:
        - A note adds a one-step note and moves on a step. If the note is
          already there, it is removed instead, and the step still moves on.
        - While sustain (button 4) is held, notes don't move on, so they
          build a chord, and a note played again grows a step longer.
          Lifting sustain moves on.
        """
        clocks = self._pattern_clocks()
        if clocks is None:
            return
        steps = clocks[0]
        try:
            step = self._libseq.getPatternPlayhead() % steps
            duration = int(self._libseq.getNoteDuration(step, note))
            if self._sustain_chan is not None:
                _write_note(self._libseq, step, note, velocity,
                            float(min(duration + 1, steps - step)))
            else:
                if duration:
                    self._libseq.removeNote(step, note)
                else:
                    _write_note(self._libseq, step, note, velocity, 1.0)
                self._advance_step()
        except Exception as ex:
            logging.error(f"Launchpad could not enter a note: {ex}")
            return
        _flag_pattern_changed()

    def _advance_step(self):
        """Move the stopped playhead on a step, wrapping at the end."""
        clocks = self._pattern_clocks()
        if clocks is None:
            return
        steps, per_step = clocks
        try:
            step = (self._libseq.getPatternPlayhead() + 1) % steps
            self._libseq.setSequencePlayPosition(
                self._zynseq.scene, *self._seq, step * per_step)
        except Exception as ex:
            logging.error(f"Launchpad could not move the playhead: {ex}")

    def _read_editor_state(self):
        if self._seq is None:
            self._playing = self._recording = False
            return
        self._playing = self._seq_playing()
        self._recording = self._is_recording()

    def _poll_editor(self):
        """Called from the poll thread while the keyboard is over the editor."""
        with self._lock:
            if not self._is_active or self._seq is None:
                return
            try:
                seq = (self._zynseq.phrase, self._zynseq.chan)
            except Exception:
                seq = self._seq
            if seq != self._seq:
                # The editor moved to another sequence, maybe on another
                # chain: stop what the old one was playing.
                self.release_all()
                self._seq = seq
                self._poll_count = 0
            if self._poll_count == 0:
                self._read_editor_state()
                # The LED cache only sends what changed.
                self.refresh()
            elif self._update_echo():
                self._render_grid()
            self._poll_count = (self._poll_count + 1) % KEYS_SYNC_EVERY

    def _update_echo(self, force=False):
        """Read which of the grid's notes the edited pattern is playing.
        They are read again when the playhead moves on a step, or if force
        is set. Returns True if they changed."""
        step = None
        if self._seq is not None and self._seq_playing():
            try:
                step = int(self._libseq.getPatternPlayhead())
                if not 0 <= step < self._libseq.getSteps():
                    step = None
            except Exception:
                step = None
        if step == self._echo_step and not force:
            return False
        self._echo_step = step
        echo = set()
        if step is not None:
            shown = {self._pad_note(col, prow) for prow in range(8) for col in range(8)}
            shown.discard(None)
            try:
                # getNoteStart finds a note covering the step, so a long
                # note stays lit for as long as it sounds.
                echo = {note for note in shown if self._libseq.getNoteStart(step, note) != -1}
            except Exception:
                echo = set()
        echo = frozenset(echo)
        if echo == self._echo:
            return False
        self._echo = echo
        return True

    # ----- painting ----------------------------------------------------------

    def refresh(self, force=False):
        with self._lock:
            if not self._is_active:
                return
            if force:
                self._leds.forget()
            self._load_drums()
            self._load_layout()
            # The layout may have moved, or the pattern been edited.
            self._update_echo(force=True)
            self._render_grid()
            self._render_controls()

    def _root_colour(self):
        try:
            return zynthian_gui_config.LAUNCHER_COLOUR[self._chain_chan()]["launchpad"]
        except Exception:
            return COLORS.KEY_ROOT

    def _pad_colour(self, note, sounding, root_colour):
        if note is None:
            return COLORS.OFF
        if note in sounding:
            return COLORS.KEY_PRESSED
        if note in self._echo:
            return COLORS.KEY_ECHO
        if (note - self._tonic) % 12 == 0:
            return root_colour
        if self._intervals is None and note % 12 in KEYS_BLACK_KEYS:
            return COLORS.KEY_BLACK
        return COLORS.KEY

    def _drum_colour(self, entry, sounding, chain_colour):
        if entry is None:
            return COLORS.OFF
        if entry["note"] in sounding:
            return COLORS.DRUM_PRESSED
        if entry["note"] in self._echo:
            return COLORS.KEY_ECHO
        colour = self._drum_family_colour(entry["name"])
        return chain_colour if colour is None else colour

    def _render_grid(self):
        if not self._is_active:
            return
        # A note can be on more than one pad; all of them light while it sounds.
        sounding = {note for _, note in self._held.values()}
        root_colour = self._root_colour()
        drums = self._drums_shown()
        for prow in range(8):
            for col in range(8):
                pad = 10 * (prow + 1) + col + 1
                if drums:
                    colour = self._drum_colour(self._drum_entry(col, prow), sounding, root_colour)
                else:
                    colour = self._pad_colour(self._pad_note(col, prow), sounding, root_colour)
                if colour == COLORS.OFF:
                    self._leds.pad_off(pad)
                else:
                    self._leds.pad_on(pad, colour)

    def _render_controls(self):
        if not self._is_active:
            return
        if self._drums_shown():
            lit = {
                CC_UP: self._drum_offset < self._max_drum_offset(),
                CC_DOWN: self._drum_offset > 0,
                CC_RIGHT: False,
                CC_LEFT: False,
            }
        else:
            octave, step = self._octave_step(), 1
            lit = {
                CC_UP: self._can_shift(octave),
                CC_DOWN: self._can_shift(-octave),
                CC_RIGHT: self._can_shift(step),
                CC_LEFT: self._can_shift(-step),
            }
        for ccnum, on in lit.items():
            if on:
                self._leds.cc_on(ccnum, COLORS.ARROW)
            else:
                self._leds.cc_off(ccnum)
        for ccnum in SS_RIGHT_COL_CCS:
            if ccnum == CC_KEYS_PLAY and self._seq is not None:
                self._leds.cc_on(ccnum, COLORS.TRANSPORT_ACTIVE if self._playing
                                 else COLORS.TRANSPORT, 2 if self._playing else 0)
            elif ccnum == CC_KEYS_RECORD and self._seq is not None:
                self._leds.cc_on(ccnum, COLORS.RECORD_ON if self._recording
                                 else COLORS.RECORD, 2 if self._recording else 0)
            elif ccnum == CC_UNDO and self._seq is not None:
                self._leds.cc_on(ccnum, COLORS.UNDO)
            elif ccnum == CC_SUSTAIN:
                self._leds.cc_on(ccnum, COLORS.SUSTAIN_ON if self._sustain_chan is not None
                                 else COLORS.SUSTAIN)
            elif ccnum == CC_KEYS_DRUMS and self._drums:
                self._leds.cc_on(ccnum, COLORS.KEYS_DRUMS_ON if self._drums_shown()
                                 else COLORS.KEYS_DRUMS_OFF)
            else:
                self._leds.cc_off(ccnum)
        self._leds.cc_off(self._driver.cc_back)


# ------------------------------------------------------------------------------------------------------------------
class zynthian_ctrldev_launchpad_base(zynthian_ctrldev_zynpad):
    """Base driver for the Novation Launchpad family.

    A model's driver sets dev_ids, driver_name and the device profile
    below. The defaults are the Launchpad X's, except the SysEx device ID,
    which every model must set.
    """

    dev_ids = []
    driver_description = "Launcher + piano roll step sequencer"

    # ----- device profile -------------------------------------------------
    # Byte 6 of the model's SysEx header (F0 00 20 29 02 <id> ...).
    sysex_device_id = None
    # Layout selection SysEx payloads.
    sysex_layout_session = "00 00"
    # The layout left on the device when the driver ends.
    sysex_layout_standalone = "00 05"
    # The device's own layout buttons, which send CCs even in DAW mode.
    cc_session = CC_SESSION
    cc_note = CC_NOTE
    layout_button_ccs = LAYOUT_BUTTON_CCS
    # The button that sends BACK in the step sequencer.
    cc_back = CC_CAPTURE

    def __init__(self, state_manager, idev_in, idev_out=None):
        super().__init__(state_manager, idev_in, idev_out)
        self._mode = MODE_LAUNCHER
        self._stepseq = None
        self._keys = None
        # True while the grid is the keyboard rather than the launcher.
        self._note_mode = False
        # Launcher pad presses in flight, so a long press can be told from a
        # tap. The launcher path used to look at note-on only.
        self._launcher_presses = {}

    def send_sysex(self, data):
        if self.idev_out is not None and self.sysex_device_id is not None:
            msg = bytes.fromhex(f"F0 00 20 29 02 {self.sysex_device_id} {data} F7")
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
        self.send_sysex(self.sysex_layout_session)
        self._stepseq = StepSeqHandler(self.state_manager, self, self.idev)
        self._keys = KeysHandler(self.state_manager, self)
        super().init()
        zynsigman.register(zynsigman.S_GUI, zynsigman.SS_GUI_SHOW_SCREEN, self._on_gui_show_screen)

    def end(self):
        if self._stepseq is not None:
            self._stepseq.end()
        if self._keys is not None:
            self._keys.end()
        zynsigman.unregister(zynsigman.S_GUI, zynsigman.SS_GUI_SHOW_SCREEN, self._on_gui_show_screen)
        super().end()
        # Exit DAW session mode
        self.send_sysex("10 00")
        # Leave the device in a layout it can be played in on its own
        self.send_sysex(self.sysex_layout_standalone)

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
                # The step sequencer takes over the grid.
                self._exit_note_mode(refresh=False)
            self._stepseq.set_active(True)
        else:
            if self._note_mode:
                # The keyboard was over the editor, which has gone: back to
                # the launcher.
                self._exit_note_mode(refresh=False)
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
        # The keyboard can be over either mode, so it comes first.
        if self._note_mode and self._keys is not None:
            # Only the base refresh (chain list); skip the launcher repaint.
            zynthian_ctrldev_base.refresh(self)
            self._keys.refresh()
            return
        if self._mode == MODE_STEPSEQ and self._stepseq is not None:
            zynthian_ctrldev_base.refresh(self)
            self._stepseq.refresh()
            return
        super().refresh()

    def on_active_chain(self, active_chain_id):
        super().on_active_chain(active_chain_id)
        # The keyboard plays the active chain and shows its colour.
        if self._note_mode and self._keys is not None:
            self._keys.refresh()

    def update_seq_state(self, phrase, chan):
        if self._note_mode:
            # The keyboard over the editor polls the play state itself.
            return
        if self._mode == MODE_STEPSEQ and self._stepseq is not None:
            self._stepseq.update_seq_state(phrase, chan)
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
        self.send_sysex(self.sysex_layout_session)
        if self._note_mode and self._keys is not None:
            # The other layout drew its own LEDs; paint the keyboard in full.
            self._keys.refresh(force=True)
            return
        self.refresh()

    # ----- the device's own layout buttons --------------------------------

    def _handle_layout_button(self, ccnum):
        """The device's layout buttons, which double as mode switches.

        Novation's Session, Note and Custom buttons switch the device's layout
        and also send CCs, so the driver has to catch them before its own
        dispatch - otherwise a pad press in the wrong layout means something
        entirely different.

        Note means "play the instrument": the device goes back to its Session
        layout and the driver draws the keyboard, over the launcher or over
        the step sequencer. On the keyboard, Session goes back to what was
        under it; in the step sequencer, it goes back to the launcher.
        Custom is not a layout this driver paints, so it always falls back
        to Session.
        """
        if self._note_mode:
            # Only Session leaves note mode; the others are not layouts the
            # driver paints.
            if ccnum == self.cc_session:
                self._exit_note_mode()
            else:
                self._restore_session_layout()
            return True
        if self._mode == MODE_STEPSEQ:
            if ccnum == self.cc_session:
                self._restore_session_layout()
                # Back to the launcher, with the device in its Session layout.
                self.state_manager.send_cuia("SCREEN_ZYNPAD")
            elif ccnum == self.cc_note:
                self._enter_note_mode()
            else:
                self._restore_session_layout()
            return True
        if ccnum == self.cc_note:
            self._enter_note_mode()
            return True
        self._restore_session_layout()
        return True

    def _enter_note_mode(self):
        """Turn the grid into a keyboard: for the active chain over the
        launcher, for the edited sequence over the step sequencer.

        Pressing Note has already switched the device to its own Note
        layout, whose pads send on the MIDI port, where this driver doesn't
        listen. So the device goes back to Session layout, whose pads send
        on the DAW port with velocity and pressure, and the driver draws the
        keyboard there.
        """
        if self.idev_out is None or self._keys is None:
            return False
        seq = None
        if self._mode == MODE_STEPSEQ and self._stepseq is not None:
            seq = self._stepseq._selected_seq
            self._stepseq.set_active(False)
        self._note_mode = True
        self.send_sysex(self.sysex_layout_session)
        self.light_off()
        self._keys.set_active(True, seq)
        logging.info("Launchpad keyboard on")
        return True

    def _exit_note_mode(self, refresh=True):
        """Back from the keyboard. Sounding notes are stopped, and with
        refresh what was under it is shown again: the step sequencer if the
        editor is still open, the launcher otherwise."""
        self._note_mode = False
        if self._keys is not None:
            self._keys.set_active(False)
        if refresh:
            if self._mode == MODE_STEPSEQ and self._stepseq is not None:
                self.send_sysex(self.sysex_layout_session)
                self._stepseq.set_active(True)
            else:
                self._restore_session_layout()
        logging.info("Launchpad keyboard off")

    def _launcher_note_on(self, note):
        """A launcher pad was pressed. Nothing happens until it is released,
        when the length of the press says what it meant.

        The pattern is resolved now, so a scroll while the pad is held
        cannot make the release act on a different pad's pattern.
        """
        col, row = self.get_note_xy(note)
        midi_chan = None
        if 0 <= col < self.cols:
            midi_chan = self.get_filtered_midi_chan_by_index(col + self.scroll_h)
        if midi_chan is None:
            return
        self._launcher_presses[note] = (time.time(), row + self.scroll_v, midi_chan)

    def _launcher_note_off(self, note):
        """A launcher pad was released.

        A tap launches or stops the pattern. A long press opens it in the
        Pattern Editor instead, without starting it. The launcher grid
        already says which pattern is under which pad: the column is the
        chain and the row is the phrase.
        """
        pressed = self._launcher_presses.pop(note, None)
        if pressed is None:
            return False
        pressed_at, phrase, midi_chan = pressed
        if time.time() - pressed_at >= SS_LONG_PRESS_TIME:
            self._open_pattern_editor(phrase, midi_chan)
            return True
        try:
            self.zynseq.libseq.togglePlayState(self.zynseq.scene, phrase, midi_chan)
        except Exception as ex:
            logging.error(f"Launchpad could not launch a pattern: {ex}")
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
                f"Launchpad opening the pattern editor on phrase {phrase}, "
                f"channel {midi_chan}")
            return True
        except Exception as ex:
            logging.error(f"Launchpad could not open the pattern editor: {ex}")
            return False

    def _keys_midi_event(self, ev, evtype):
        """Route input to the keyboard."""
        keys = self._keys
        value1 = ev[1] & 0x7F if len(ev) > 1 else 0
        value2 = ev[2] & 0x7F if len(ev) > 2 else 0
        if evtype == 0x9 and value2 > 0:
            keys.note_on(value1, value2)
        elif evtype in (0x8, 0x9):
            keys.note_off(value1)
        elif evtype == 0xA:
            keys.pressure(value1, value2)
        elif evtype == 0xD:
            keys.channel_pressure(value1)
        elif evtype == 0xB:
            keys.cc_change(value1, value2)
        return True

    def midi_event(self, ev):
        # logging.debug(f"Launchpad MIDI handler => {ev}")
        evtype = (ev[0] >> 4) & 0x0F
        # SysEx
        if ev[0] == 0xF0:
            logging.info(f"Received SysEx => {ev.hex(' ')}")
            return True
        # Layout buttons, caught before mode dispatch so they are handled in
        # launcher mode and stepseq mode alike.
        if evtype == 0xB and (ev[2] & 0x7F) > 0 and (ev[1] & 0x7F) in self.layout_button_ccs:
            logging.debug(f"Layout button {ev[1] & 0x7F} pressed")
            return self._handle_layout_button(ev[1] & 0x7F)
        if self._note_mode and self._keys is not None:
            return self._keys_midi_event(ev, evtype)
        if self._mode == MODE_STEPSEQ and self._stepseq is not None:
            return self._stepseq_midi_event(ev, evtype)
        # Note ON / OFF => launch or stop a sequence on release, or open its
        # editor on a long press
        if evtype == 0x9:
            note = ev[1] & 0x7F
            vel = ev[2] & 0x7F
            if vel > 0:
                self._launcher_note_on(note)
            else:
                # Note OFF with no velocity: the pad was released.
                self._launcher_note_off(note)
            return True
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