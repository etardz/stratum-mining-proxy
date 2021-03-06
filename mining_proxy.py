#!/usr/bin/env python
'''
    Stratum mining proxy
    Copyright (C) 2012 Marek Palatinus <slush@satoshilabs.com>
    
    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

import argparse
import os
import socket

def parse_args():
    parser = argparse.ArgumentParser(description='This proxy allows you to run miners against Stratum mining pool.')
    parser.add_argument('-o', '--host', dest='host', type=str, default='stratum.bitcoin.cz', help='Hostname of Stratum mining pool')
    parser.add_argument('-p', '--port', dest='port', type=int, default=3333, help='Port of Stratum mining pool')
    parser.add_argument('-sh', '--stratum-host', dest='stratum_host', type=str, default='0.0.0.0', help='On which network interface listen for stratum miners. Use "localhost" for listening on internal IP only.')
    parser.add_argument('-sp', '--stratum-port', dest='stratum_port', type=int, default=3333, help='Port on which port listen for stratum miners.')
    parser.add_argument('-cs', '--custom-stratum', dest='custom_stratum', type=str, help='Override URL provided in X-Stratum header')
    parser.add_argument('-cu', '--custom-user', dest='custom_user', type=str, help='Use this username for submitting shares')
    parser.add_argument('-cp', '--custom-password', dest='custom_password', type=str, help='Use this password for submitting shares')
    parser.add_argument('--blocknotify', dest='blocknotify_cmd', type=str, default='', help='Execute command when the best block changes (%%s in BLOCKNOTIFY_CMD is replaced by block hash)')
    parser.add_argument('--socks', dest='proxy', type=str, default='', help='Use socks5 proxy for upstream Stratum connection, specify as host:port')
    parser.add_argument('--tor', dest='tor', action='store_true', help='Configure proxy to mine over Tor (requires Tor running on local machine)')
    parser.add_argument('-v', '--verbose', dest='verbose', action='store_true', help='Enable low-level debugging messages')
    parser.add_argument('-q', '--quiet', dest='quiet', action='store_true', help='Make output more quiet')
    parser.add_argument('-i', '--pid-file', dest='pid_file', type=str, help='Store process pid to the file')
    parser.add_argument('-l', '--log-file', dest='log_file', type=str, help='Log to specified file')
    return parser.parse_args()

from stratum import settings
settings.LOGLEVEL='INFO'

if __name__ == '__main__':
    # We need to parse args & setup Stratum environment
    # before any other imports
    args = parse_args()
    if args.quiet:
        settings.DEBUG = False
        settings.LOGLEVEL = 'WARNING'
    elif args.verbose:
        settings.DEBUG = True
        settings.LOGLEVEL = 'DEBUG'
    if args.log_file:
        settings.LOGFILE = args.log_file
            
from twisted.internet import reactor, defer
from stratum.socket_transport import SocketTransportFactory, SocketTransportClientFactory
from stratum.services import ServiceEventHandler
from twisted.web.server import Site

from mining_libs import stratum_listener
from mining_libs import client_service
from mining_libs import jobs
from mining_libs import worker_registry
# from mining_libs import multicast_responder
from mining_libs import version
from mining_libs import utils

import stratum.logger
log = stratum.logger.get_logger('proxy')

def on_shutdown(f):
    '''Clean environment properly'''
    log.info("Shutting down proxy...")
    f.is_reconnecting = False # Don't let stratum factory to reconnect again
    
@defer.inlineCallbacks
def on_connect(f, workers, job_registry):
    '''Callback when proxy get connected to the pool'''
    log.info("Connected to Stratum pool at %s:%d" % f.main_host)
    
    # Hook to on_connect again
    f.on_connect.addCallback(on_connect, workers, job_registry)
    
    # Every worker have to re-autorize
    workers.clear_authorizations() 
       
    # Subscribe for receiving jobs
    log.info("Subscribing for mining jobs")
    (_, extranonce1, extranonce2_size) = (yield f.rpc('mining.subscribe', []))[:3]
    job_registry.set_extranonce(extranonce1, extranonce2_size)
    stratum_listener.StratumProxyService._set_extranonce(extranonce1, extranonce2_size)
    
    if args.custom_user:
        log.warning("Authorizing custom user %s, password %s" % (args.custom_user, args.custom_password))
        workers.authorize(args.custom_user, args.custom_password)

    defer.returnValue(f)
     
def on_disconnect(f, workers, job_registry):
    '''Callback when proxy get disconnected from the pool'''
    log.info("Disconnected from Stratum pool at %s:%d" % f.main_host)
    f.on_disconnect.addCallback(on_disconnect, workers, job_registry)
    
    stratum_listener.MiningSubscription.disconnect_all()
    
    # Reject miners because we don't give a *job :-)
    workers.clear_authorizations() 
    
    return f              

def print_deprecation_warning():
    '''Once new version is detected, this method prints deprecation warning every 30 seconds.'''

    log.warning("New proxy version available! Please update!")
    reactor.callLater(30, print_deprecation_warning)

def test_update():
    '''Perform lookup for newer proxy version, on startup and then once a day.
    When new version is found, it starts printing warning message and turned off next checks.'''
 
    GIT_URL='https://raw.github.com/slush0/stratum-mining-proxy/master/mining_libs/version.py'

    import urllib2
    log.warning("Checking for updates...")
    try:
        if version.VERSION not in urllib2.urlopen(GIT_URL).read():
            print_deprecation_warning()
            return # New version already detected, stop periodic checks
    except:
        log.warning("Check failed.")

    reactor.callLater(3600 * 24, test_update)

@defer.inlineCallbacks
def main(args):
    if args.pid_file:
        fp = file(args.pid_file, 'w')
        fp.write(str(os.getpid()))
        fp.close()

    log.warning("Stratum proxy version: %s" % version.VERSION)
    # Setup periodic checks for a new version
    # Disabled due to WIP on another branch
    # test_update()
    
    if args.tor:
        log.warning("Configuring Tor connection")
        args.proxy = '127.0.0.1:9050'
        args.host = 'pool57wkuu5yuhzb.onion'
        args.port = 3333
        
    if args.proxy:
        proxy = args.proxy.split(':')
        if len(proxy) < 2:
            proxy = (proxy, 9050)
        else:
            proxy = (proxy[0], int(proxy[1]))
        log.warning("Using proxy %s:%d" % proxy)
    else:
        proxy = None

    log.warning("Trying to connect to Stratum pool at %s:%d" % (args.host, args.port))        
        
    # Connect to Stratum pool
    f = SocketTransportClientFactory(args.host, args.port,
                debug=args.verbose, proxy=proxy,
                event_handler=client_service.ClientMiningService)
    
    
    job_registry = jobs.JobRegistry(f, cmd=args.blocknotify_cmd)
    client_service.ClientMiningService.job_registry = job_registry
    client_service.ClientMiningService.reset_timeout()
    
    workers = worker_registry.WorkerRegistry(f)
    f.on_connect.addCallback(on_connect, workers, job_registry)
    f.on_disconnect.addCallback(on_disconnect, workers, job_registry)

    # Cleanup properly on shutdown
    reactor.addSystemEventTrigger('before', 'shutdown', on_shutdown, f)

    # Block until proxy connect to the pool
    yield f.on_connect

    # Setup stratum listener
    if args.stratum_port > 0:
        stratum_listener.StratumProxyService._set_upstream_factory(f)
        stratum_listener.StratumProxyService._set_custom_user(args.custom_user, args.custom_password)
        reactor.listenTCP(args.stratum_port, SocketTransportFactory(debug=False, event_handler=ServiceEventHandler), interface=args.stratum_host)

    # Setup multicast responder
    # reactor.listenMulticast(3333, multicast_responder.MulticastResponder((args.host, args.port), args.stratum_port, args.getwork_port), listenMultiple=True)
    
    log.warning("-----------------------------------------------------------------------")
    if args.stratum_host == '0.0.0.0':
        log.warning("PROXY IS LISTENING ON ALL IPs ON PORT %d (stratum)" % args.stratum_port)
    else:
        log.warning("LISTENING FOR MINERS ON stratum+tcp://%s:%d (stratum)" % (args.stratum_host, args.stratum_port))
    log.warning("-----------------------------------------------------------------------")

if __name__ == '__main__':
    main(args)
    reactor.run()
