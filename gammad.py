#!/usr/bin/env python2
#
# Detector controller for gamma measurements
# Copyright (C) 2016  Norwegain Radiation Protection Authority
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Authors: Dag Robole,

from __future__ import print_function

import os, sys, json, threading, importlib

from twisted.internet import reactor, threads, defer, task
from twisted.internet.protocol import DatagramProtocol
from twisted.python import log

import gc_gps as gps
import gc_database as database
from gc_exceptions import ProtocolError

log.startLogging(sys.stdout)

class SessionState: Ready, Busy = range(2)

class SpectrumState: Ready, Busy = range(2)

class DetectorState: Cold, Warm = range(2)

class Controller(DatagramProtocol):

    def __init__(self):

        self.client_address = None

        self.detector_state = DetectorState.Cold
        self.detector_data = None

        self.session_args = None
        self.session_loop = None
        self.session_state = SessionState.Ready

        self.spectrum_state = SpectrumState.Ready
        self.spectrum_index = 0
        self.spectrum_failures = 0

        self.database_connection = None

        self.gps_stop = threading.Event() # Event used to notify gps thread
        self.gps = gps.GpsThread(self.gps_stop)

        self.plugin = None

    def sendResponse(self, msg):

        if self.client_address is not None:
            log.msg("Send response: %s" % msg['command'])
            self.transport.write(bytes(json.dumps(msg)), self.client_address)
        else:
            log.msg("Send response failed: Client address invalid")

    def sendResponseWithCommand(self, command, msg):

        msg['command'] = command
        self.sendResponse(msg)

    def sendResponseWithInfo(self, command, info):

        msg = {'command':"%s" % command, 'message':"%s" % info}
        self.sendResponse(msg)

    def loadPlugin(self, name):

        if self.plugin != None:
            self.plugin.finalizePlugin()
        modname = 'plugin_' + name
        return sys.modules[modname] if modname in sys.modules else importlib.import_module(modname)

    def startProtocol(self):

        log.msg('Starting GPS thread')
        self.gps.start()

    def stopProtocol(self):

        log.msg('Stopping GPS thread')
        if self.plugin != None:
            self.plugin.finalizePlugin()
        self.gps_stop.set()
        self.gps.join()

    def datagramReceived(self, data, addr):

        self.client_address = addr

        try:
            msg = json.loads(data.decode("utf-8"))

            log.msg("Received %s from %s" % (msg, self.client_address)) # FIXME

            if not 'command' in msg:
                raise ProtocolError('error', "Message has no command");

            cmd = msg['command']

            if cmd == 'detector_config':
                if self.session_state == SessionState.Busy:
                    raise ProtocolError('detector_config_busy', "Detector config failed, session is active")

                self.detector_data = msg['detector_data']

                if not 'plugin_name' in self.detector_data:
                    raise ProtocolError('detector_config_error', "Detector config failed, plugin_name missing")

                self.plugin = self.loadPlugin(self.detector_data['plugin_name'])
                self.plugin.initializePlugin()
                self.plugin.initializeDetector(self.detector_data)
                self.detector_state = DetectorState.Warm
                self.sendResponseWithCommand('detector_config_success', self.detector_data)

            elif cmd == 'start_session':
                if self.session_state == SessionState.Busy:
                    raise ProtocolError('start_session_busy', "Start session failed, session is active")

                self.initializeSession(msg)
                self.startSession(msg)
                self.sendResponseWithCommand('start_session_success', msg)

            elif cmd == 'stop_session':
                if self.session_state == SessionState.Ready:
                    raise ProtocolError('stop_session_noexist', "Stop session failed, no session active")
                if self.session_args['session_name'] != msg['session_name']:
                    raise ProtocolError('stop_session_wrongname', "Stop session failed, wrong session name")

                self.stopSession(msg)
                self.finalizeSession(msg)
                self.sendResponseWithCommand('stop_session_success', msg)

            elif cmd == 'dump_session':
                if self.session_state == SessionState.Ready:
                    raise ProtocolError('dump_session_none', "Dump session failed, no session active")

                msg["message"] = "dumping session to " + str(self.client_address)
                self.sendResponseWithCommand('dump_session_success', msg)

            elif cmd == 'get_status':
                stat = os.statvfs('/') # FIXME: python2 only
                response = {
                    'free_disk_space': stat.f_bsize * stat.f_bavail,
                    'session_running': True if self.session_state == SessionState.Busy else False,
                    'spectrum_index': 0 if self.session_state == SessionState.Ready else self.spectrum_index,
                    'detector_configured': True if self.detector_state == DetectorState.Warm else False
                }
                self.sendResponseWithCommand('get_status_success', response)

            elif cmd == 'sync_session':
                specs = database.getSyncSpectrums(msg['session_name'], list(msg['indices_list']), int(msg['last_index']))
                for s in specs:
                    spec = {
                        'command': 'spectrum',
                        'session_name': s[2],
                        'index': s[3],
                        'time': s[4],
                        'latitude': s[5],
                        'latitude_error': s[6],
                        'longitude': s[7],
                        'longitude_error': s[8],
                        'altitude': s[9],
                        'altitude_error': s[10],
                        'track': s[11],
                        'track_error': s[12],
                        'speed': s[13],
                        'speed_error': s[14],
                        'climb': s[15],
                        'climb_error': s[16],
                        'livetime': s[17],
                        'realtime': s[18],
                        'total_count': s[19],
                        'num_channels': s[20],
                        'channels': s[21]
                    }
                    self.sendResponse(spec)

            else: raise Exception("Unknown command: %s" % cmd)

        except ProtocolError as pe:
            log.msg("ProtocolError: %s" % (str(pe)))
            self.sendResponseWithInfo(pe.command, pe.message)

        except ImportError as ie:
            log.msg("ImportError: %s" % (str(ie)))
            self.sendResponseWithInfo('error', "Unable to import module")

        except Exception as e:
            log.msg("Exception: %s" % (str(e)))
            self.sendResponseWithInfo('error', str(e))

    def initializeSession(self, msg):

        log.msg("Initializing session " + msg['session_name'])
        self.session_args = msg
        self.spectrum_index = 0
        self.spectrum_failures = 0
        self.database_connection = database.create(self.detector_data, msg)
        self.plugin.initializeSession(msg)

    def finalizeSession(self, msg):

        log.msg("Finalizing session")
        self.plugin.finalizeSession(msg)
        database.close(self.database_connection)
        self.database_connection = None

    def startSession(self, msg):

        log.msg("Starting session " + msg['session_name'])
        self.session_loop = task.LoopingCall(self.sessionTick)
        self.session_loop.start(0.05)
        self.session_state = SessionState.Busy

    def stopSession(self, msg):

        log.msg("Stopping session")
        self.session_loop.stop()
        self.session_state = SessionState.Ready

    def sessionTick(self):

        if self.spectrum_state == SpectrumState.Ready:
            d = threads.deferToThread(self.aquireSpectrum)
            d.addCallbacks(self.handleSpectrumSuccess, self.handleSpectrumFailure)
            self.spectrum_state = SpectrumState.Busy

    def aquireSpectrum(self):

        position = self.gps.position
        velocity = self.gps.velocity
        time = self.gps.time

        msg = self.plugin.acquireSpectrum(self.session_args)

        msg.update(position)
        msg.update(velocity)
        msg['time'] = time

        return msg

    def handleSpectrumSuccess(self, msg):

        msg['index'] = self.spectrum_index
        self.spectrum_index += 1
        database.insertSpectrum(self.database_connection, msg)
        self.sendResponse(msg)
        self.spectrum_state = SpectrumState.Ready

    def handleSpectrumFailure(self, err):

        self.sendResponseWithInfo('error', err.getErrorMessage())

        self.spectrum_failures += 1
        if self.spectrum_failures >= 3:
            self.stopSession(self.session_args)
            self.finalizeSession(self.session_args)
            self.sendResponseWithInfo('error', "Acquiring spectrum has failed 3 times, stopping session")

        self.spectrum_state = SpectrumState.Ready

if __name__ == "__main__":
    reactor.listenUDP(9999, Controller())
    reactor.run()
