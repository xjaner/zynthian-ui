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

"""Novation Launchpad X driver.

All the behaviour - launcher, step sequencer, note mode - lives in the
shared Launchpad base, zynthian_ctrldev_base_launchpad, whose docstring
describes the controls. This file is the Launchpad X's device profile.
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
