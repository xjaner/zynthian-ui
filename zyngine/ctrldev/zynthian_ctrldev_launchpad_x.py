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

"""Novation Launchpad X driver: launcher, piano-roll step sequencer and
keyboard.

All the behaviour lives in the shared Launchpad base,
zynthian_ctrldev_base_launchpad, whose docstring describes the controls and
their colours. This file is only the Launchpad X's device profile.

Only the DAW port is registered - "Launchpad X IN 1" here, and the
matching OUT 1 - because the device's own Note layout sends on the MIDI port
(IN 2), which goes straight to the chains and must not reach this driver.
The driver therefore never sees a pad in the device's own Note layout, and
puts the device into DAW session mode (SysEx 10 01) on start, so the pads
send on the DAW port with velocity and polyphonic pressure.

Profile, all of it the base's defaults because the base was written for this
model and measured on it:

    sysex_device_id       0C
                          Byte 6 of F0 00 20 29 02 <id> ..., the same in
                          every Launchpad X message. 0D is the Mini MK3.
    session layout        00 00, and 00 05 (the standalone layout) on exit,
                          so the device is playable on its own afterwards.
    layout buttons        95 Session, 96 Note, 97 Custom, 98 Capture MIDI.
                          Sent over CC even in DAW mode. 95 and 96 drive the
                          modes, and 98 is BACK in the step sequencer, where
                          the arrow cluster is needed for paging.
    right column          89, 79, 69, 59, 49, 39, 29, 19 from the top, and
                          the bottom row of pads is notes 11-18.

The palette is the one measured on this model by sweeping all 128 velocity
values across the grid: see COLORS in the base, which the note, step
sequencer and keyboard colours are drawn from. A model with a different
palette would need those constants reviewed, not just the profile above.

Verified on hardware: the launcher, the step sequencer (editing,
recording, undo, the playhead echo), the keyboard and drum pads, the
echo of the edited pattern on the keyboard, and leaving the device in a
playable layout on exit.
"""

from zyngine.ctrldev.zynthian_ctrldev_base_launchpad import zynthian_ctrldev_launchpad_base

# ------------------------------------------------------------------------------------------------------------------
# Novation Launchpad X
# ------------------------------------------------------------------------------------------------------------------


class zynthian_ctrldev_launchpad_x(zynthian_ctrldev_launchpad_base):

    dev_ids = ["Launchpad X IN 1"]
    driver_name = "Launchpad X"
    driver_description = "Launcher + piano roll step sequencer"

    # The top row (Session 95, Note 96, Custom 97, Capture MIDI 98) and the
    # layout SysEx are the base's defaults, which were measured on this model.
    sysex_device_id = "0C"

# ------------------------------------------------------------------------------
