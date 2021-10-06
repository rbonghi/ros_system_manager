# -*- coding: UTF-8 -*-
# Copyright (C) 2021, Raffaello Bonghi <raffaello@rnext.it>
# All rights reserved
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright 
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its 
#    contributors may be used to endorse or promote products derived 
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND 
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, 
# BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS 
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, 
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; 
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, 
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE 
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, 
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import queue
import sys
import os
import stat
from grp import getgrnam
# Logging
import logging
from multiprocessing import Process, Queue, Event, Value
from multiprocessing.managers import SyncManager
from .common import get_key
from .exceptions import SystemManagerException
# Create logger for tegrastats
logger = logging.getLogger(__name__)
# Pipe configuration
# https://refspecs.linuxfoundation.org/FHS_3.0/fhs/ch05s13.html
# https://en.wikipedia.org/wiki/Filesystem_Hierarchy_Standard
ROBOT_PIPE = '/run/ros_system_manager.sock'
ROBOT_USER = 'system_manager'
# Gain timeout lost connection
TIMEOUT_GAIN = 3
TIMEOUT_SWITCHOFF = 3.0


class SystemManager(SyncManager):
    
    def __init__(self, authkey=None):
        if authkey is None:
            authkey = get_key().encode("utf-8")
        super(SystemManager, self).__init__(address=(ROBOT_PIPE), authkey=authkey)

    def get_queue(self):
        pass

    def sync_data(self):
        pass

    def sync_event(self):
        pass


class SystemManagerServer(Process):
    def __init__(self, force=False):
        self.force = force
        # Check if running a root
        if os.getuid() != 0:
            raise SystemManagerException("ros_system_manager service need sudo to work")
        # Error queue
        self._error = Queue()
        # Command queue
        self.q = Queue()
        # Speed interval
        self.interval = Value('d', -1.0)
        # Dictionary to sync
        self.data = {}
        # Event lock
        self.event = Event()
        # Load super Thread constructor
        super(SystemManagerServer, self).__init__()
        # Register stats
        # https://docs.python.org/2/library/multiprocessing.html#using-a-remote-manager
        SystemManager.register('get_queue', callable=lambda: self.q)
        SystemManager.register("sync_data", callable=lambda: self.data)
        SystemManager.register('sync_event', callable=lambda: self.event)
        # Generate key and open broadcaster
        self.broadcaster = SystemManager()

    def system_message(self, message):
        print(f"message: {message}")

    def run(self):
        # Initialize variables
        timeout = None
        interval = 1
        try:
            while True:
                try:
                    # Decode control message
                    control = self.q.get(timeout=timeout)
                    # Check if control is not empty
                    if not control:
                        continue
                    # Decode system message
                    if 'system' in control:
                        self.system_message(control['system'])
                    # Update timeout interval
                    timeout = TIMEOUT_GAIN if interval <= TIMEOUT_GAIN else interval * TIMEOUT_GAIN
                except queue.Empty:
                    self.sync_event.clear()
                    # Disable timeout
                    timeout = None
                    self.interval.value = -1.0
        except (KeyboardInterrupt, SystemExit):
            pass
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error("Error subprocess {error}".format(error=e), exc_info=1)
            # Write error messag
            self._error.put(sys.exc_info())
        finally:
            pass

    def start(self):
        # Initialize socket
        try:
            gid = getgrnam(ROBOT_USER).gr_gid
        except KeyError:
            # User does not exist
            raise SystemManagerException("Group {jtop_user} does not exist!".format(jtop_user=ROBOT_USER))
        # Remove old pipes if exists
        if os.path.exists(ROBOT_PIPE):
            if self.force:
                logger.info("Remove pipe {pipe}".format(pipe=ROBOT_PIPE))
                os.remove(ROBOT_PIPE)
            else:
                raise SystemManagerException("Service already active! Please check before run it again")
        # Start broadcaster
        try:
            self.broadcaster.start()
        except EOFError:
            raise SystemManagerException("Server already alive")
        # Initialize synchronized data and conditional
        self.sync_data = self.broadcaster.sync_data()
        self.sync_event = self.broadcaster.sync_event()
        # Change owner
        os.chown(ROBOT_PIPE, os.getuid(), gid)
        # Change mode cotroller and stats
        # https://www.tutorialspoint.com/python/os_chmod.htm
        # Equivalent permission 660 srw-rw----
        os.chmod(ROBOT_PIPE, stat.S_IREAD | stat.S_IWRITE | stat.S_IWGRP | stat.S_IRGRP)
        # Run the Control server
        super(SystemManagerServer, self).start()

    def loop_for_ever(self):
        try:
            self.start()
        except SystemManagerException as e:
            logger.error(e)
            return
        # Join main subprocess
        try:
            self.join()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            # Close communication
            self.close()

    def close(self):
        self.q.close()
        self.broadcaster.shutdown()
        # If process is alive wait to quit
        # logger.debug("Status subprocess {status}".format(status=self.is_alive()))
        while self.is_alive():
            # If process is in timeout manually terminate
            if self.interval.value == -1.0:
                logger.info("Terminate subprocess")
                self.terminate()
            logger.info("Wait shutdown subprocess")
            self.join(timeout=TIMEOUT_SWITCHOFF)
            self.interval.value = -1.0
        # Close tegrastats
        try:
            error = self._error.get(timeout=0.5)
            # Raise error if exist
            if error:
                ex_type, ex_value, tb_str = error
                ex_value.__traceback__ = tb_str
                raise ex_value
        except queue.Empty:
            pass
        self.remove_files()
        # Close stats server
        logger.info("Service closed")
        return True

    def remove_files(self):
        # If exist remove pipe
        if os.path.exists(ROBOT_PIPE):
            logger.info("Remove pipe {pipe}".format(pipe=ROBOT_PIPE))
            os.remove(ROBOT_PIPE)
# EOF