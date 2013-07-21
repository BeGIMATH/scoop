#
#    This file is part of Scalable COncurrent Operations in Python (SCOOP).
#
#    SCOOP is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as
#    published by the Free Software Foundation, either version 3 of
#    the License, or (at your option) any later version.
#
#    SCOOP is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#    GNU Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public
#    License along with SCOOP. If not, see <http://www.gnu.org/licenses/>.
#
import time
import sys
import random
import socket
try:
    import cPickle as pickle
except ImportError:
    import pickle

import zmq

import scoop
from . import shared, encapsulation
from .shared import SharedElementEncapsulation


class ReferenceBroken(Exception):
    """An object could not be unpickled (dereferenced) on a worker"""
    pass


class Shutdown(Exception):
    pass


def CreateZMQSocket(sock_type):
    """Create a socket of the given sock_type and deactivate message dropping"""
    sock = ZMQCommunicator.context.socket(sock_type)
    sock.setsockopt(zmq.LINGER, 1000)
    if zmq.zmq_version_info() >= (3, 0, 0):
        sock.setsockopt(zmq.SNDHWM, 0)
        sock.setsockopt(zmq.RCVHWM, 0)
    return sock


class ZMQCommunicator(object):
    """This class encapsulates the communication features toward the broker."""
    context = zmq.Context()

    def __init__(self):
        # TODO number of broker
        self.number_of_broker = float('inf')
        self.broker_set = set()

        # Get the current address of the interface facing the broker
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((scoop.BROKER.hostname, scoop.BROKER.task_port))
        external_addr = s.getsockname()[0]
        s.close()

        # Create an inter-worker socket
        self.direct_socket_peers = []
        self.direct_socket = CreateZMQSocket(zmq.ROUTER)
        # TODO: This doesn't seems to be respected in the ROUTER socket
        self.direct_socket.setsockopt(zmq.SNDTIMEO, 0)
        # Code stolen from pyzmq's bind_to_random_port() from sugar/socket.py
        for i in range(100):
            try:
                self.direct_socket_port = random.randrange(49152, 65536)
                # Set current worker inter-worker socket name to its addr:port
                scoop.worker = "{addr}:{port}".format(
                    addr=external_addr,
                    port=self.direct_socket_port,
                ).encode()
                self.direct_socket.setsockopt(zmq.IDENTITY, scoop.worker)
                self.direct_socket.bind("tcp://*:{0}".format(
                    self.direct_socket_port,
                ))
            except:
                # Except on ZMQError with a check on EADDRINUSE should go here
                # but its definition is not consistent in pyzmq over multiple
                # versions
                pass
            else:
                break

        # socket for the futures, replies and request
        self.socket = CreateZMQSocket(zmq.DEALER)
        self.socket.setsockopt(zmq.IDENTITY, scoop.worker)

        # socket for the shutdown signal
        self.infoSocket = CreateZMQSocket(zmq.SUB)
        
        # Set poller
        self.task_poller = zmq.Poller()
        self.task_poller.register(self.socket, zmq.POLLIN)
        self.task_poller.register(self.direct_socket, zmq.POLLIN)

        self._addBroker(scoop.BROKER)

        # Send an INIT to get all previously set variables and share
        # current configuration to broker
        self.socket.send_multipart([
            b"INIT",
            pickle.dumps(scoop.CONFIGURATION)
        ])
        scoop.CONFIGURATION = pickle.loads(self.socket.recv())
        inboundVariables = pickle.loads(self.socket.recv())
        shared.elements = dict([
            (pickle.loads(key),
                dict([(pickle.loads(varName),
                       pickle.loads(varValue))
                    for varName, varValue in value.items()
                ]))
                for key, value in inboundVariables.items()
        ])
        for broker in pickle.loads(self.socket.recv()):
            # Skip already connected brokers
            if broker in self.broker_set:
                continue
            self._addBroker(broker)

        self.OPEN = True

    def addPeer(self, peer):
        if peer not in self.direct_socket_peers:
            self.direct_socket_peers.append(peer)
            new_peer = "tcp://{0}".format(peer.decode("utf-8"))
            self.direct_socket.connect(new_peer)
            # Wait for zmq socket stabilize
            # TODO: Find another (asynchronous) way to know when it's stable
            time.sleep(0.05)

    def _addBroker(self, brokerEntry):
        # Add a broker to the socket and the infosocket.
        broker_address = "tcp://{hostname}:{port}".format(
            hostname=brokerEntry.hostname,
            port=brokerEntry.task_port,
        )
        meta_address = "tcp://{hostname}:{port}".format(
            hostname=brokerEntry.hostname,
            port=brokerEntry.info_port,
        )
        self.socket.connect(broker_address)

        self.infoSocket.connect(meta_address)
        self.infoSocket.setsockopt(zmq.SUBSCRIBE, b"")

        self.broker_set.add(brokerEntry)


    def _poll(self, timeout):
        self.pumpInfoSocket()
        return self.task_poller.poll(timeout)

    def _recv(self):
        # Prioritize answers over new tasks
        if self.direct_socket.poll(0):
            msg = self.direct_socket.recv_multipart()
            # Remove the sender address
            msg = msg[1:]
        else:
            msg = self.socket.recv_multipart()
        
        # Handle group (reduction) replies
        if msg[1] == b"GROUP":
            data = pickle.loads(msg[2])
            scoop.reduction.answers[data[0]][msg[0]] = (data[1], data[2])
            return

        try:
            thisFuture = pickle.loads(msg[1])
        except AttributeError as e:
            scoop.logger.error(
                "An instance could not find its base reference on a worker. "
                "Ensure that your objects have their definition available in "
                "the root scope of your program.\n{error}".format(
                    error=e
                )
            )
            raise ReferenceBroken(e)

        # Try to connect directly to this worker to send the result afterwards
        if msg[0] == b"TASK":
            self.addPeer(thisFuture.id.worker)
            
        isCallable = callable(thisFuture.callable)
        isDone = thisFuture._ended()
        if not isCallable and not isDone:
            # TODO: Also check in root module globals for fully qualified name
            try:
                module_found = hasattr(sys.modules["__main__"],
                                       thisFuture.callable)
            except TypeError:
                module_found = False
            if module_found:
                thisFuture.callable = getattr(sys.modules["__main__"],
                                              thisFuture.callable)
            else:
                raise ReferenceBroken("This element could not be pickled: "
                                      "{0}.".format(thisFuture))
        return thisFuture

    def pumpInfoSocket(self):
        while self.infoSocket.poll(0):
            msg = self.infoSocket.recv_multipart()
            if msg[0] == b"SHUTDOWN":
                if scoop.IS_ORIGIN is False:
                    raise Shutdown("Shutdown received")
                if not scoop.SHUTDOWN_REQUESTED:
                    scoop.logger.error(
                        "A worker exited unexpectedly. Read the worker logs "
                        "for more information. SCOOP pool will now shutdown."
                    )
                    raise Shutdown("Unexpected shutdown received")
            elif msg[0] == b"VARIABLE":
                key = pickle.loads(msg[3])
                varValue = pickle.loads(msg[2])
                varName = pickle.loads(msg[1])
                shared.elements.setdefault(key, {}).update({varName: varValue})
                self.convertVariable(key, varName, varValue)
            elif msg[0] == b"TASKEND":
                source_addr = pickle.loads(msg[1])
                if source_addr and source_addr != scoop.worker:
                    # If results are asked
                    self.sendGroupedResult(msg[1], msg[2])
                scoop.reduction.cleanGroupID(pickle.loads(msg[2]))
            elif msg[0] == b"BROKER_INFO":
                # TODO: find out what to do here ...
                if len(self.broker_set) == 0: # The first update
                    self.broker_set.add(pickle.loads(msg[1]))
                if len(self.broker_set) < self.number_of_broker:
                    brokers = pickle.loads(msg[2])
                    needed = self.number_of_broker - len(self.broker_set)
                    try:
                        new_brokers = random.sample(brokers, needed)
                    except ValueError:
                        new_brokers = brokers
                        self.number_of_broker = len(self.broker_set) + len(new_brokers)
                        scoop.logger.warning(("The number of brokers could not be set"
                                        " on worker {0}. A total of {1} worker(s)"
                                        " were set.".format(scoop.worker,
                                                            self.number_of_broker)))

                    for broker in new_brokers:
                        broker_address = "tcp://" + broker.hostname + broker.task_port
                        meta_address = "tcp://" + broker.hostname + broker.info_port
                        self._addBroker(broker_address, meta_address)
                    self.broker_set.update(new_brokers)

    def convertVariable(self, key, varName, varValue):
        """Puts the function in the globals() of the main module."""
        if isinstance(varValue, encapsulation.FunctionEncapsulation):
            result = varValue.getFunction()

            # Update the global scope of the function to match the current module
            # TODO: Rework this not to be dependent on runpy / bootstrap call 
            # stack
            # TODO: Builtins doesn't work
            mainModule = sys.modules["__main__"]
            result.__name__ = varName
            result.__globals__.update(mainModule.__dict__)
            setattr(mainModule, varName, result)
            shared.elements[key].update({
                varName: result,
            })

    def recvFuture(self):
        while self._poll(0):
            received = self._recv()
            if received:
                yield received

    def sendFuture(self, future):
        try:
            if shared.getConst(hash(future.callable),
                               timeout=0):
                # Enforce name reference passing if already shared
                future.callable = SharedElementEncapsulation(hash(future.callable))
            self.socket.send_multipart([b"TASK",
                                        pickle.dumps(future,
                                                     pickle.HIGHEST_PROTOCOL)])
        except pickle.PicklingError as e:
            # If element not picklable, pickle its name
            # TODO: use its fully qualified name
            scoop.logger.warn("Pickling Error: {0}".format(e))
            previousCallable = future.callable
            future.callable = hash(future.callable)
            self.socket.send_multipart([b"TASK",
                                        pickle.dumps(future,
                                                     pickle.HIGHEST_PROTOCOL)])
            future.callable = previousCallable

    def sendResult(self, future):
        # Remove (now) extraneous elements from future class
        future.callable = future.args = future.greenlet =  None
        
        if not future.sendResultBack:
            # Don't reply back the result if it isn't asked
            future.resultValue = None

        self._sendReply(
            future.id.worker,
            pickle.dumps(
                future,
                pickle.HIGHEST_PROTOCOL,
            ),
        )

    def sendGroupedResult(self, destination, group_id):
        self._sendReply(
            destination,
            b"GROUP",
            pickle.dumps([
                group_id,
                int(scoop.reduction.sequence[group_id]),
                scoop.reduction.total[group_id],
            ], pickle.HIGHEST_PROTOCOL),
        )

    def _sendReply(self, destination, *args):
        """Send a REPLY directly to its destination. If it doesn't work, launch
        it back to the broker."""
        # Try to send the result directly to its parent
        self.addPeer(destination)

        self.direct_socket.send_multipart([
            destination,
            b"REPLY",
        ] + list(args))

        # TODO: Fallback on Broker routing if no direct connection possible
        #self.socket.send_multipart([
        #    b"REPLY",
        #    pickle.dumps(future,
        #                 pickle.HIGHEST_PROTOCOL),
        #    future.id.worker,
        #])

    def sendVariable(self, key, value):
        self.socket.send_multipart([b"VARIABLE",
                                    pickle.dumps(key),
                                    pickle.dumps(value,
                                                 pickle.HIGHEST_PROTOCOL),
                                    pickle.dumps(scoop.worker,
                                                 pickle.HIGHEST_PROTOCOL)])

    def taskEnd(self, groupID, askResults=False):
        self.socket.send_multipart([
            b"TASKEND",
            pickle.dumps(
                askResults,
                pickle.HIGHEST_PROTOCOL
            ),
            pickle.dumps(
                groupID,
                pickle.HIGHEST_PROTOCOL
            ),
        ])

    def sendRequest(self):
        for _ in range(len(self.broker_set)):
            self.socket.send(b"REQUEST")

    def workerDown(self):
        self.socket.send(b"WORKERDOWN")

    def shutdown(self):
        """Sends a shutdown message to other workers."""
        if self.OPEN:
            self.OPEN = False
            scoop.SHUTDOWN_REQUESTED = True
            self.socket.send(b"SHUTDOWN")
            self.socket.close()
            self.infoSocket.close()
            time.sleep(0.3)
