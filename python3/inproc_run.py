import builtins as builtin_mod
import code
import enum
from functools import partial
import logging
import os
import sys
import time
import traceback
import types

import simplejson as json
from IPython.core.completer import Completer

import getpass

from sorna.types import (
    InputRequest, ControlRecord, ConsoleRecord, MediaRecord, HTMLRecord, CompletionRecord,
)


log = logging.getLogger()

class StreamToEmitter:

    def __init__(self, emitter, stream_type):
        self.emit = emitter
        self.stream_type = stream_type

    def write(self, s):
        self.emit(ConsoleRecord(self.stream_type, s))

    def flush(self):
        pass


class PythonInprocRunner:
    '''
    A thin wrapper for REPL.

    It creates a dummy module that user codes run and keeps the references to user-created objects
    (e.g., variables and functions).
    '''

    def __init__(self, runner):
        self.runner = runner
        self.insock = runner.insock
        self.outsock = runner.outsock

        self.stdout_emitter = StreamToEmitter(self.emit, 'stdout')
        self.stderr_emitter = StreamToEmitter(self.emit, 'stderr')

        # Initialize user module and namespaces.
        user_module = types.ModuleType('__main__',
                                       doc='Automatically created module for the interactive shell.')
        user_module.__dict__.setdefault('__builtin__', builtin_mod)
        user_module.__dict__.setdefault('__builtins__', builtin_mod)
        self.user_module = user_module
        self.user_ns = user_module.__dict__

        self.completer = Completer(namespace=self.user_ns, global_namespace={})
        self.completer.limit_to__all__ = True

    def handle_input(self, prompt=None, password=False):
        if prompt is None:
            prompt = 'Password: ' if password else ''
        # Use synchronous version of ZeroMQ sockets
        raw_insock = self.insock.transport._zmq_sock
        raw_outsock = self.outsock.transport._zmq_sock
        raw_outsock.send_multipart([
            b'stdout',
            prompt.encode('utf8'),
        ])
        raw_outsock.send_multipart([
            b'waiting-input',
            json.dumps({'is_password': password}).encode('utf8'),
        ])
        data = raw_insock.recv_multipart()
        return data[1].decode('utf8')

    def get_completions(self, data):
        state = 0
        matches = []
        while True:
            ret = self.completer.complete(data['line'], state)
            if ret is None:
                break
            matches.append(ret)
            state += 1
        return matches

    def emit(self, record):
        if isinstance(record, ConsoleRecord):
            assert record.target in ('stdout', 'stderr')
            self.outsock.write([
                record.target.encode('ascii'),
                record.data.encode('utf8'),
            ])
        elif isinstance(record, MediaRecord):
            self.outsock.write([
                b'media',
                json.dumps({
                    'type': record.type,
                    'data': record.data,
                }).encode('utf8'),
            ])
        elif isinstance(record, HTMLRecord):
            self.outsock.write([
                b'html',
                record.html.encode('utf8'),
            ])
        elif isinstance(record, InputRequest):
            self.outsock.write([
                b'waiting-input',
                json.dumps({
                    'is_password': record.is_password,
                }).encode('utf8'),
            ])
        elif isinstance(record, CompletionRecord):
            self.outsock.write([
                b'completion',
                json.dumps(record.matches).encode('utf8'),
            ])
        elif isinstance(record, ControlRecord):
            self.outsock.write([
                record.event.encode('ascii'),
                b'',
            ])
        else:
            raise TypeError('Unsupported record type.')

    @staticmethod
    def strip_traceback(tb):
        while tb is not None:
            frame_summary = traceback.extract_tb(tb, limit=1)[0]
            if frame_summary[0] == '<input>':
                break
            tb = tb.tb_next
        return tb

    def query(self, code_text):
        # Set Sorna Media handler
        self.user_module.__builtins__._sorna_emit = self.emit

        # Override interactive input functions
        self.user_module.__builtins__.input = self.handle_input
        getpass.getpass = partial(self.handle_input, password=True)

        try:
            code_obj = code.compile_command(code_text, symbol='exec')
        except (OverflowError, IndentationError, SyntaxError,
                ValueError, TypeError, MemoryError) as e:
            exc_type, exc_val, tb = sys.exc_info()
            user_tb = type(self).strip_traceback(tb)
            err_str = ''.join(traceback.format_exception(exc_type, exc_val, user_tb))
            hdr_str = 'Traceback (most recent call last):\n' if not err_str.startswith('Traceback ') else ''
            self.emit(ConsoleRecord('stderr', hdr_str + err_str))
            self.emit(ControlRecord('finished'))
        else:
            sys.stdout, orig_stdout = self.stdout_emitter, sys.stdout
            sys.stderr, orig_stderr = self.stderr_emitter, sys.stderr
            try:
                exec(code_obj, self.user_ns)
            except Exception as e:
                # strip the first frame
                exc_type, exc_val, tb = sys.exc_info()
                user_tb = type(self).strip_traceback(tb)
                traceback.print_exception(exc_type, exc_val, user_tb)
            finally:
                self.emit(ControlRecord('finished'))
                sys.stdout = orig_stdout
                sys.stderr = orig_stderr