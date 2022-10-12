try:
    from math import inf
except:
    inf = float('inf')
from queue import Queue
import socket
import _socket
import sys
import threading
import time
from collections import deque
from typing import Dict, List
import uuid

import json
import marshal
import pickle

TIMEOUT = socket.getdefaulttimeout()
DEFAULT_PORT = 2110
THREAD_SLEEP = 0.001
BUSY_WAITING = 0.001
DEFAULT_PASSWORD = 'password'
SERVER_START_RETRIES = 5
DEBUG_DEFAULT = False


def isrelatedclass(typ, cls):
    """Determines if typ is a subclass, superclass, or equivalent to cls
    cls can be an iterable of classes/types.
    """
    if type(cls) == type:
        cls = [cls]

    for t in cls:
        if typ == cls or issubclass(typ, cls) or issubclass(cls, typ):
            return True
    return False


class IdentifyingException(Exception):
    """An exception with an overriden string representation, telling its class name along with error message."""

    def __repr__(self):
        return f'{self.__class__.__name__}: {super(IdentifyingException, self).__repr__()}'


class UnsupportedCommand(IdentifyingException):
    """Error given for when a command was sent to a RemoteServer that cannot process it."""
    pass


class brickle:
    """A replacement for pickle, in the context of this RemoteClient/Server library. 
    Only allows the parsing of objects that subclass PasswordProtected.
    Attributes of these objects can only be primitive values. 
    The parse can be changed to utilize the slower pickle library to cover all value types."""
    _parser = marshal

    class UnpicklingError(IdentifyingException):
        pass

    def dumps(obj):
        try:
            res = {}
            if isinstance(obj, PasswordProtected):
                res = brickle._dumps(obj)
            else:
                pass
            return brickle._parser.dumps(res)
        except Exception as err:
            raise brickle.UnpicklingError(err)

    def _dumps(obj):
        res = vars(obj).copy()
        res['__class__'] = obj.__class__.__name__
        return res

    def loads(data):
        try:
            data = brickle._parser.loads(data)
            if data['__class__'] == 'Command':
                c = Command(data['func_name'])
                return brickle._loads(c, data)
            elif data['__class__'] == 'Message':
                m = Message(data['text'])
                return brickle._loads(m, data)
            else:
                return None
        except Exception as err:
            raise brickle.UnpicklingError(err)

    def _loads(obj, data):
        # Specific to this implementation
        del data['__class__']
        obj.__dict__.update(data)
        return obj


class PasswordProtected:
    def __init__(self, password=None):
        if password is None:
            password = DEFAULT_PASSWORD
        self.password = password

    def verify_password(self, test):
        return test == self.password


class MessageReplyException(IdentifyingException):
    pass


class Message(PasswordProtected):
    def __init__(self, text):
        super(Message, self).__init__()
        self.text = str(text)
        self.sender = None

    def reply(self, text):
        # Intended for Connection objects, but can apply to anything
        # that has a send method that can handle a Message obj
        if self.sender is not None and hasattr(self.sender, 'send') and callable(getattr(self.sender, 'send')):
            self.sender.send(Message(text))
        else:
            raise MessageReplyException("No sender available")

    def __repr__(self):
        return self.text


class Command(PasswordProtected):
    def __init__(self, func_name, *args, **kwargs):
        super(Command, self).__init__()
        self.func_name = func_name
        self.args = args
        self.kwargs = kwargs
        self.id = str(uuid.uuid1())
        self.result = None
        self._result_given = False
        self._result_exception = False

    def __repr__(self):
        return f"{self.id}: {self.func_name}({self.args},{self.kwargs})"


class Debuggable:
    DEBUG_ALL = {}
    DEBUG_COUNTER = 0

    def __init__(self, debug=None):
        self.debug = DEBUG_DEFAULT if debug is None else debug
        if self.debug:
            i = id(self)
            if i not in self.__class__.DEBUG_ALL:
                self.__class__.DEBUG_ALL[i] = f'{self.__class__.__name__}{self.__class__.DEBUG_COUNTER}'
                self.__class__.DEBUG_COUNTER += 1

    def _debug(self, text):
        if self.debug:
            i = self.__class__.DEBUG_ALL[id(self)]
            print(f'>>> ({i}) {text}\t', file=sys.stderr)


class ConnectionError(IdentifyingException):
    pass


class ConnectionFatalError(IdentifyingException):
    pass


class Connection:
    def __init__(self, sock, password="password", debug=None):
        self.sock: socket.socket = sock
        self.listeners = {}
        self.run_event = threading.Event()
        self.lock_listener = threading.Lock()
        self.lock_send = threading.Lock()
        self._isclosed = False

        self.password = password
        self.run_event.set()
        t = threading.Thread(target=Connection._func,
                             args=(self,), daemon=True)
        t.start()

    def _func(self):
        # self._debug('starting connection thread')
        while self.run_event.is_set():
            try:
                # self._debug('start receiving')
                try:
                    d = self.sock.recv(4096)
                except:
                    # The read failed because the connection probably died.
                    self.close()
                    break
                # self._debug('received. loading...')
                if len(d) <= 0:
                    self.run_event.clear()
                    self.close()
                    break
                o = brickle.loads(d)
                # self._debug('received. loaded...')

                self.lock_listener.acquire()

                if isinstance(o, PasswordProtected) and o.verify_password(self.password):
                    for key, val in self.listeners.items():
                        listener, args = val
                        try:
                            # self._debug(f'running listener "{key}"')
                            listener(*args, o, self)
                            # self._debug(f'completed listener "{key}"')
                        except Exception as err:
                            c = ConnectionError(
                                f"Error: Listener {key} - {err} {val}")
                            print(c, file=sys.stderr)
                self.lock_listener.release()
            except OSError as err:
                if self.isclosed():
                    return
                print('Warning:', err, file=sys.stderr)
            except brickle.UnpicklingError as err:
                print('Data Unpickling Error:', err, file=sys.stderr)
            except Exception as err:
                c = ConnectionFatalError(f'Bad Error: {err}')
                print(c, file=sys.stderr)
        # self._debug(f'connection thread ended')

    def send(self, obj):
        if isinstance(obj, PasswordProtected):
            self.lock_send.acquire()
            obj.password = self.password
            # self._debug(f'dumping data ({str(obj)})')
            d = brickle.dumps(obj)
            # self._debug(f'sending data dump ({str(obj)})')
            self.sock.send(d)
            # self._debug(f'data sent ({str(obj)})')
            self.lock_send.release()

    def register_listener(self, name, listener, args=None):
        """Expects a listener of function type:
        def func(*args, obj, connection)

        where obj is the unpickled object, at the time
        args are the arguments of the args tuple
        and connection refers to this Connection object
        """
        if args is None:
            args = tuple()

        self.lock_listener.acquire()
        self.listeners[name] = (listener, args)
        self.lock_listener.release()

    def __del__(self):
        self.close()

    def close(self):
        try:
            self.run_event.clear()
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        except:
            pass

        self._isclosed = True

    def isclosed(self):
        return self._isclosed


class MethodCallerException(IdentifyingException):
    pass


class _MethodCaller:
    def __init__(self, obj, custom=None, var_name=''):
        if custom is None:
            custom = []

        self.cls = obj.__class__
        self.obj = obj
        self.var_name = var_name

        self.methods = {f'{self.var_name}.{func_name}': getattr(self.cls, func_name) for func_name in dir(
            self.cls) if func_name in custom or (callable(getattr(self.cls, func_name)) and not func_name.startswith("__"))}

    def supports_command(self, command: Command):
        return command.func_name in self.methods

    def execute(self, command: Command):
        if command.func_name in self.methods:
            try:
                command.result = self.methods[command.func_name](self.obj,
                                                                 *command.args, **command.kwargs)
            except Exception as err:
                command.result = str(MethodCallerException(err))
        return command


class MessageReceiver(object):  # Somewhat abstract class that needs self.conn to be set
    def __init__(self):
        self.messages = deque()
        self.lock_messages = threading.Lock()

    def wait_messages(self, timeout=None, wait_interval=None):
        if timeout is None:
            timeout = inf
        if wait_interval is None:
            wait_interval = BUSY_WAITING

        while timeout > 0 and not self.has_messages():
            timeout -= 1
            time.sleep(wait_interval)
        return True

    def has_messages(self):
        return self.num_messages() > 0

    def num_messages(self):
        self.lock_messages.acquire()
        r = len(self.messages)
        self.lock_messages.release()
        return r

    def get_messages(self, count=0):
        """Gets the specified number of messages from the message buffer.
        Thread-safe.
        """
        # Receiving messages in the listener thread for this RemoteBrick socket
        self.lock_messages.acquire()

        result = []
        if count <= 0:
            result = list(self.messages)
            self.messages.clear()
        elif count > 0:
            count = min(len(self.messages), count)
            for i in range(count):
                result.append(self.messages.popleft())

        self.lock_messages.release()
        return result

    def get_message(self, wait=False):
        if wait:
            self.wait_messages()
        m = self._get_message()
        return m

    def _get_message(self):
        """Gets the one message from the message buffer, or None if none present.
        Thread-safe.
        """
        self.lock_messages.acquire()
        try:
            m = self.messages.popleft()
        except:
            m = None
        self.lock_messages.release()
        return m


class _RemoteCaller:
    TESTING = False

    def create_caller(obj, remote_client, custom=None, var_name=''):
        caller = _RemoteCaller(remote_client, var_name)
        
        if custom is None:
            custom = []

        for name in dir(obj):
            attr = getattr(obj, name)
            if name in custom or (callable(attr) and not name.startswith('__')):
                setattr(obj, name, caller._generate(name))

        obj.__remote__ = caller
        return obj

    def __init__(self, remote_client, var_name):
        self.remote_client = remote_client
        self.var_name = var_name

    def _generate(self, func_name):
        func_name = f'{self.var_name}.{func_name}'
        def func(*args, wait_for_data=60, **kwargs):
            res = self.remote_client._send_command(
                func_name, *args, wait_for_data=wait_for_data, **kwargs)
            if _RemoteCaller.TESTING:
                return res
            else:
                if hasattr(res, 'result'):
                    return res.result
                else:
                    return res
        return func


class RemoteException(Exception):
    """Exception for when an exception occurs on a RemoteServer and it is sent back to the RemoteClient"""
    pass


class RemoteClient(MessageReceiver):
    TESTING = False

    def __init__(self, address, password, port=None, sock=None):
        super(RemoteClient, self).__init__()

        self.address = socket.gethostbyname(address)
        self.password = DEFAULT_PASSWORD if password is None else password
        self.port = DEFAULT_PORT if port is None else port

        self.buffer = {}
        self.lock_buffer = threading.Lock()

        self.status = None

        if sock is None:
            self.sock = socket.create_connection((self.address, self.port))
        else:
            self.sock = sock

        self.conn = Connection(self.sock, self.password)

        self.conn.register_listener('main', RemoteClient._listener, (self,))

    def create_caller(self, obj, custom=None, var_name=''):
        return _RemoteCaller.create_caller(obj, self, custom=custom, var_name=var_name)

    def send_message(self, text):
        self.conn.send(Message(text))

    def __del__(self):
        self.close()

    def close(self):
        try:
            self.conn.close()
        except:
            pass

    def _listener(self, obj, conn):
        if isinstance(obj, Message):
            self.lock_messages.acquire()
            obj.sender = conn
            self.messages.append(obj)
            self.lock_messages.release()
        elif isinstance(obj, Command):
            self.lock_buffer.acquire()
            self.buffer[obj.id] = obj
            self.lock_buffer.release()
        else:
            pass

    def _send_command(self, func, *args, wait_for_data=True, **kwargs):
        """Send a command object to the other brick.
        Thread-safe.
        """
        c = Command(func, * args, **kwargs)
        self.conn.send(c)
        if wait_for_data:
            res = self._get_result(c.id, wait_for_data)
            if res._result_exception and not RemoteClient.TESTING:
                raise RemoteException(res.result)
        else:
            res = c.id

        return res

    def _get_result(self, cid, wait_for_data=True) -> Command:
        """Get the result of the following command id.
        Thread-safe.
        """
        waiting = not not wait_for_data
        if not isinstance(wait_for_data, (int, float)):
            wait_for_data = inf

        start = time.perf_counter()
        end = time.perf_counter()
        while waiting and wait_for_data > (end-start):
            self.lock_buffer.acquire()
            if cid in self.buffer:
                self.lock_buffer.release()
                break
            self.lock_buffer.release()

            time.sleep(BUSY_WAITING)
            end = time.perf_counter()

        self.lock_buffer.acquire()
        o = self.buffer.get(cid, None)
        if o is not None:
            del self.buffer[cid]
        self.lock_buffer.release()
        return o


class RemoteServer(MessageReceiver):
    def __init__(self, password, port=None):
        super(RemoteServer, self).__init__()
        self.password = (DEFAULT_PASSWORD if password is None else password)
        self.port = (DEFAULT_PORT if port is None else port)

        self._callers: List[_MethodCaller] = []
        self._caller_methods: Dict[str, _MethodCaller] = {}

        self._isclosed = False
        self.connections: List[RemoteClient] = []
        self.commands = []
        self.lock_commands = threading.Lock()
        self.lock_connections = threading.Lock()
        self.run_event = threading.Event()
        self.run_event.set()

        self.sock = None
        self.t1 = threading.Thread(target=self._thread_server, daemon=True)
        self.t1.start()

    def _thread_server(self):
        # True or False flag for server restartability on Linux.
        # Must be False on systems that don't support it e.g. Windows
        reuse_port = (hasattr(_socket, "SO_REUSEPORT"))

        while self.run_event.is_set():
            with socket.create_server(('0.0.0.0', self.port), reuse_port=reuse_port) as server:
                self.sock = server
                while self.run_event.is_set():
                    try:
                        conn, addr = self.sock.accept()  # blocking, don't need time sleep
                    except OSError:
                        self.run_event.clear()
                        break

                    self.lock_connections.acquire()
                    self.connections = list(
                        filter(lambda s: not s.isclosed(), self.connections))

                    connection = Connection(conn, self.password)
                    connection.register_listener(
                        'main', self._thread_listener)
                    self.connections.append(connection)
                    self.lock_connections.release()
                self.close_connections()
            self.close()

    def _thread_listener(self, obj, conn):
        if isinstance(obj, Command):
            self.lock_commands.acquire()
            self._execute(conn, obj)
            self.lock_commands.release()
        if isinstance(obj, Message):
            self.lock_messages.acquire()
            obj.sender = conn
            self.messages.append(obj)
            self.lock_messages.release()

    def register_object(self, obj, custom=None, var_name=''):
        caller = _MethodCaller(obj, custom=custom, var_name=var_name)
        for method in caller.methods:
            self._caller_methods[method] = caller
        self._callers.append(caller)

    def _caller_retrieve_command(self, command: Command) -> _MethodCaller:
        return self._caller_methods.get(command.func_name, None)

    def _caller_supports_command(self, command: Command):
        return command is not None and command.func_name in self._caller_methods.keys()

    def _caller_execute(self, command: Command):
        return self._caller_methods[command.func_name].execute(command)

    def _execute(self, conn: Connection, command: Command):
        """Executes a command and sends the result back to the remote brick (rem)"""
        command._result_given = True

        try:
            if (caller := self._caller_retrieve_command(command)) is not None:
                caller.execute(command)
                conn.send(command)
                return
            elif command.func_name == '__initialize':
                return
            elif command.func_name == '__verify':
                command.result = (
                    f"I am sending back the command for {command.id}")
                conn.send(command)
                return
            else:
                command.result = str(UnsupportedCommand(
                    f"Command '{command.func_name}' is not supported."))
        except Exception as err:
            command.result = str(f'{err.__class__.__name__}: {err}')

        command._result_exception = True
        conn.send(command)

    def __del__(self):
        self.close()

    def broadcast_message(self, text):
        m = Message(text)
        self.lock_connections.acquire()
        for conn in self.connections:
            try:
                conn.send_message(m)
            except:
                pass
        self.lock_connections.release()

    def close_connections(self):
        self.lock_connections.acquire()
        c = self.connections
        self.connections = []
        self.lock_connections.release()

        for conn in c:
            try:
                conn.close()
                del conn
            except:
                pass
        c.clear()

    def close(self):
        self._isclosed = True
        self.run_event.clear()
        self.close_connections()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
        except:
            pass

    def isclosed(self):
        return self._isclosed