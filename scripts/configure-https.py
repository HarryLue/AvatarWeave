#!/usr/bin/env python3
"""Obtain a free certificate, enable HTTPS alongside HTTP, and schedule renewal."""

import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

from cryptography import x509

ROOT = Path(__file__).resolve().parents[1]
ACME_ROOT = Path('/var/lib/avatarweave-acme')
CERTIFICATE_ROOT = Path('/etc/letsencrypt/live')
SITE = Path('/etc/nginx/conf.d/avatarweave-https.conf')
NGINX = '/usr/sbin/nginx'


def load_config(path):
    config = json.loads(path.read_text())
    hosts = config['hosts']
    if not isinstance(hosts, list) or not hosts:
        raise ValueError('HTTPS hosts must be a nonempty list')
    normalized = []
    for host in hosts:
        if not isinstance(host, str):
            raise ValueError('HTTPS hosts must contain IP addresses or DNS names')
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                raise ValueError('Public certificates require a public IP address')
            normalized.append(str(address))
        except ValueError:
            if not re.fullmatch(r'(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}', host):
                raise ValueError(f'Invalid public HTTPS host: {host}')
            normalized.append(host.lower())
    config['hosts'] = normalized
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', config['certificate_name']):
        raise ValueError('Invalid certificate name')
    return config


def certificate_valid(path, hosts, days=0):
    try:
        certificate = x509.load_pem_x509_certificate(path.read_bytes())
        expires = certificate.not_valid_after_utc
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        covered = set(names.get_values_for_type(x509.DNSName))
        covered.update(str(ip) for ip in names.get_values_for_type(x509.IPAddress))
        return set(hosts).issubset(covered) and expires > datetime.now(timezone.utc) + timedelta(days=days)
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False


def issue_command(config):
    command = [str(ROOT / '.tools/certbot/bin/certbot'), 'certonly',
               '--non-interactive', '--agree-tos', '--webroot', '--webroot-path', str(ACME_ROOT),
               '--cert-name', config['certificate_name'], '--keep-until-expiring', '--renew-with-new-domains']
    email = config.get('email', '')
    command += ['--email', email] if email else ['--register-unsafely-without-email']
    contains_ip = False
    for host in config['hosts']:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            command += ['--domain', host]
        else:
            command += ['--ip-address', host]
            contains_ip = True
    if contains_ip:
        command += ['--preferred-profile', 'shortlived']
    return command


def self_signed(config):
    directory = Path('/etc/avatarweave/tls')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    certificate, key = directory / 'fullchain.pem', directory / 'privkey.pem'
    if key.exists() and certificate_valid(certificate, config['hosts'], days=7):
        return certificate, key
    names = []
    for host in config['hosts']:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            names.append('DNS:' + host)
        else:
            names.append('IP:' + host)
    with tempfile.TemporaryDirectory(dir=directory) as folder:
        pending_cert, pending_key = Path(folder) / 'cert.pem', Path(folder) / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-nodes',
                        '-days', '30', '-subj', '/CN=' + config['hosts'][0],
                        '-addext', 'subjectAltName=' + ','.join(names),
                        '-keyout', str(pending_key), '-out', str(pending_cert)], check=True)
        pending_key.chmod(0o600)
        pending_cert.chmod(0o644)
        pending_key.replace(key)
        pending_cert.replace(certificate)
    return certificate, key


def reload_nginx():
    subprocess.run([NGINX, '-t'], check=True)
    subprocess.run([NGINX, '-s', 'reload'], check=True)


def enable_site(config, certificate, key):
    template = (ROOT / 'infra/nginx-tls.conf').read_text()
    text = template.replace('__TLS_NAMES__', ' '.join(config['hosts']))
    text = text.replace('__TLS_CERT__', str(certificate)).replace('__TLS_KEY__', str(key))
    previous = SITE.read_bytes() if SITE.exists() else None
    try:
        SITE.write_text(text)
        SITE.chmod(0o644)
        reload_nginx()
    except Exception:
        if previous is None:
            SITE.unlink(missing_ok=True)
        else:
            SITE.write_bytes(previous)
        raise


def schedule_renewal():
    # Running the same command also retries public issuance after a fallback.
    command = [str(ROOT / '.tools/certbot/bin/python'), str(ROOT / 'scripts/configure-https.py'), '--renew']
    if Path('/run/systemd/system').is_dir():
        unit = '[Unit]\nDescription=Renew AvatarWeave HTTPS certificate\nAfter=network-online.target nginx.service\nWants=network-online.target\n\n[Service]\nType=oneshot\nExecStart=' + ' '.join(json.dumps(part) for part in command) + '\n'
        timer = '[Unit]\nDescription=Check AvatarWeave HTTPS certificate every six hours\n\n[Timer]\nOnBootSec=10min\nOnCalendar=*-*-* 00,06,12,18:00:00\nRandomizedDelaySec=10min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n'
        Path('/etc/systemd/system/avatarweave-certificate.service').write_text(unit)
        Path('/etc/systemd/system/avatarweave-certificate.timer').write_text(timer)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', '--now', 'avatarweave-certificate.timer'], check=True)
    else:
        subprocess.run(['service', 'cron', 'start'], check=True)
        cron = Path('/etc/cron.d/avatarweave-certificate')
        cron.write_text('PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n0 */6 * * * root ' + shlex.join(command).replace('%', '\\%') + '\n')
        cron.chmod(0o644)


def configure(renew=False):
    config = load_config(ROOT / 'infra/https.json')
    ACME_ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
    directory = CERTIFICATE_ROOT / config['certificate_name']
    certificate, key = directory / 'fullchain.pem', directory / 'privkey.pem'
    try:
        result = subprocess.run(issue_command(config), timeout=300)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f'Certificate request could not complete: {error}', flush=True)
        result = subprocess.CompletedProcess([], 1)
    trusted = result.returncode == 0 and key.exists() and certificate_valid(certificate, config['hosts'])
    if not trusted:
        # A temporary CA failure must not replace a still-valid public certificate.
        trusted = key.exists() and certificate_valid(certificate, config['hosts'])
    if not trusted:
        if not config.get('fallback_self_signed', False):
            raise RuntimeError('Public certificate issuance failed; HTTP remains available')
        print('Public issuance failed. Using a self-signed fallback; browsers require explicit trust.', flush=True)
        certificate, key = self_signed(config)
    enable_site(config, certificate, key)
    if not renew:
        schedule_renewal()
    print(f'HTTPS enabled for {", ".join(config["hosts"])}; HTTP remains enabled. Certificate: {"publicly trusted" if trusted else "self-signed"}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--renew', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit('HTTPS configuration requires root/sudo.')
    configure(args.renew)
