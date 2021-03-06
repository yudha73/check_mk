#!/usr/bin/python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2012             mk@mathias-kettner.de |
# +------------------------------------------------------------------+
#
# This file is part of Check_MK.
# The official homepage is at http://mathias-kettner.de/check_mk.
#
# check_mk is free software;  you can redistribute it and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the Free Software Foundation in version 2.  check_mk is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# ails.  You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.

import config
import lib

treestates = {}
treestates_for_id = None

def load_tree_states():
    global treestates
    global treestates_for_id
    if html.id is not treestates_for_id:
        treestates = config.load_user_file("treestates", {})
        treestates_for_id = html.id

def save_tree_states():
    config.save_user_file("treestates", treestates)

def get_tree_states(tree):
    load_tree_states()
    return treestates.get(tree, {})

def set_tree_state(tree, key, val):
    load_tree_states()

    if tree not in treestates:
        treestates[tree] = {}

    treestates[tree][key] = val

def set_tree_states(tree, val):
    load_tree_states()
    treestates[tree] = val

def ajax_tree_openclose():
    load_tree_states()

    tree = html.var("tree")
    name = html.var("name")

    if not tree or not name:
        MKUserError('tree or name parameter missing')

    set_tree_state(tree, name, html.var("state"))
    save_tree_states()
