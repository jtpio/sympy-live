#!/usr/bin/python
#
# Copyright 2007 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
An interactive, stateful AJAX shell that runs Python code on the server.

Part of http://code.google.com/p/google-app-engine-samples/.

May be run as a standalone app or in an existing app as an admin-only handler.
Can be used for system administration tasks, as an interactive way to try out
APIs, or as a debugging aid during development.

The logging, os, sys, ndb, and users modules are imported automatically.

Interpreter state is stored in the datastore so that variables, function
definitions, and other values in the global and local namespaces can be used
across commands.

To use the shell in your app, copy shell.py, static/*, and templates/* into
your app's source directory. Then, copy the URL handlers from app.yaml into
your app.yaml.

TODO: unit tests!
"""

import ast
import logging
import new
import os
import pickle
import sys
import pdb
import traceback
import tokenize
import json
import rlcompleter
import datetime

from StringIO import StringIO
import six
# https://github.com/googleapis/python-ndb/issues/249#issuecomment-560957294
from app.constants import (
    UNPICKLABLE_TYPES,
    INITIAL_UNPICKLABLES,
    PREEXEC,
    PREEXEC_INTERNAL,
    PREEXEC_MESSAGE,
    VERBOSE_MESSAGE,
    VERBOSE_MESSAGE_SPHINX,
    PRINTERS
)

six.moves.reload_module(six)

# https://cloud.google.com/appengine/docs/standard/python/issue-requests#requests
import requests_toolbelt.adapters.appengine
# Use the App Engine Requests adapter. This makes sure that Requests uses URLFetch.
requests_toolbelt.adapters.appengine.monkeypatch()

from google.appengine.api import users
from google.cloud import ndb
from google.appengine.ext.webapp import template
from google.appengine.ext import webapp
from google.appengine.runtime import DeadlineExceededError
from google.appengine.runtime.apiproxy_errors import RequestTooLargeError

ndb_client = ndb.Client(project=os.environ['PROJECT_ID'])

sys.path.insert(0, os.path.join(os.getcwd(), 'sympy'))
sys.path.insert(0, os.path.join(os.getcwd(), 'mpmath'))

from sympy import sstr
from sympy.interactive.session import int_to_Integer

import settings

from .models import Session, Searches

LIVE_VERSION, LIVE_DEPLOYED = os.environ['CURRENT_VERSION_ID'].split('.')
LIVE_DEPLOYED = datetime.datetime.fromtimestamp(long(LIVE_DEPLOYED) / pow(2, 28))
LIVE_DEPLOYED = LIVE_DEPLOYED.strftime("%d/%m/%y %X")


def gdb():
    """Enter pdb in Google App Engine. """
    pdb.Pdb(stdin=getattr(sys, '__stdin__'),
            stdout=getattr(sys, '__stderr__')).set_trace(sys._getframe().f_back)


# Set to True if stack traces should be shown in the browser, etc.
_DEBUG = True

# The entity kind for shell sessions. Feel free to rename to suit your app.
_SESSION_KIND = '_Shell_Session'


def banner(quiet=False):
    from sympy import __version__ as sympy_version
    python_version = "%d.%d.%d" % sys.version_info[:3]

    message = "Python console for SymPy %s (Python %s)\n" % (sympy_version, python_version)

    if not quiet:
        source = ""

        for line in PREEXEC_MESSAGE.split('\n')[:-1]:
            if not line:
                source += '\n'
            else:
                source += '>>> ' + line + '\n'

        docs_version = sympy_version
        if 'git' in sympy_version or '.rc' in sympy_version:
            docs_version = 'dev'

        message += '\n' + VERBOSE_MESSAGE % {
            'source': source,
            'version': docs_version
        }

    return message


def banner_sphinx(quiet=False):
    from sympy import __version__ as sympy_version
    python_version = "%d.%d.%d" % sys.version_info[:3]

    message = "Python console for SymPy %s (Python %s)\n" % (sympy_version, python_version)

    if not quiet:
        source = ""

        for line in PREEXEC_MESSAGE.split('\n')[:-1]:
            if not line:
                source += '\n'
            else:
                source += '>>> ' + line + '\n'

        message += '\n' + VERBOSE_MESSAGE_SPHINX % {'source': source}

    return message


class Live(object):

    _header = 'Traceback (most recent call last):\n'
    _file = '<string>'

    def traceback(self, offset=None):
        """Return nicely formatted most recent traceback. """
        etype, value, tb = sys.exc_info()

        if tb.tb_next is not None:
            _tb = tb.tb_next
        else:
            _tb = tb

        try:
            if offset is not None:
                lines = traceback.extract_tb(_tb)

                line = lines[0][1] + offset
                lines[0] = (lines[0][0], line) + lines[0][2:]

                text = [self._header]
                text = text + traceback.format_list(lines)
                text = text + traceback.format_exception_only(etype, value)

                line = lines[0][1]
            else:
                text = traceback.format_exception(etype, value, _tb)
                line = _tb.tb_lineno
        finally:
            del tb, _tb

        return ''.join(text), line

    def syntaxerror(self):
        """Return nicely formatted syntax error. """
        etype, value, sys.last_traceback = sys.exc_info()

        sys.last_type = etype
        sys.last_value = value

        # extract info from error value (specifcally, we want the line number)
        try:
            msg, (dummy_filename, line, offset, source) = value
        except:
            pass
        else:
            # re-package error with `self._file` instead of `dummy_filename`
            value = etype(msg, (self._file, line, offset, source))
            sys.last_value = value

        text = [self._header]
        text = text + traceback.format_exception_only(etype, value)

        return ''.join(text), line

    def error(self, stream, error):
        """Write error message to a stream. """
        if stream is not None:
            stream.write(error[0])

    def split(self, source):
        """Extract last logical line from multi-line source code. """
        string = StringIO(source).readline

        try:
            tokens = list(tokenize.generate_tokens(string))
        except (OverflowError, SyntaxError, ValueError, tokenize.TokenError):
            return None, source

        for tok, _, (n, _), _, _ in reversed(tokens):
            if tok == tokenize.NEWLINE:
                lines = source.split('\n')

                exec_source = '\n'.join(lines[:n])
                eval_source = '\n'.join(lines[n:])

                return exec_source, eval_source
        else:
            return None, source

    def compile(self, source, mode):
        """Wrapper over Python's built-in function. """
        return compile(source, self._file, mode)

    def complete(self, statement, session):
        """Autocomplete the statement in the session's globals."""

        statement_module = new.module('__main__')
        import __builtin__
        statement_module.__builtin__ = __builtin__

        old_main = sys.modules.get('__main__')

        try:
            sys.modules['__main__'] = statement_module

            statement_module.__name__ = '__main__'

            # re-evaluate the unpicklables
            for code in session.unpicklables:
                exec code in statement_module.__dict__

            old_globals = dict(statement_module.__dict__)

            # re-initialize the globals
            session_globals_dict = session.globals_dict()

            for name, val in session_globals_dict.items():
                try:
                    statement_module.__dict__[name] = val
                except:
                    session.remove_global(name)

            __builtin__._ = session_globals_dict.get('_')

            completer = rlcompleter.Completer(statement_module.__dict__)

            if '=' in statement:
                statement = statement.split('=', 1)[1].strip()
            # XXX need a better way to do this
            if '.' in statement:
                return completer.attr_matches(statement)
            else:
                return completer.global_matches(statement)

        finally:
            sys.modules['__main__'] = old_main
            try:
                del __builtin__._
            except AttributeError:
                pass

    def evaluate(self, statement, session, printer=None, stream=None):
        """Evaluate the statement in sessions's globals. """
        # the Python compiler doesn't like network line endings
        source = statement.replace('\r\n', '\n').rstrip()

        # allow spaces before one-liners (to emulate Python shell's behaviour)
        if '\n' not in source:
            source = source.lstrip()

        try:
            # check for a SyntaxError now; this way the user will see their
            # original statement and not the transformed one
            ast.parse(source)
        except SyntaxError:
            return self.error(stream, self.syntaxerror())

        # convert int to Integer (1/2 -> Integer(1)/Integer(2))
        source = int_to_Integer(source)

        # split source code into 'exec' and 'eval' parts
        exec_source, eval_source = self.split(source)

        try:
            self.compile(eval_source, 'eval')
        except (OverflowError, SyntaxError, ValueError):
            exec_source, eval_source = source, None

        if exec_source is not None:
            exec_source += '\n'
        if eval_source is not None:
            eval_source += '\n'

        # create a dedicated module to be used as this statement's __main__
        statement_module = new.module('__main__')

        # use this request's __builtin__, since it changes on each request.
        # this is needed for import statements, among other things.
        import __builtin__
        statement_module.__builtin__ = __builtin__

        # create customized display hook
        stringify_func = printer or sstr

        def displayhook(arg):
            if arg is not None:
                __builtin__._ = None
                print stringify_func(arg)
                __builtin__._ = arg

        old_displayhook = sys.displayhook
        sys.displayhook = displayhook

        # swap in our custom module for __main__. then unpickle the session
        # globals, run the statement, and re-pickle the session globals, all
        # inside it.
        old_main = sys.modules.get('__main__')

        try:
            old_globals = {}
            sys.modules['__main__'] = statement_module
            statement_module.__name__ = '__main__'

            # re-evaluate the unpicklables
            for code in session.unpicklables:
                exec code in statement_module.__dict__
                exec code in old_globals

            # re-initialize the globals
            session_globals_dict = session.globals_dict()

            for name, val in session_globals_dict.items():
                try:
                    statement_module.__dict__[name] = val
                    old_globals[name] = val
                except:
                    session.remove_global(name)

            # re-initialize '_' special variable
            __builtin__._ = session_globals_dict.get('_')

            # run!
            offset = 0

            try:
                old_stdout = sys.stdout
                old_stderr = sys.stderr

                try:
                    if stream is not None:
                        sys.stdout = stream
                        sys.stderr = stream

                    if exec_source is not None:
                        try:
                            exec_code = self.compile(exec_source, 'exec')
                        except (OverflowError, SyntaxError, ValueError):
                            return self.error(stream, self.syntaxerror())

                        eval(exec_code, statement_module.__dict__)

                    if eval_source is not None:
                        if exec_source is not None:
                            offset = len(exec_source.split('\n'))

                        result = eval(eval_source, statement_module.__dict__)
                        sys.displayhook(result)
                finally:
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr
            except DeadlineExceededError:
                logging.debug("is deadlineexceedederror in evaluate")
                raise DeadlineExceededError
            except:
                return self.error(stream, self.traceback(offset))

            # extract the new globals that this statement added
            new_globals = {}

            for name, val in statement_module.__dict__.items():
                if name not in old_globals or val != old_globals[name]:
                    new_globals[name] = val

            for name in old_globals:
                if name not in statement_module.__dict__:
                    session.remove_global(name)

            if True in [isinstance(val, UNPICKLABLE_TYPES) for val in new_globals.values()]:
                # this statement added an unpicklable global. store the statement and
                # the names of all of the globals it added in the unpicklables
                source = ""

                if exec_source:
                    source += exec_source
                if eval_source:
                    source += eval_source

                source += "\n"

                session.add_unpicklable(source, new_globals.keys())
                logging.debug('Storing this statement as an unpicklable.')
            else:
                # this statement didn't add any unpicklables. pickle and store the
                # new globals back into the datastore
                for name, val in new_globals.items():
                    if not name.startswith('__'):
                        try:
                            session.set_global(name, val)
                        except (TypeError, pickle.PicklingError):
                            pass

            # save '_' special variable into the datastore
            val = getattr(__builtin__, '_', None)

            try:
                session.set_global('_', val)
            except (TypeError, pickle.PicklingError):
                session.set_global('_', None)
        finally:
            sys.modules['__main__'] = old_main
            sys.displayhook = old_displayhook

            try:
                del __builtin__._
            except AttributeError:
                pass

        try:
            with ndb_client.context():
                session.put()
        except RequestTooLargeError:
            stream.truncate(0) # clear output
            self.error(stream, ('Unable to process statement due to its excessive size.',))


class ForceDesktopCookieHandler(webapp.RequestHandler):
    def get(self):
        #Cookie stuff
        import Cookie
        import datetime

        expiration = datetime.datetime.now() + datetime.timedelta(days=1000)
        cookie = Cookie.SimpleCookie()
        cookie["desktop"] = "yes"
        #cookie["desktop"]["domain"] = "live.sympy.org"
        cookie["desktop"]["path"] = "/"
        cookie["desktop"]["expires"] = \
        expiration.strftime("%a, %d-%b-%Y %H:%M:%S PST")
        print cookie.output()
        template_file = os.path.join(os.path.dirname(__file__), '../templates', 'redirect.html')
        vars = { 'server_software': os.environ['SERVER_SOFTWARE'],
                 'python_version': sys.version,
                 'user': users.get_current_user(),
                 }
        rendered = webapp.template.render(template_file, vars, debug=_DEBUG)
        self.response.out.write(rendered)


class FrontPageHandler(webapp.RequestHandler):
    """Creates a new session and renders the ``shell.html`` template. """

    def get(self):
        #Get the 10 most recent queries
        with ndb_client.context():
            searches_query = Searches.query_(Searches.private == False).order(-Searches.timestamp)
            search_results = [result.query for result in searches_query.fetch(10)]
            user = users.get_current_user()
            if user:
                _saved_searches = Searches.query_(Searches.user_id == user.user_id()).order(-Searches.timestamp).fetch()
                saved_searches = [search.query for search in _saved_searches]
            else:
                saved_searches = []
            saved_searches_count = len(saved_searches)
        template_file = os.path.join(os.path.dirname(__file__), '../templates', 'shell.html')

        vars = {
            'server_software': os.environ['SERVER_SOFTWARE'],
            'application_version': LIVE_VERSION,
            'current_year': datetime.datetime.utcnow().year,
            'date_deployed': LIVE_DEPLOYED,
            'python_version': sys.version,
            'user': users.get_current_user(),
            'login_url': users.create_login_url('/'),
            'logout_url': users.create_logout_url('/'),
            'banner': banner(),
            'printer': self.request.get('printer').lower() or '',
            'submit': self.request.get('submit').lower() or '',
            'tabWidth': self.request.get('tabWidth').lower() or 'undefined',
            'searches': search_results,
            'has_searches': bool(search_results),
            'saved_searches': saved_searches,
            'has_saved_searches': saved_searches_count
        }

        rendered = webapp.template.render(template_file, vars, debug=_DEBUG)
        self.response.out.write(rendered)


class CompletionHandler(webapp.RequestHandler):
    """Takes an incomplete statement and returns possible completions."""

    def _cross_site_headers(self):
        self.response.headers['Access-Control-Allow-Origin'] = '*'
        self.response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Requested-With'

    def options(self):
        self._cross_site_headers()

    def post(self):
        self._cross_site_headers()
        try:
            message = json.loads(self.request.body)
        except ValueError:
            self.error(400)
            return

        session_key = message.get('session')
        statement = message.get('statement').encode('utf-8')
        live = Live()

        if session_key is not None:
            try:
                with ndb_client.context():
                    session = ndb.Key(urlsafe=session_key).get()
            except ndb.exceptions.Error:
                self.error(400)
                return
        else:
            with ndb_client.context():
                session = Session()
                session.unpicklables = [line for line in INITIAL_UNPICKLABLES]
                session_key = session.put().urlsafe()

            live.evaluate(PREEXEC, session)
            live.evaluate(PREEXEC_INTERNAL, session)

        completions = list(sorted(set(live.complete(statement, session))))
        if not statement.split('.')[-1].startswith('_'):
            completions = [x for x in completions if
                           not x.split('.')[-1].startswith('_')]

        # From http://stackoverflow.com/a/1916632
        # Get longest common prefix to fill instantly
        common = os.path.commonprefix(completions)

        result = {
            'session': str(session_key),
            'completions': completions,
            'prefix': common
        }

        self.response.headers['Content-Type'] = 'application/json'
        self.response.out.write(json.dumps(result))


class EvaluateHandler(webapp.RequestHandler):
    """Evaluates a Python statement in a given session and returns the result. """

    def _cross_site_headers(self):
        self.response.headers['Access-Control-Allow-Origin'] = '*'
        self.response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Requested-With'

    def options(self):
        self._cross_site_headers()

    def post(self):
        self._cross_site_headers()

        try:
            message = json.loads(self.request.body)
        except ValueError:
            self.error(400)
            return

        # Code modified to store each query in a database
        print_statement = '\n'.join(message.get('print_statement'))
        statement = message.get('statement')
        privacy = message.get('privacy')

        with ndb_client.context():
            if statement != '':
                user = users.get_current_user()

                searches = Searches()
                searches.user_id = user.user_id() if user else None
                searches.query = print_statement

            if privacy == 'off': searches.private = False
            if privacy == 'on': searches.private = True

            searches.put()

        session_key = message.get('session')
        printer_key = message.get('printer')
        live = Live()

        if session_key is not None:
            try:
                with ndb_client.context():
                    session = ndb.Key(urlsafe=session_key).get()
            except ndb.exceptions.Error:
                self.error(400)
                return
        else:
            with ndb_client.context():
                session = Session()
                session.unpicklables = [line for line in INITIAL_UNPICKLABLES]
                session_key = session.put().urlsafe()

            live.evaluate(PREEXEC, session)
            live.evaluate(PREEXEC_INTERNAL, session)

        try:
            printer = PRINTERS[printer_key]
        except KeyError:
            printer = None

        stream = StringIO()
        try:
            live.evaluate(statement, session, printer, stream)
            result = {
                'session': str(session_key),
                'output': stream.getvalue(),
            }
        except DeadlineExceededError:
            result = {
                'session': str(session_key),
                'output': 'Error: Operation timed out.'
            }
        except Exception, e:
            if settings.DEBUG:
                errmsg = '\n'.join([
                    'Exception in SymPy Live of type ',
                    str(type(e)),
                    'for reference the stack trace is',
                    traceback.format_exc()
                ])
            else:
                errmsg = '\n'.join([
                    'Exception in SymPy Live of type ',
                    str(type(e)),
                    'for reference the last 5 stack trace entries are',
                    traceback.format_exc(5)
                ])
            result = {
                'session': str(session_key),
                'output': errmsg
            }

        self.response.headers['Content-Type'] = 'application/json'
        self.response.out.write(json.dumps(result))


class SphinxBannerHandler(webapp.RequestHandler):
    """Provides the banner for the Sphinx extension.
    """

    def _cross_site_headers(self):
        self.response.headers['Access-Control-Allow-Origin'] = '*'
        self.response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Requested-With'

    def get(self):
        self._cross_site_headers()
        self.response.headers['Content-Type'] = 'text/plain'
        self.response.out.write(banner_sphinx())


class DeleteHistory(webapp.RequestHandler):
    """Deletes all of the user's history"""

    def get(self):
        with ndb_client.context():
            user = users.get_current_user()
            results = Searches.query_(Searches.user_id == user.user_id()).order(-Searches.timestamp)

            for result in results:
                result.key.delete()

        self.response.out.write("Your queries have been deleted.")


class RedirectHandler(webapp.RedirectHandler):
    """Redirects deprecated pages to the frontpage."""

    def get(self):
        self.redirect('/', permanent=True)


class StatusHandler(webapp.RequestHandler):
    """Status endpoint to check if the app is running or not."""

    def get(self):
        self.response.headers['Content-Type'] = 'application/json'
        self.response.out.write(json.dumps({"status": "ok"}))
