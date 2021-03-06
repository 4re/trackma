# This file is part of Trackma.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import pyinotify

import os
import re
import time

from trackma.tracker import tracker
from trackma import utils

class pyinotifyTracker(tracker.TrackerBase):
    name = 'Tracker (pyinotify)'

    open_file = (None, None, None)

    def __init__(self, messenger, tracker_list, process_name, watch_dir, interval, update_wait, update_close, not_found_prompt):
        super().__init__(messenger, tracker_list, process_name, watch_dir, interval, update_wait, update_close, not_found_prompt)

        self.re_players = re.compile(self.process_name.encode('utf-8'))

    def _is_being_played(self, filename):
        """
        This function makes sure that the filename is being played
        by the player specified in players.

        It uses procfs so if we're using inotify that means we're using Linux
        thus we should be safe.
        """

        for p in os.listdir("/proc/"):
            if not p.isdigit(): continue
            d = "/proc/%s/fd/" % p
            try:
                for fd in os.listdir(d):
                    f = os.readlink(d+fd)
                    if f == filename:
                        # Get process name
                        with open('/proc/%s/cmdline' % p, 'rb') as f:
                            cmdline = f.read()
                            pname = cmdline.partition(b'\x00')[0]
                        self.msg.debug(self.name, 'Playing process: {} {} ({})'.format(p, pname, cmdline))

                        # Check if it's our process
                        if self.re_players.search(pname):
                            return p, fd
                        else:
                            self.msg.debug(self.name, "Not read by player ({})".format(pname))
            except OSError:
                pass

        self.msg.debug(self.name, "Couldn't find playing process.")
        return None, None

    def _closed_handle(self, pid, fd):
        """ Check if this pid has closed this handle (or never opened it) """
        d = "/proc/%s/fd/%s" % (pid, fd)
        return not os.path.islink(d)

    def _proc_open(self, path, name):
        self.msg.debug(self.name, 'Got OPEN event: {} {}'.format(path, name))
        pathname = os.path.join(path, name)

        if self.open_file[0]:
            self.msg.debug(self.name, "There's already a tracked open file.")
            return

        pid, fd = self._is_being_played(pathname)

        if pid:
            self._emit_signal('detected', path, name)
            self.open_file = (pathname, pid, fd)

            (state, show_tuple) = self._get_playing_show(name)
            self.msg.debug(self.name, "Got status: {} {}".format(state, show_tuple))
            self.update_show_if_needed(state, show_tuple)
        else:
            self.msg.debug(self.name, "Not played by player, ignoring.")

    def _proc_close(self, path, name):
        self.msg.debug(self.name, 'Got CLOSE event: {} {}'.format(path, name))
        pathname = os.path.join(path, name)

        open_pathname, pid, fd = self.open_file
        time.sleep(0.1) # TODO : If we don't wait the filehandle will still be there

        if pathname != open_pathname:
            self.msg.debug(self.name, "A different file was closed.")
            return

        if not self._closed_handle(pid, fd):
            self.msg.debug(self.name, "Our pid hasn't closed the file.")
            return

        self._emit_signal('detected', path, name)
        self.open_file = (None, None, None)

        (state, show_tuple) = self._get_playing_show(None)
        self.update_show_if_needed(state, show_tuple)

    def observe(self, watch_dir, interval):
        self.msg.info(self.name, 'Using pyinotify.')
        wm = pyinotify.WatchManager()  # Watch Manager
        mask = (pyinotify.IN_OPEN
                | pyinotify.IN_CLOSE_NOWRITE
                | pyinotify.IN_CLOSE_WRITE
                | pyinotify.IN_CREATE
                | pyinotify.IN_MOVED_FROM
                | pyinotify.IN_MOVED_TO
                | pyinotify.IN_DELETE)

        class EventHandler(pyinotify.ProcessEvent):
            def my_init(self, parent=None):
                self.parent = parent

            def process_IN_OPEN(self, event):
                if not event.mask & pyinotify.IN_ISDIR:
                    self.parent._proc_open(event.path, event.name)

            def process_IN_CLOSE_NOWRITE(self, event):
                if not event.mask & pyinotify.IN_ISDIR:
                    self.parent._proc_close(event.path, event.name)

            def process_IN_CLOSE_WRITE(self, event):
                if not event.mask & pyinotify.IN_ISDIR:
                    self.parent._proc_close(event.path, event.name)

            def process_IN_CREATE(self, event):
                if not event.mask & pyinotify.IN_ISDIR:
                    self.parent._emit_signal('detected', event.path, event.name)

            def process_IN_MOVED_TO(self, event):
                if not event.mask & pyinotify.IN_ISDIR:
                    self.parent._emit_signal('detected', event.path, event.name)

            def process_IN_MOVED_FROM(self, event):
                if not event.mask & pyinotify.IN_ISDIR:
                    self.parent._emit_signal('removed', event.path, event.name)

            def process_IN_DELETE(self, event):
                if not event.mask & pyinotify.IN_ISDIR:
                    self.parent._emit_signal('removed', event.path, event.name)

        handler = EventHandler(parent=self)
        notifier = pyinotify.Notifier(wm, handler)
        self.msg.debug(self.name, 'Watching directory {}'.format(watch_dir))
        wdd = wm.add_watch(watch_dir, mask, rec=True, auto_add=True)

        try:
            #notifier.loop()
            timeout = None
            while self.active:
                if notifier.check_events(timeout):
                    notifier.read_events()
                    notifier.process_events()
                    if self.last_state == utils.TRACKER_NOVIDEO or self.last_updated:
                        timeout = None  # Block indefinitely
                    else:
                        timeout = 1000  # Check each second for counting
                else:
                    self.msg.debug(self.name, "Sending last state {} {}".format(self.last_state, self.last_show_tuple))
                    self.update_show_if_needed(self.last_state, self.last_show_tuple)
        finally:
            notifier.stop()
            self.msg.info(self.name, 'Tracker has stopped.')

