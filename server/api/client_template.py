#!/usr/bin/env python3

import argparse
import binascii
import itertools
import logging
import os
import random
import re
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from math import ceil
from urllib.parse import urljoin
from dataclasses import dataclass

import requests


if sys.version_info < (3, 4):
    logging.critical('Support of Python < 3.4 is not implemented yet')
    sys.exit(1)

os_windows = (os.name == 'nt')

HEADER = '''

  ,ad8888ba,                88  88                                           88
 d8"'    `"8b               88  88                                           88
d8'                         88  88                                           88
88             88       88  88  88,dPPYba,   8b      db      d8   ,adPPYba,  88,dPPYba,
88             88       88  88  88P'    "8a  `8b    d88b    d8'  a8"     ""  88P'    "8a
Y8,            88       88  88  88       88   `8b  d8'`8b  d8'   8b          88       88
 Y8a.    .a8P  "8a,   ,a88  88  88       88    `8bd8'  `8bd8'    "8a,   ,aa  88       88
  `"Y8888Y"'    `"YbbdP'Y8  88  88       88      YP      YP       `"Ybbd8"'  88       88

by @arkiix of 89cr3w
'''[1:]


@dataclass
class Team:
    team_name: str
    team_ip: str
    team_id: int = None


class Style(Enum):
    """
    Bash escape sequences, see:
    https://misc.flogisoft.com/bash/tip_colors_and_formatting
    """

    BOLD = 1

    FG_BLACK = 30
    FG_RED = 31
    FG_GREEN = 32
    FG_YELLOW = 33
    FG_BLUE = 34
    FG_MAGENTA = 35
    FG_CYAN = 36
    FG_LIGHT_GRAY = 37


BRIGHT_COLORS = [Style.FG_RED, Style.FG_GREEN, Style.FG_BLUE,
                 Style.FG_MAGENTA, Style.FG_CYAN]

VERBOSE_LINES = 5


def highlight(text, style=None):
    if os_windows:
        return text

    if style is None:
        style = [Style.BOLD, random.choice(BRIGHT_COLORS)]
    return '\033[{}m'.format(';'.join(str(item.value) for item in style)) + text + '\033[0m'


log_format = '%(asctime)s {} %(message)s'.format(highlight('%(levelname)s', [Style.FG_YELLOW]))
logging.basicConfig(format=log_format, datefmt='%H:%M:%S', level=logging.DEBUG)


def parse_args():
    parser = argparse.ArgumentParser(description='Run a sploit on all teams in a loop',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('sploit',
                        help="Sploit executable (should take a victim's host as the first argument)")
    parser.add_argument('--server-url', metavar='URL', default='{{server_url}}',
                        help='Server URL')
    parser.add_argument('--server-pass', metavar='PASS', default='{{server_password}}',
                        help='Server password')
    parser.add_argument('--interpreter', metavar='COMMAND',
                        help='Explicitly specify sploit interpreter (use on Windows, which doesn\'t '
                             'understand shebangs)')

    parser.add_argument('--pool-size', metavar='N', type=int, default=50,
                        help='Maximal number of concurrent sploit instances. '
                             'Too little value will make time limits for sploits smaller, '
                             'too big will eat all RAM on your computer')
    parser.add_argument('--attack-period', metavar='N', type=float, default=int('{{round_length}}'),
                        help='Rerun the sploit on all teams each N seconds '
                             'Too little value will make time limits for sploits smaller, '
                             'too big will miss flags from some rounds')

    parser.add_argument('-v', '--verbose-attacks', metavar='N', type=int, default=1,
                        help="Sploits' outputs and found flags will be shown for the N first attacks")

    parser.add_argument('-e', '--endless', default=False, action='store_true',
                        help="Run sploits without timeouts")

    group = parser.add_mutually_exclusive_group()
    group.add_argument('--not-per-team', action='store_true',
                       help='Run a single instance of the sploit instead of an instance per team')
    group.add_argument('--distribute', metavar='K/N',
                       help='Divide the team list to N parts (by address hash modulo N) '
                            'and run the sploits only on Kth part of it (K >= 1)')

    return parser.parse_args()


def fix_args(args):
    check_sploit(args)

    if '://' not in args.server_url:
        args.server_url = 'http://' + args.server_url

    if args.distribute is not None:
        valid = False
        match = re.fullmatch(r'(\d+)/(\d+)', str(args.distribute))
        if match is not None:
            k, n = (int(match.group(1)), int(match.group(2)))
            if n >= 2 and 1 <= k <= n:
                args.distribute = k, n
                valid = True

        if not valid:
            raise ValueError('Wrong syntax for --distribute, use --distribute K/N (N >= 2, 1 <= K <= N)')


SCRIPT_EXTENSIONS = {
    '.pl': 'perl',
    '.py': 'python',
    '.rb': 'ruby',
}


def check_script_source(source):
    errors = []
    if not os_windows and source[:2] != '#!':
        errors.append(
            'Please use shebang (e.g. {}) as the first line of your script'.format(
                highlight('#!/usr/bin/env python3', [Style.FG_GREEN])))
    if re.search(r'flush[(=]', source) is None:
        errors.append(
            'Please print the newline and call {} each time after your sploit outputs flags. '
            'In Python 3, you can use {}. '
            'Otherwise, the flags may be lost (if the sploit process is killed) or '
            'sent with a delay.'.format(
                highlight('flush()', [Style.FG_RED]),
                highlight('print(..., flush=True)', [Style.FG_GREEN])))
    return errors


class InvalidSploitError(Exception):
    pass


def check_sploit(args):
    path = args.sploit
    if not os.path.isfile(path):
        raise ValueError('No such file: {}'.format(path))

    extension = os.path.splitext(path)[1].lower()
    is_script = extension in SCRIPT_EXTENSIONS
    if is_script:
        with open(path, 'r', errors='ignore') as f:
            source = f.read()
        errors = check_script_source(source)

        if errors:
            for message in errors:
                logging.error(message)
            raise InvalidSploitError('Sploit won\'t be run because of validation errors')

        if os_windows and args.interpreter is None:
            args.interpreter = SCRIPT_EXTENSIONS[extension]
            logging.info('Using interpreter `{}`'.format(args.interpreter))

    if not os_windows:
        file_mode = os.stat(path).st_mode
        # TODO: May be check the owner and other X flags properly?
        if not file_mode & stat.S_IXUSR:
            if is_script:
                logging.info('Setting the executable bit on `{}`'.format(path))
                os.chmod(path, file_mode | stat.S_IXUSR)
            else:
                raise InvalidSploitError("The provided file doesn't appear to be executable")


if os_windows:
    # By default, Ctrl+C does not work on Windows if we spawn subprocesses.
    # Here we fix that using WinApi. See https://stackoverflow.com/a/43095532

    import signal
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # BOOL WINAPI HandlerRoutine(
    #   _In_ DWORD dwCtrlType
    # );
    PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    win_ignore_ctrl_c = PHANDLER_ROUTINE()  # = NULL


    def _errcheck_bool(result, _, args):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return args


    # BOOL WINAPI SetConsoleCtrlHandler(
    #   _In_opt_ PHANDLER_ROUTINE HandlerRoutine,
    #   _In_     BOOL             Add
    # );
    kernel32.SetConsoleCtrlHandler.errcheck = _errcheck_bool
    kernel32.SetConsoleCtrlHandler.argtypes = (PHANDLER_ROUTINE, wintypes.BOOL)


    @PHANDLER_ROUTINE
    def win_ctrl_handler(dwCtrlType):
        if dwCtrlType == signal.CTRL_C_EVENT:
            kernel32.SetConsoleCtrlHandler(win_ignore_ctrl_c, True)
            shutdown()
        return False


    kernel32.SetConsoleCtrlHandler(win_ctrl_handler, True)


class APIException(Exception):
    pass


SERVER_TIMEOUT = 5


def get_auth_headers(args):
    return {'Authorization': args.server_pass}


def get_config(args):
    url = urljoin(args.server_url, '/api/settings')
    headers = get_auth_headers(args)

    r = requests.get(url, headers=headers, timeout=SERVER_TIMEOUT)
    if not r.ok:
        raise APIException(r.text)

    return r.json().get('settings')


def init_sploit(args):
    sploit_name = os.path.basename(args.sploit)

    url = urljoin(args.server_url, '/api/sploits')
    headers = get_auth_headers(args)
    data = {'sploit_name': sploit_name}

    r = requests.post(url, headers=headers, json=data, timeout=SERVER_TIMEOUT)
    if not r.ok:
        raise APIException(r.text)

    return r.json().get('sploit_id')


def get_teams(args):
    url = urljoin(args.server_url, '/api/teams')
    headers = get_auth_headers(args)

    r = requests.get(url, headers=headers, timeout=SERVER_TIMEOUT)
    if not r.ok:
        raise APIException(r.text)

    return list(map(
        lambda t: Team(
            t.get('team_name'),
            t.get('team_ip'),
            t.get('team_id')
        ),
        r.json().get('teams')
    ))



def post_flags(args, flags, sploit_id):
    data = [
        {'flag': item['flag'], 'sploit_id': sploit_id, 'team_id': item['team_id']}
        for item in flags
    ]

    url = urljoin(args.server_url, '/api/flags')
    headers = get_auth_headers(args)
    r = requests.post(url, headers=headers, json=data, timeout=SERVER_TIMEOUT)
    if not r.ok:
        raise APIException(r.text)


exit_event = threading.Event()


def once_in_a_period(period):
    for iter_no in itertools.count(1):
        start_time = time.time()
        yield iter_no

        time_spent = time.time() - start_time
        if period > time_spent:
            exit_event.wait(period - time_spent)
        if exit_event.is_set():
            break


class FlagStorage:
    """
    Thread-safe storage comprised of a set and a post queue.

    Any number of threads may call add(), but only one "consumer thread"
    may call pick_flags() and mark_as_sent().
    """

    def __init__(self):
        self._flags_seen = set()
        self._queue = []
        self._lock = threading.RLock()

    def add(self, flags, team_id):
        with self._lock:
            for item in flags:
                if item not in self._flags_seen:
                    self._flags_seen.add(item)
                    self._queue.append({'flag': item, 'team_id': team_id})

    def pick_flags(self, count):
        with self._lock:
            return self._queue[:count]

    def mark_as_sent(self, count):
        with self._lock:
            self._queue = self._queue[count:]

    @property
    def queue_size(self):
        with self._lock:
            return len(self._queue)


flag_storage = FlagStorage()

POST_PERIOD = 5
POST_FLAG_LIMIT = 10000


# TODO: test that 10k flags won't lead to the hangup of the farm server


def run_post_loop(args):
    try:
        sploit_id = init_sploit(args)

        for _ in once_in_a_period(POST_PERIOD):
            flags_to_post = flag_storage.pick_flags(POST_FLAG_LIMIT)

            if flags_to_post:
                try:
                    post_flags(args, flags_to_post, sploit_id)

                    flag_storage.mark_as_sent(len(flags_to_post))
                    logging.info('{} flags posted to the server ({} in the queue)'.format(
                        len(flags_to_post), flag_storage.queue_size))
                except Exception as e:
                    logging.error("Can't post flags to the server: {}".format(repr(e)))
                    logging.info("The flags will be posted next time")
    except Exception as e:
        logging.critical('Posting loop died: {}'.format(repr(e)))
        shutdown()


display_output_lock = threading.RLock()


def display_sploit_output(team_name, output_lines):
    if not output_lines:
        logging.info('{}: No output from the sploit'.format(team_name))
        return

    prefix = highlight(team_name + ': ')
    with display_output_lock:
        print('\n' + '\n'.join(prefix + line.rstrip() for line in output_lines) + '\n')


def process_sploit_output(stream, args, team, flag_format, attack_no):
    try:
        output_lines = []
        instance_flags = set()

        line_cnt = 1

        while True:
            line = stream.readline()
            if not line:
                break

            line = line.decode(errors='replace')
            output_lines.append(line)

            line_flags = set(flag_format.findall(line))
            if line_flags:
                flag_storage.add(line_flags, team.team_id)
                instance_flags |= line_flags

            if args.endless and line_cnt <= args.verbose_attacks * VERBOSE_LINES:
                line_cnt += 1
                display_sploit_output(team.team_name, output_lines)
                output_lines = []
                if instance_flags:
                    logging.info('Got {} flags from "{}": {}'.format(
                        len(instance_flags), team.team_name, instance_flags))
                    instance_flags = set()

        if attack_no <= args.verbose_attacks and not exit_event.is_set():
            # We don't want to spam the terminal on KeyboardInterrupt

            display_sploit_output(team.team_name, output_lines)
            if instance_flags:
                logging.info('Got {} flags from "{}": {}'.format(
                    len(instance_flags), team.team_name, instance_flags))
    except Exception as e:
        logging.error('Failed to process sploit output: {}'.format(repr(e)))


class InstanceStorage:
    """
    Storage comprised of a dictionary of all running sploit instances and some statistics.

    Always acquire instance_lock before using this class. Do not release the lock
    between actual spawning/killing a process and calling register_start()/register_stop().
    """

    def __init__(self):
        self._counter = 0
        self.instances = {}

        self.n_completed = 0
        self.n_killed = 0

    def register_start(self, process):
        instance_id = self._counter
        self.instances[instance_id] = process
        self._counter += 1
        return instance_id

    def register_stop(self, instance_id, was_killed):
        del self.instances[instance_id]

        self.n_completed += 1
        self.n_killed += was_killed


instance_storage = InstanceStorage()
instance_lock = threading.RLock()


def launch_sploit(args, team, attack_no, flag_format):
    # For sploits written in Python, this env variable forces the interpreter to flush
    # stdout and stderr after each newline. Note that this is not default behavior
    # if the sploit's output is redirected to a pipe.
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'

    command = [os.path.abspath(args.sploit)]
    if args.interpreter is not None:
        command = [args.interpreter] + command
    if team.team_ip is not None:
        command.append(team.team_ip)
    need_close_fds = (not os_windows)

    if os_windows:
        # On Windows, we block Ctrl+C handling, spawn the process, and
        # then recover the handler. This is the only way to make Ctrl+C
        # intercepted by us instead of our child processes.
        kernel32.SetConsoleCtrlHandler(win_ignore_ctrl_c, True)
    proc = subprocess.Popen(command,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=1, close_fds=need_close_fds, env=env)
    if os_windows:
        kernel32.SetConsoleCtrlHandler(win_ignore_ctrl_c, False)

    threading.Thread(target=lambda: process_sploit_output(
        proc.stdout, args, team, flag_format, attack_no)).start()

    return proc, instance_storage.register_start(proc)


def run_sploit(args, team, attack_no, max_runtime, flag_format):
    try:
        with instance_lock:
            if exit_event.is_set():
                return

            proc, instance_id = launch_sploit(args, team, attack_no, flag_format)
    except Exception as e:
        if isinstance(e, FileNotFoundError):
            logging.error('Sploit file or the interpreter for it not found: {}'.format(repr(e)))
            logging.error('Check presence of the sploit file and the shebang (use {} for compatibility)'.format(
                highlight('#!/usr/bin/env ...', [Style.FG_GREEN])))
        else:
            logging.error('Failed to run sploit: {}'.format(repr(e)))

        if attack_no == 1:
            shutdown()
        return

    try:
        try:
            proc.wait(timeout=max_runtime)
            need_kill = False
        except subprocess.TimeoutExpired:
            need_kill = True
            if attack_no <= args.verbose_attacks:
                logging.warning('Sploit for "{}" ({}) ran out of time'.format(team.team_name, team.team_ip))

        with instance_lock:
            if need_kill:
                proc.kill()

            instance_storage.register_stop(instance_id, need_kill)
    except Exception as e:
        logging.error('Failed to finish sploit: {}'.format(repr(e)))


def show_time_limit_info(args, config, max_runtime, attack_no):
    if attack_no == 1:
        min_attack_period = config['flag_lifetime'] - config['submit_period'] - POST_PERIOD
        if args.attack_period >= min_attack_period:
            logging.warning("--attack-period should be < {:.1f} sec, "
                            "otherwise the sploit will not have time "
                            "to catch flags for each round before their expiration".format(min_attack_period))

    if max_runtime is not None:
        logging.info('Time limit for a sploit instance: {:.1f} sec'.format(max_runtime))
    else:
        logging.info('Time limit for a sploit instance: endless')

    with instance_lock:
        if instance_storage.n_completed > 0:
            # TODO: Maybe better for 10 last attacks
            logging.info('Total {:.1f}% of instances ran out of time'.format(
                float(instance_storage.n_killed) / instance_storage.n_completed * 100))


PRINTED_TEAM_NAMES = 5


def get_target_teams(args, teams, attack_no):
    if args.not_per_team:
        teams = {'*': '*'}

    if args.distribute is not None:
        k, n = args.distribute
        teams = list(filter(lambda t: binascii.crc32(t.team_ip.encode()) % n == k - 1, teams))

    if teams:
        if attack_no <= args.verbose_attacks:
            names = sorted(list(map(lambda t: t.team_name, teams)))

            if len(names) > PRINTED_TEAM_NAMES:
                names = names[:PRINTED_TEAM_NAMES] + ['...']
            logging.info('Sploit will be run on {} teams: {}'.format(len(teams), ', '.join(names)))
    else:
        logging.error('There is no teams to attack for this farm client, fix "TEAMS" value '
                      'in your server config or the usage of --distribute')

    return teams


def main(args):
    try:
        fix_args(args)
    except (ValueError, InvalidSploitError) as e:
        logging.critical(str(e))
        return

    print(highlight(HEADER))
    logging.info('Connecting to the farm server at {}'.format(args.server_url))

    threading.Thread(target=lambda: run_post_loop(args)).start()

    config = flag_format = teams = None
    pool = ThreadPoolExecutor(max_workers=args.pool_size)

    if args.endless:
        print()
        for warn in range(5):
            logging.warning("Be careful! We won't restart your sploit if it fails")
        print()

    for attack_no in once_in_a_period(args.attack_period):
        try:
            config = get_config(args)
            flag_format = re.compile(config.get('regex_flag_format'))
        except Exception as e:
            logging.error("Can't get config from the server: {}".format(repr(e)))
            if attack_no == 1:
                return
            logging.info('Using the old config')

        try:
            teams = get_teams(args)

            teams = get_target_teams(args, teams, attack_no)
            if not teams:
                if attack_no == 1:
                    return
                continue
        except Exception as e:
            logging.error("Can't get teams from the server: {}".format(repr(e)))
            if attack_no == 1:
                return
            logging.info('Using the old list of teams')

        max_runtime = None

        if not args.endless:
            max_runtime = args.attack_period / ceil(len(teams) / args.pool_size)

        if not args.endless or attack_no == 1:

            print()
            logging.info('Launching an attack #{}'.format(attack_no))

            show_time_limit_info(args, config, max_runtime, attack_no)

            for team in teams:
                pool.submit(run_sploit, args, team, attack_no, max_runtime, flag_format)


def shutdown():
    # Stop run_post_loop thread
    exit_event.set()
    # Kill all child processes (so consume_sploit_output and run_sploit also will stop)
    with instance_lock:
        for proc in instance_storage.instances.values():
            proc.kill()


if __name__ == '__main__':
    try:
        main(parse_args())
    except KeyboardInterrupt:
        logging.info('Got Ctrl+C, shutting down')
    finally:
        shutdown()
