#   Copyright (C) 2016 Canonical Ltd.
#
#   Author: Wesley Wiedenmeier <wesley.wiedenmeier@canonical.com>
#
#   Curtin is free software: you can redistribute it and/or modify it under
#   the terms of the GNU Affero General Public License as published by the
#   Free Software Foundation, either version 3 of the License, or (at your
#   option) any later version.
#
#   Curtin is distributed in the hope that it will be useful, but WITHOUT ANY
#   WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
#   FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for
#   more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with Curtin.  If not, see <http://www.gnu.org/licenses/>.

# This module provides a mechanism for shutting down virtual storage layers on
# top of a block device, making it possible to reuse the block device without
# having to reboot the system

from curtin import (block, util)
from curtin.log import LOG

import errno
import os


def split_vg_lv_name(full_name):
    """
    Break full logical volume device name into volume group and logical volume
    """
    # FIXME: when block.lvm is written this should be moved there
    return (None, None)


def shutdown_bcache(device):
    pass


def shutdown_mdadm(device):
    pass


def shutdown_lvm(device):
    """
    Shutdown specified lvm device. Device must be given as a path in /sys/block
    or /sys/virtual/block/.

    Will not raise io errors, but will collect and return them

    May return a partial function that should be run by the caller on the
    underlying block device
    """
    # lvm devices have a dm directory that containes a file 'name' containing
    # '{volume group}-{logical volume}'. The volume can be freed using lvremove

    catcher = util.ForgiveIoError()
    with catcher:
        name_file = os.path.join(device, 'dm', 'name')
        full_name = util.load_file(name_file)
        (vg_name, lv_name) = split_vg_lv_name(full_name)
        if vg_name is None or lv_name is None:
            raise OSError(errno.ENOENT,
                          'file: {} missing or has invalid contents'.format(
                             name_file))

    # use two --force flags here in case the volume group that this lv is
    # attached two has been damaged by a disk being wiped or other storage
    # volumes being shut down.

    # We do not need to and should not remove the volume group here, as it
    # should be possible for a user to destroy some logical volumes in a
    # volume group, but set 'preserve: true' on the volgroup itself and
    # the data in other logical volumes on the volgroup

    # if something has already destroyed the logical volume, such as another
    # partition being forcibly removed from the volume group, then lvremove
    # will return 5.

    # If this happens, then we should not halt installation, it
    # is most likely not an issue. However, we will record the error and pass
    # it up the clear_holders stack so that if other clear_holders calls fail
    # and this is a potential cause it will be written to install log
    cmd = ["lvremove", "--force", "--force", "{}/{}".format(vg_name, lv_name)]
    try:
        util.subp(cmd)
    except util.ProcessExecutionError as e:
        catcher.caught.append(e)
        if not (hasattr(e, 'exit_code') and e.exit_code == 5):
            raise

    # The underlying volume can be freed of its lvm metadata using
    # block.wipe_volume with wipe mode 'pvremove'
    return (None, catcher.caught)


def get_holders(device):
    """
    Look up any block device holders.
    Can handle devices and partitions as devnames (vdb, md0, vdb7)
    Can also handle devices and partitions by path in /sys/
    Will not raise io errors, but will collect and return them
    """
    holders = []
    catcher = util.ForgiveIoError()

    # get sysfs path if missing
    # block.sys_block_path works when given a /sys or /dev path
    with catcher:
        sysfs_path = block.sys_block_path(device)

    # block.sys_block_path may have failed
    if not sysfs_path:
        LOG.debug('get_holders: did not find sysfs path for %s', device)
        return (holders, catcher.caught)

    # get holders
    with catcher:
        holders = os.listdir(os.path.join(sysfs_path, 'holders'))

    LOG.debug("devname '%s' had holders: %s", device, ','.join(holders))
    return (holders, catcher.caught)


def clear_holders(device):
    """
    Shutdown all storage layers holding specified device.
    Device can be specified either with a path in /dev or /sys/block

    Will supress all io errors encountered while removing holders, as there may
    be situations in which shutting down one holding device may remove another,
    causing it to disappear. Once all handlers have been run, check if all
    holders have been shut down.

    Returns True is all holders could be shut down sucessfully, False
    otherwise. Also returns a list of all IOErrors encountered and ignored
    while running.
    """
    catcher = util.ForgiveIoError()

    # block.sys_block_path works when given a /sys path as well
    with catcher:
        device = block.sys_block_path(device)

    (holders, _err) = get_holders(device)
    catcher.caught.extend(_err)
    LOG.info("clear_holders running on '%s', with holders '%s'" %
             (device, holders))

    # if there were no holders or the holders dir could not be accessed, skip
    if not holders:
        return (True, catcher.caught)

    # go through all found holders, get their real path, detect what type they
    # are, and shut them down
    for holder in holders:
        # get realpath, skip holder if cannot get it, as holder may be gone
        # already
        holder_realpath = None
        with catcher:
            holder_realpath = os.path.realpath(os.path.join(
                device, "holders", holder))
        if not holder_realpath:
            continue

        # run clear holders on all found holders, if it fails, give up
        (res, _err) = clear_holders(holder_realpath)
        catcher.caught.extend(_err)
        if not res:
            return (False, catcher.caught)

    # detect holder type, if holder returns any functions to be called for disk
    # wiping, run them, and log the output
    wipe_cmds = []
    holder_types = {
        'bcache': shutdown_bcache,
        'md': shutdown_mdadm,
        'dm': shutdown_lvm,
    }
    for (type_name, handler) in holder_types.items():
        if os.path.exists(os.path.join(device, type_name)):
            (wipe_cmd, _err) = handler(device)
            catcher.caught.extend(_err)
            if wipe_cmd is not None:
                wipe_cmds.append(wipe_cmd)

    # if any wipe commands were generated by clear handlers, run them on block
    # device.
    for wipe_cmd in wipe_cmds:
        pass

    # only return true if there are no remaining holders
    (holders, _err) = get_holders(device)
    catcher.caught.extend(_err)
    return ((len(holders) == 0 and len(_err) == 0), catcher.caught)


def check_clear(device):
    """
    Run clear_holders on device.

    If clear_holders fails, dump all exceptions caught by clear_holders to log
    and raise an OSError
    """
    (res, _err) = clear_holders(device)
    for e in _err:
        LOG.warn('clear_holders encountered error: {}'.format(e))
    if not res:
        raise OSError('could not clear holders for device: {}'.format(e))
