"""VCAP_SERVICES credential lookup + paramiko SSH session to Ops Manager.

Only talks to the OM VM over SSH and runs read-only `bosh` commands there via
a login shell (`bash -lc 'bosh ...'`). Never shells out to a local `bosh`
binary -- it will not exist in the CF container. Never logs or returns the
private key.
"""

import io
import json
import logging
import os
import shlex
import socket
import threading
import time

import paramiko

logging.getLogger("paramiko").setLevel(logging.CRITICAL)

SERVICE_NAME = "ops-manager-ssh"


class CredsError(Exception):
    pass


def load_creds_from_vcap(vcap_services_json=None):
    """Find ops-manager-ssh credentials in VCAP_SERVICES.

    Lookup order: user-provided service named `ops-manager-ssh`, else any
    user-provided service whose credentials contain `private_key` plus
    `host` or `ip`. Returns the credentials dict, or None if not found.
    """
    raw = vcap_services_json if vcap_services_json is not None else os.environ.get("VCAP_SERVICES")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    candidates = []
    for services in data.values():
        if isinstance(services, list):
            candidates.extend(services)

    fallback = None
    for svc in candidates:
        if not isinstance(svc, dict):
            continue
        creds = svc.get("credentials") or {}
        if not isinstance(creds, dict):
            continue
        if svc.get("name") == SERVICE_NAME and "private_key" in creds:
            return creds
        if fallback is None and "private_key" in creds and ("host" in creds or "ip" in creds):
            fallback = creds
    return fallback


def _load_private_key(private_key_str):
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key(io.StringIO(private_key_str))
        except paramiko.SSHException:
            continue
    raise CredsError("could not load private_key with any supported key type")


class OmSSH:
    """A reconnecting SSH session to the Ops Manager VM."""

    KEEPALIVE_SECONDS = 30
    CONNECT_TIMEOUT = 10

    def __init__(self, creds):
        self.creds = creds
        self.user = creds.get("user")
        self.port = int(creds.get("port") or 22)
        self._hosts = [h for h in (creds.get("host"), creds.get("ip")) if h]
        self._client = None
        self._host_used = None
        self._lock = threading.RLock()
        self.last_error = None

    @property
    def host(self):
        return self._host_used or (self._hosts[0] if self._hosts else None)

    @property
    def connected(self):
        with self._lock:
            transport = self._client.get_transport() if self._client else None
            return bool(transport and transport.is_active())

    def connect(self):
        with self._lock:
            key = _load_private_key(self.creds["private_key"])
            last_exc = None
            for host in self._hosts:
                sock = None
                try:
                    # Pre-open TCP then hand the socket to paramiko. Passing
                    # timeout= on connect() alone makes paramiko's banner
                    # reader hit EOF against this OM (raw nc still works).
                    sock = socket.create_connection((host, self.port), timeout=self.CONNECT_TIMEOUT)
                    sock.settimeout(None)
                    time.sleep(0.15)
                    client = paramiko.SSHClient()
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    client.connect(
                        hostname=host,
                        port=self.port,
                        username=self.user,
                        pkey=key,
                        sock=sock,
                        timeout=30,
                        banner_timeout=30,
                        auth_timeout=30,
                        look_for_keys=False,
                        allow_agent=False,
                        gss_auth=False,
                        gss_kex=False,
                    )
                    transport = client.get_transport()
                    transport.set_keepalive(self.KEEPALIVE_SECONDS)
                    self._client = client
                    self._host_used = host
                    self.last_error = None
                    return
                except Exception as exc:  # noqa: BLE001 - want to try all hosts
                    last_exc = exc
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:  # noqa: BLE001
                            pass
                    continue
            self._client = None
            self.last_error = str(last_exc) if last_exc else "no host reachable"
            raise ConnectionError(self.last_error)

    def ensure_connected(self):
        if self.connected:
            return
        self.connect()

    def exec_bosh(self, args, timeout=60):
        """Run `bosh <args>` on the OM VM and return (status, stdout, stderr)."""
        return self._exec(f"bosh {args}", timeout=timeout)

    def _exec(self, bosh_cmd, timeout=60):
        self.ensure_connected()
        with self._lock:
            client = self._client
        cmd = "bash -lc " + shlex.quote(bosh_cmd)
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        status = stdout.channel.recv_exit_status()
        if status != 0 and "not found" in (err + out).lower():
            cmd2 = "bash -lc " + shlex.quote(f"source /home/ubuntu/.profile; {bosh_cmd}")
            _stdin, stdout, stderr = client.exec_command(cmd2, timeout=timeout)
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
            status = stdout.channel.recv_exit_status()
        return status, out, err

    def stream_bosh_lines(self, args, timeout=None):
        """Run `bosh <args>` and yield stdout lines as they arrive.

        Blocks until the remote command exits -- for a processing task this
        follows until the task ends (bosh task's own follow behavior); for a
        finished task it just returns the full log once.
        """
        self.ensure_connected()
        with self._lock:
            client = self._client
        cmd = "bash -lc " + shlex.quote(f"bosh {args}")
        _stdin, stdout, _stderr = client.exec_command(cmd, timeout=timeout)
        for raw in iter(stdout.readline, ""):
            if raw == "":
                break
            yield raw.rstrip("\n")

    def close(self):
        with self._lock:
            if self._client:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
            self._client = None
