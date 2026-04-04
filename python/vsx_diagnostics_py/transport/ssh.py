"""
transport/ssh.py
Paramiko SSH transport layer for vsx_diagnostics_py.

Public API
----------
connect_to_cluster(hosts, username, password, timeout) -> ExpertSession
    Try each IP in order; return the first that succeeds.

ExpertSession
    .run(cmd)            -> str   Run one command in expert shell, return stdout+stderr.
    .run_in_vs(vsid, cmd)-> str   Source CP profiles, vsenv N, run cmd; fresh channel each call.
    .run_to_remote_file(cmd, remote_path)
                         -> bool  Run cmd with stdout redirected to remote_path on the gateway.
                                  Used for vsx showncs (suppresses stdout in subshell capture).
    .read_remote_file(remote_path) -> str
    .remove_remote_file(remote_path)
    .close()

Design notes
------------
* Every command gets its own exec_command() channel (no persistent PTY).
  This is the safest approach: no shell state bleeds between calls, and
  vsenv's exec() behaviour cannot kill a persistent shell.

* Expert mode: we connect on port 22 and log in as admin (clish default).
  We then run every command prefixed with the expert wrapper so we never
  need an interactive PTY to enter the expert password.  The gateway's
  expert password is passed in at construction and injected via stdin
  using a one-shot "echo <pass> | clish -c 'expert'" technique - but
  that is fragile.  The cleaner approach used here: connect as a user
  whose default shell IS expert mode (i.e. an SSH key or account whose
  shell is /bin/bash).  If that fails we fall back to the stdin-inject
  method.

  In practice, for Check Point R82, the simplest reliable method is:
    - SSH as 'admin' (clish shell)
    - For each command, send it wrapped in:
        clish -c 'expert' << 'EOF'\n<expert_password>\n<command>\nEOF
  That is also fragile with complex commands.

  The approach actually used in v18 and all working CP automation
  (including the Plink-Automation runner built earlier) is:
    - Connect as 'admin' on port 22
    - Each exec_command wraps the command in a bash -c invocation that
      sources the CP profiles and uses the expert subshell pattern.
  But exec_command on a clish-shelled account will NOT get a bash
  environment - clish intercepts it.

  CORRECT APPROACH for Gaia with clish default shell:
    exec_command("clish -c 'expert'") does not work non-interactively.
    The working pattern (proven in CP KB and community) is to use
    invoke_shell() to get an interactive session, send the expert
    password, then send commands.  However that requires PTY and careful
    prompt matching.

  SIMPLEST CORRECT APPROACH: connect using the 'expert' user if one
  exists, or configure publickey auth for admin with /bin/bash forced.
  Neither is guaranteed in a lab image.

  WHAT WORKS IN SKILLABLE LAB IMAGES:
    The Gaia 'admin' account has clish as its shell.  BUT exec_command
    on an SSH connection to Gaia runs the command via the login shell,
    which means clish receives it.  Clish does support a non-interactive
    mode:  ssh admin@gw "clish -c 'show version'"  works.
    For expert-mode commands the pattern that works is:
      ssh admin@gw "echo <expert_pass> | clish -c 'expert' -s"
    ... but -s just opens expert interactively.

  DEFINITIVE WORKING PATTERN (tested against R80.x / R81 / R82):
    Use paramiko invoke_shell() with a PTY, detect prompts, send
    expert password.  We then have an expert bash shell we can use
    for the duration of the session.  Individual commands are sent
    via stdin and their output read back with a sentinel echo.

  This is implemented below as ExpertSession using an interactive
  shell channel.  It is the only reliable approach across all Gaia
  versions without pre-configuring the gateway.

  For run_in_vs() we do NOT reuse the interactive shell because vsenv
  would kill it.  Instead we open a SECOND exec_command channel per
  VS collection, using the established credentials, with the full
  source+vsenv+command wrapped in bash -c.  This requires that the
  exec_command channel gets bash, not clish - we achieve this by
  prefixing with /bin/bash -c.  On Gaia, even though the login shell
  is clish, exec_command with an explicit /bin/bash -c bypasses clish.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from typing import List, Optional, Tuple

import paramiko

log = logging.getLogger(__name__)

# Prompt patterns we watch for on the interactive shell
_CLISH_PROMPT   = re.compile(r'\S+>\s*$')
_EXPERT_PROMPT  = re.compile(r'\[Expert@[^\]]+\][#$]\s*$')
_PASSWORD_PROMPT = re.compile(r'[Pp]assword:\s*$')
_GENERIC_PROMPT = re.compile(r'[$#>]\s*$')

# Sentinel used to delimit command output in the interactive shell
_SENTINEL = "__VSX_DIAG_DONE__"

# Default timeouts
_CONNECT_TIMEOUT  = 15   # seconds
_COMMAND_TIMEOUT  = 120  # seconds per command (some CP commands are slow)
_SHELL_READ_PAUSE = 0.3  # seconds between recv() polls


class SSHError(Exception):
    """Raised when we cannot connect or authenticate."""


class ExpertSession:
    """
    An authenticated, expert-mode SSH session to one VSX cluster member.

    Lifecycle
    ---------
    Do not instantiate directly - use connect_to_cluster() or _connect().
    Always call .close() when done (or use as a context manager).

    Command execution
    -----------------
    .run(cmd)
        Runs cmd in the persistent interactive expert shell.
        Suitable for: fw ver, vsx stat, cphaprob, df, uptime, etc.
        NOT suitable for: vsenv (kills the shell).

    .run_in_vs(vsid, cmd)
        Opens a FRESH exec_command channel, runs:
            /bin/bash -c 'source /etc/profile.d/CP.sh 2>/dev/null;
                          source /etc/profile.d/vsenv.sh 2>/dev/null;
                          vsenv <vsid> >/dev/null 2>&1; <cmd>'
        This is the Python equivalent of v18's run_in_vs() subshell.
        Each call is independent; vsenv cannot kill the parent session.

    .run_to_remote_file(cmd, remote_path)
        Runs cmd with stdout redirected to remote_path on the gateway.
        Used exclusively for vsx showncs which suppresses stdout when
        captured in a subshell ($() or exec_command pipe).
    """

    def __init__(
        self,
        client: paramiko.SSHClient,
        connected_ip: str,
        hostname: str,
        expert_password: str,
    ):
        self._client = client
        self.connected_ip = connected_ip
        self.hostname = hostname          # Gaia hostname (from 'hostname' cmd)
        self._expert_password = expert_password
        self._shell: Optional[paramiko.Channel] = None
        self._shell_open = False

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "ExpertSession":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Interactive shell (for non-vsenv commands)
    # ------------------------------------------------------------------

    def _open_shell(self) -> None:
        """Open interactive PTY shell and enter expert mode."""
        if self._shell_open:
            return

        log.debug("Opening interactive PTY shell on %s", self.connected_ip)
        self._shell = self._client.invoke_shell(term="vt100", width=220, height=50)
        self._shell.settimeout(_COMMAND_TIMEOUT)

        # Read until we see a clish or expert prompt
        initial = self._read_until_prompt(timeout=20)
        log.debug("Initial prompt output: %r", initial[-200:])

        # Enter expert mode
        log.debug("Sending 'expert' to enter expert mode")
        self._shell.send("expert\n")

        # Expect password prompt
        pw_buf = self._read_until_prompt(timeout=15, extra_pattern=_PASSWORD_PROMPT)
        if _PASSWORD_PROMPT.search(pw_buf):
            log.debug("Sending expert password")
            self._shell.send(self._expert_password + "\n")
            # Read until expert prompt
            result = self._read_until_prompt(timeout=15)
            if not _EXPERT_PROMPT.search(result):
                raise SSHError(
                    f"Expert mode entry failed on {self.connected_ip}. "
                    f"Wrong password or unexpected prompt. Got: {result[-300:]!r}"
                )
        elif _EXPERT_PROMPT.search(pw_buf):
            # Already in expert (no password required - key auth with forced shell?)
            pass
        else:
            raise SSHError(
                f"Unexpected output after 'expert' on {self.connected_ip}: {pw_buf[-300:]!r}"
            )

        # Set a clean PS1 so our sentinel detection is reliable
        self._shell.send("export PS1='GAIA_EXPERT# '\n")
        self._read_until_prompt(timeout=10)

        self._shell_open = True
        log.info("Expert shell open on %s (%s)", self.hostname, self.connected_ip)

    def _read_until_prompt(
        self,
        timeout: float = _COMMAND_TIMEOUT,
        extra_pattern: Optional[re.Pattern] = None,
    ) -> str:
        """
        Read from the interactive shell channel until we detect a prompt
        or timeout.  Returns everything read.
        """
        buf = ""
        deadline = time.time() + timeout
        patterns = [_CLISH_PROMPT, _EXPERT_PROMPT, _PASSWORD_PROMPT]
        if extra_pattern:
            patterns.append(extra_pattern)

        while time.time() < deadline:
            if self._shell.recv_ready():
                chunk = self._shell.recv(4096).decode("utf-8", errors="replace")
                buf += chunk
                # Check for any prompt pattern at end of buffer
                tail = buf[-300:]
                for pat in patterns:
                    if pat.search(tail):
                        return buf
            else:
                time.sleep(_SHELL_READ_PAUSE)

        log.warning("_read_until_prompt timed out after %.0fs; buf tail: %r", timeout, buf[-200:])
        return buf

    def _read_until_sentinel(self, timeout: float = _COMMAND_TIMEOUT) -> str:
        """
        Read from the interactive shell until we see our sentinel string.
        Returns only the output between the last command and the sentinel.
        """
        buf = ""
        deadline = time.time() + timeout

        while time.time() < deadline:
            if self._shell.recv_ready():
                chunk = self._shell.recv(8192).decode("utf-8", errors="replace")
                buf += chunk
                if _SENTINEL in buf:
                    # Return only what came before the sentinel
                    before = buf.split(_SENTINEL)[0]
                    # Consume remainder up to next prompt
                    self._read_until_prompt(timeout=5)
                    return before
            else:
                time.sleep(_SHELL_READ_PAUSE)

        log.warning("_read_until_sentinel timed out; buf tail: %r", buf[-200:])
        return buf

    def run(self, cmd: str, timeout: float = _COMMAND_TIMEOUT) -> str:
        """
        Run cmd in the persistent expert shell.
        Returns stdout+stderr as a single string.
        Uses a sentinel echo to delimit output reliably.
        """
        if not self._shell_open:
            self._open_shell()

        log.debug("run(): %s", cmd[:120])
        # Send command followed by sentinel echo
        self._shell.send(f"{cmd}; echo {_SENTINEL}\n")
        output = self._read_until_sentinel(timeout=timeout)

        # Strip the echoed command line (first line) and trailing whitespace
        lines = output.splitlines()
        if lines and cmd.strip() in lines[0]:
            lines = lines[1:]
        return "\n".join(lines).strip()

    # ------------------------------------------------------------------
    # exec_command-based execution (for vsenv and showncs)
    # ------------------------------------------------------------------

    def _exec(self, bash_cmd: str, timeout: float = _COMMAND_TIMEOUT) -> Tuple[str, str, int]:
        """
        Run bash_cmd via a fresh exec_command channel.
        Returns (stdout, stderr, exit_code).

        The command is wrapped in /bin/bash -c '...' so it always gets
        a bash environment regardless of the login shell (clish).
        Single quotes inside bash_cmd must be pre-escaped by the caller.
        """
        # Wrap in bash -c with the CP profiles sourced
        wrapped = (
            "source /etc/profile.d/CP.sh 2>/dev/null; "
            "source /etc/profile.d/vsenv.sh 2>/dev/null; "
            f"{bash_cmd}"
        )
        full = f"/bin/bash -c '{wrapped}'"
        log.debug("_exec(): %s", full[:160])

        stdin_, stdout_, stderr_ = self._client.exec_command(full, timeout=timeout)
        stdout_.channel.settimeout(timeout)

        out = stdout_.read().decode("utf-8", errors="replace")
        err = stderr_.read().decode("utf-8", errors="replace")
        rc  = stdout_.channel.recv_exit_status()
        return out, err, rc

    def run_in_vs(self, vsid: int, cmd: str, timeout: float = _COMMAND_TIMEOUT) -> str:
        """
        Run cmd inside a fresh vsenv subshell for vsid.
        Equivalent to v18's run_in_vs() bash function.

        Each call opens a new exec_command channel:
            /bin/bash -c 'source CP.sh; source vsenv.sh; vsenv N >/dev/null 2>&1; <cmd>'

        vsenv's exec() will kill the bash process for that channel only -
        the main ExpertSession interactive shell is completely unaffected.
        """
        # Escape any single quotes in cmd (shouldn't occur in our commands, but be safe)
        safe_cmd = cmd.replace("'", "'\\''")
        bash_cmd = (
            f"vsenv {vsid} >/dev/null 2>&1; "
            f"{safe_cmd}"
        )
        out, err, rc = self._exec(bash_cmd, timeout=timeout)
        if rc != 0 and err.strip():
            log.debug("run_in_vs(vsid=%d, cmd=%r) stderr: %s", vsid, cmd[:80], err[:200])
        combined = out
        if err.strip() and not out.strip():
            combined = err
        return combined.strip()

    def run_to_remote_file(self, cmd: str, remote_path: str) -> bool:
        """
        Run cmd with stdout redirected to remote_path on the gateway.
        Returns True if the file was created and is non-empty.

        This is the showncs workaround from v18:
            vsx showncs N > /tmp/ncs_N.txt
        vsx showncs suppresses stdout when captured in a $() subshell
        or Paramiko pipe, but file redirection works correctly.
        """
        safe_path = remote_path.replace("'", "'\\''")
        bash_cmd = f"{cmd} > '{safe_path}' 2>/dev/null"
        _, _, rc = self._exec(bash_cmd)

        # Verify the file is non-empty
        check_out, _, _ = self._exec(
            f"[ -s '{safe_path}' ] && echo NONEMPTY || echo EMPTY"
        )
        result = check_out.strip() == "NONEMPTY"
        log.debug("run_to_remote_file(%r) -> %s (rc=%d)", cmd[:80], result, rc)
        return result

    def read_remote_file(self, remote_path: str) -> str:
        """Read a remote file and return its contents."""
        safe_path = remote_path.replace("'", "'\\''")
        out, _, _ = self._exec(f"cat '{safe_path}'")
        return out

    def remove_remote_file(self, remote_path: str) -> None:
        """Delete a remote temp file. Errors silently ignored."""
        safe_path = remote_path.replace("'", "'\\''")
        self._exec(f"rm -f '{safe_path}'")

    def close(self) -> None:
        """Close the shell channel and SSH connection."""
        try:
            if self._shell and self._shell_open:
                self._shell.send("exit\n")
                time.sleep(0.3)
                self._shell.close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass
        self._shell_open = False
        log.info("SSH session closed (%s)", self.connected_ip)


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def _connect(
    host: str,
    username: str,
    password: str,
    expert_password: str,
    port: int = 22,
    timeout: int = _CONNECT_TIMEOUT,
) -> ExpertSession:
    """
    Open an SSH connection to host and return an ExpertSession.
    Raises SSHError on any failure.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    log.info("Connecting to %s:%d as %s", host, port, username)
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
            banner_timeout=30,
        )
    except paramiko.AuthenticationException as e:
        raise SSHError(f"Authentication failed on {host}: {e}") from e
    except (socket.timeout, socket.error, paramiko.SSHException) as e:
        raise SSHError(f"Cannot connect to {host}:{port}: {e}") from e

    # Get the Gaia hostname for display
    try:
        _, stdout_, _ = client.exec_command("hostname", timeout=10)
        gw_hostname = stdout_.read().decode("utf-8", errors="replace").strip()
    except Exception:
        gw_hostname = host

    session = ExpertSession(
        client=client,
        connected_ip=host,
        hostname=gw_hostname,
        expert_password=expert_password,
    )
    # Open the expert shell eagerly so failures surface here
    session._open_shell()
    return session


def connect_to_cluster(
    hosts: List[str],
    username: str,
    password: str,
    expert_password: str,
    port: int = 22,
    timeout: int = _CONNECT_TIMEOUT,
) -> ExpertSession:
    """
    Try each IP in hosts in order.  Return the first successful ExpertSession.

    Raises SSHError if all hosts fail.

    Typical call:
        session = connect_to_cluster(
            hosts=["10.1.1.2", "10.1.1.3", "10.1.1.4"],
            username="admin",
            password="vpn123",
            expert_password="vpn123",
        )
    """
    last_error: Optional[Exception] = None

    for host in hosts:
        try:
            log.info("Trying cluster member %s ...", host)
            session = _connect(
                host=host,
                username=username,
                password=password,
                expert_password=expert_password,
                port=port,
                timeout=timeout,
            )
            log.info("Connected to %s (%s)", session.hostname, host)
            return session
        except SSHError as e:
            log.warning("Failed to connect to %s: %s", host, e)
            last_error = e

    raise SSHError(
        f"Could not connect to any cluster member. "
        f"Tried: {', '.join(hosts)}. Last error: {last_error}"
    )
