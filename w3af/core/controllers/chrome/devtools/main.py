"""
devtools.py

Copyright 2018 Andres Riancho

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
import os
import sys
import json
import errno
import socket
import logging
import threading
import functools

from collections import deque
from PyChromeDevTools import GenericElement, ChromeInterface, TIMEOUT
from websocket import WebSocketTimeoutException, WebSocketConnectionClosedException

import w3af.core.controllers.output_manager as om

from w3af.core.controllers.chrome.tests.helpers import debugging_is_configured_in_output_manager
from w3af.core.controllers.chrome.devtools.event_handlers import (proxy_connection_failed_handler,
                                                                  net_errors_handler,
                                                                  generic_error_handler,
                                                                  inspector_crash_handler)
from w3af.core.controllers.chrome.devtools.console_message import ConsoleMessage
from w3af.core.controllers.chrome.devtools.command_result import CommandResult
from w3af.core.controllers.chrome.devtools.exceptions import ChromeInterfaceException
from w3af.core.controllers.chrome.devtools.js_dialogs import dialog_handler
from w3af.core.controllers.chrome.utils.multi_json_doc import parse_multi_json_docs

#
# Disable all the annoying logging from the urllib3 and requests libraries
#
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


DEFAULT_DIALOG_HANDLER = dialog_handler


class DebugChromeInterface(ChromeInterface, threading.Thread):
    """
    The devtools protocol is documented at

        https://chromedevtools.github.io/devtools-protocol/

    Everything you can do with Chrome via this class is documented there,
    and since this class is very generic, it shouldn't require any changes
    to be able to consume all features.
    """
    message_counter = 0

    DEBUG = os.environ.get('DEVTOOLS_DEBUG', '0') == '1'

    def __init__(self,
                 host='localhost',
                 port=9222,
                 tab=0,
                 timeout=TIMEOUT,
                 auto_connect=True,
                 debugging_id=None,
                 dialog_handler=DEFAULT_DIALOG_HANDLER):

        threading.Thread.__init__(self)
        ChromeInterface.__init__(self,
                                 host=host,
                                 port=port,
                                 tab=tab,
                                 timeout=timeout,
                                 auto_connect=auto_connect)

        self.debugging_id = debugging_id
        self.dialog_handler = dialog_handler
        self.console = deque(maxlen=500)

        self._handlers = []
        self.set_default_event_handlers()

        self.exc_type = None
        self.exc_value = None
        self.exc_traceback = None

    def set_default_event_handlers(self):
        self.set_event_handler(proxy_connection_failed_handler)
        self.set_event_handler(net_errors_handler)
        self.set_event_handler(inspector_crash_handler)
        self.set_event_handler(generic_error_handler)
        self.set_event_handler(self.console_api_handler)
        self.set_event_handler(self.javascript_dialog_handler)

    def set_event_handler(self, handler):
        """
        Sets an event handler function to be called when any Chrome event
        is received.

        The event handler function receives a parsed message. This message is
        usually inspected to make sure the received event is the expected one
        and then an action is taken.

        :param handler: The function to run when the event is received
        :return: None
        """
        self._handlers.append(handler)

    def unset_event_handler(self, handler):
        try:
            self._handlers.remove(handler)
        except ValueError:
            # Do not raise any exceptions when the handler is not found,
            # different threads might remove the same handler
            pass

    def console_api_handler(self, message):
        if 'method' not in message:
            return

        if message['method'] != 'Runtime.consoleAPICalled':
            return

        params = message['params']
        _type = params['type']
        args = params['args']
        self.console.append(ConsoleMessage(_type, args))

    def javascript_dialog_handler(self, message):
        if 'method' not in message:
            return

        if message['method'] != 'Page.javascriptDialogOpening':
            return

        # Handle alert, prompt, etc.
        params = message['params']
        message = params['message']
        _type = params['type']

        # Get the action to take (accept or cancel) and the message to
        # type in the prompt() - if any - from the dialog handler and
        # send it to Chrome
        dismiss, response_message = self.dialog_handler(_type, message)
        self.Page.handleJavaScriptDialog(accept=dismiss,
                                         promptText=response_message)

    def _result_id_match_handler(self, command_result, message):
        if self.exc_type is not None:
            command_result.set_exception(self.exc_type,
                                         self.exc_value,
                                         self.exc_traceback)

            # The exception will be re-raised, we can already forget about it
            self._clear_exc_info()
            return

        if 'result' not in message:
            return

        if message['id'] != command_result.message_id:
            return

        self.unset_event_handler(command_result.get_id_handler())
        command_result.set(message)

    def _call_event_handlers(self, message):
        """
        Calls any event handlers that match the message

        Note that in set_default_event_handlers() many default handlers are
        set for errors and exception handling.

        The code that consumes DebugChromeInterface can also set specific event
        handlers to receive and manage any event type.

        :param message: The message that was received from Chrome
        :return: None. Exceptions might be raised by the event handler.
        """
        for handler in self._handlers:
            try:
                handler(message)
            except Exception as e:
                try:
                    function_name = handler.__name__
                except AttributeError:
                    # This is required to support partials which are used to
                    # receive responses with specific messages
                    function_name = handler.func.__name__

                msg = 'Found an exception while running %s: "%s"'
                args = (function_name, e)
                om.out.debug(msg % args)

                # The exceptions are raised in the DebugChromeInterface thread,
                # and need to be accessible to the main thread. That problem is
                # solved by storing the exception information and re-raising it
                # in the next call to DebugChromeInterface.*
                #
                # When an exception is raised by an event handler, the
                # handler will most likely NOT find the event that it was
                # waiting for and the client will timeout.
                #
                # Then, in the next call to DebugChromeInterface.* and in the
                # main thread, the exception raised in the event handler is
                # raised
                self._save_exc_info()

                # Run the next handlers, there is a risk here of two handlers
                # raising exceptions and the second overwriting the exception
                # information of the first, but it is a really strange case
                continue

    def set_dialog_handler(self, dialog_handler):
        self.dialog_handler = dialog_handler

    def send(self, data):
        self.debug('Sending message to Chrome: %s' % data)
        return self.ws.send(data)

    def recv(self):
        """
        :return: Reads bytes from the websocket and returns a parsed JSON object
        """
        try:
            data = self.ws.recv()
        except WebSocketTimeoutException:
            # The websocket raises this exception when there is no data to be read
            raise
        except socket.error:
            # [Errno 11] Resource temporarily unavailable
            #
            # The socket has no data to be read
            raise
        except WebSocketConnectionClosedException:
            # The connection has been closed
            raise

        except Exception, e:
            if self.ws is None:
                raise WebSocketConnectionClosedException('WebSocket does not exist anymore')

            msg = 'Unexpected error while reading from Chrome socket: "%s"'
            raise ChromeInterfaceException(msg % e)

        self.debug('Received (raw) message from Chrome: %s' % data)
        return data

    def close(self):
        try:
            super(DebugChromeInterface, self).close()
        finally:
            # This will force the thread to stop (see while self.ws in run())
            self.ws = None

    def get_command_result(self, message_id):
        """
        Creates a CommandResult and a handler to set the result.

        :param message_id: The result identifier to match in the chrome message
        :return: The CommandResult instance to .get() to retrieve the result
        """
        command_result = CommandResult(message_id)
        id_handler = functools.partial(self._result_id_match_handler, command_result)

        command_result.set_id_handler(id_handler)
        self.set_event_handler(id_handler)

        return command_result

    def run(self):
        """
        Runs in a different thread.

        Reads all messages from the websocket and sends them to the handlers.

        :return: None
        """
        while self.ws:
            try:
                # The recv() will lock for 1 second waiting for data
                data = self.recv()
            except WebSocketTimeoutException:
                # And raise an exception if there is no data in the buffer
                continue
            except socket.error as se:
                # [Errno 11] Resource temporarily unavailable
                # The socket has no data to be read, try again
                if se.errno == errno.EAGAIN:
                    continue

                # Not sure what this error is, raise in the next call
                # See comment below to understand how this works
                self._save_exc_info()
                continue
            except WebSocketConnectionClosedException:
                # The connection has been closed, but self.ws is still not
                # set to None. Just exit the thread.
                break

            # The data we receive from the wire can contain more than one
            # JSON document, we use parse_multi_json_docs() to parse all of
            # those messages and return them one by one
            for message in parse_multi_json_docs(data):
                self._call_event_handlers(message)

    def _save_exc_info(self):
        self.exc_type, self.exc_value, self.exc_traceback = sys.exc_info()

    def _clear_exc_info(self):
        self.exc_type = None
        self.exc_value = None
        self.exc_traceback = None

    def set_debugging_id(self, debugging_id):
        self.debugging_id = debugging_id

    def debug(self, message):
        if not self.DEBUG:
            return

        if not debugging_is_configured_in_output_manager():
            return

        message = '(did: %s) %s' % (self.debugging_id, message)
        om.out.debug(message)

    def read_console_message(self):
        """
        Reads one console log message from the deque and returns it.

        Note that by reading the message you are removing it from the
        dequeue and won't be able to read the same message again.

        :return: A ConsoleMessage instance, or None if the deque is empty
        """
        try:
            return self.console.popleft()
        except IndexError:
            return None

    def __getattr__(self, attr):
        """
        The client using this class reaches the __getattr__ method when calling
        self.chrome_conn.Page.setBypassCSP() , self.chrome_conn.Runtime.enable(),
        etc.

        :param attr: Page, Runtime, etc.
        :return: The callable DebugGenericElement
        """
        # Raise any exceptions that were found in the event handlers
        if self.exc_type is not None:
            exc_type = self.exc_type
            exc_value = self.exc_value
            exc_traceback = self.exc_traceback

            # Only raise once
            self._clear_exc_info()

            raise exc_type, exc_value, exc_traceback

        generic_element = DebugGenericElement(attr, self)
        return generic_element


class DebugGenericElement(GenericElement):

    def __getattr__(self, attr):
        func_name = '{}.{}'.format(self.name, attr)

        def generic_function(**kwargs):
            self.parent.pop_messages()
            self.parent.message_counter += 1

            timeout = kwargs.pop('timeout', 20)
            timeout = timeout or self.parent.timeout

            message_id = self.parent.message_counter

            call_obj = {'id': message_id,
                        'method': func_name,
                        'params': kwargs}
            call_str = json.dumps(call_obj)

            result = self.parent.get_command_result(message_id)

            self.parent.send(call_str)

            return result.get(timeout)

        return generic_function
