import argparse
import csv
import os
import re
import requests
import sys
import yaml
from datetime import datetime

# Connection settings for the selected PCE, filled in by configure_pce().
api_key = None
api_secret = None
base_url = None
cert_path = None
auth = None
org = None

# The keys that make up one PCE entry in config.yaml.
PCE_FIELDS = ('api_key', 'api_secret', 'pce_fqdn', 'port', 'cert_path')

# org is optional; PCEs are single-org by default.
DEFAULT_ORG = 1

# config.yaml carries only the PCE's hostname and port; the scheme and API path
# are fixed here so they don't have to be repeated for every PCE.
API_SCHEME = 'https'
API_PATH = '/api/v2'


def build_base_url(pce_fqdn, port):
    """Turn pce_fqdn and port from config.yaml into the full API base URL."""
    host = str(pce_fqdn).strip()
    if '://' in host:
        host = host.split('://', 1)[1]
    host = host.strip('/')
    host = re.sub(r'/api/v\d+$', '', host)
    # Tolerate the port being written into pce_fqdn as well.
    host, _, embedded_port = host.partition(':')
    if not host:
        raise ValueError('pce_fqdn is empty: expected a hostname')

    port = str(port).strip() if port is not None else ''
    if not port:
        port = embedded_port
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"port must be a number between 1 and 65535, got: {port or '<empty>'}")

    return f'{API_SCHEME}://{host}:{port}{API_PATH}'

# Preferred column order for label keys in the output table; any other key found
# on a workload is appended after these, alphabetically.
LABEL_KEY_ORDER = ('role', 'app', 'env', 'loc', 'pt')

# Name of the PCE in use, shown in the report header.
PCE_NAME = None


def read_pce_config(config_path='config.yaml'):
    """Return (pces, default_name) from config.yaml.

    Each top-level block is one named PCE, with an optional default:

        default_pce: alpha

        alpha:
          api_key: ...
          api_secret: ...
          pce_fqdn: ...
          port: ...
          cert_path: ...
          org: 1        # optional, defaults to 1

        prod:
          api_key: ...

    A single PCE may also be written flat, with the connection fields at the
    top level and no name.
    """
    with open(config_path) as f:
        document = yaml.safe_load(f) or {}

    if not isinstance(document, dict) or not document:
        raise ValueError(f"{config_path} is empty or not a YAML mapping")

    default_name = document.get('default_pce')

    # Connection fields at the top level are one unnamed PCE.
    if any(field in document for field in PCE_FIELDS):
        return {'default': document}, 'default'

    pces = {
        str(name): entry
        for name, entry in document.items()
        if name != 'default_pce' and isinstance(entry, dict)
    }
    if not pces:
        raise ValueError(
            f"no PCEs found in {config_path}: it must hold either the "
            f"connection fields directly, or one block per named PCE"
        )

    return pces, default_name


def configure_pce(name=None, config_path='config.yaml'):
    """Select a PCE from config.yaml and set the module-level connection globals."""
    global api_key, api_secret, base_url, cert_path, auth, org, PCE_NAME

    pces, default_name = read_pce_config(config_path)
    available = ', '.join(sorted(pces))

    if name is None:
        if default_name is not None:
            if default_name not in pces:
                raise ValueError(
                    f"default_pce '{default_name}' is not defined in {config_path}. "
                    f"Available: {available}"
                )
            name = default_name
        elif len(pces) == 1:
            name = next(iter(pces))
        else:
            raise ValueError(
                f"{config_path} defines several PCEs, so --pce is required. "
                f"Available: {available}"
            )
    elif name not in pces:
        raise ValueError(f"unknown PCE '{name}'. Available: {available}")

    config = dict(pces[name])

    # Older configs carried a single base_url; split it into the new fields.
    if not config.get('pce_fqdn') and config.get('base_url'):
        legacy = str(config['base_url'])
        if '://' in legacy:
            legacy = legacy.split('://', 1)[1]
        legacy = re.sub(r'/api/v\d+/?$', '', legacy.strip('/'))
        host, _, legacy_port = legacy.partition(':')
        config.setdefault('pce_fqdn', host)
        if legacy_port and not config.get('port'):
            config['port'] = legacy_port

    missing = [field for field in PCE_FIELDS if not config.get(field)]
    if missing:
        raise ValueError(
            f"PCE '{name}' in {config_path} is missing: {', '.join(missing)}"
        )

    org_value = config.get('org', DEFAULT_ORG)
    if not str(org_value).strip().isdigit() or int(org_value) < 1:
        raise ValueError(
            f"org for PCE '{name}' must be a positive number, got: {org_value}"
        )

    api_key = config['api_key']
    api_secret = config['api_secret']
    base_url = build_base_url(config['pce_fqdn'], config['port'])
    cert_path = config['cert_path']
    auth = (api_key, api_secret)
    org = int(org_value)
    PCE_NAME = name

    return name


# The PCE caps an unqualified GET /workloads at a few hundred results, which
# would silently hide workloads on a large estate, so ask for a high ceiling and
# check the total the PCE reports back.
WORKLOAD_FETCH_LIMIT = 100000

# Enforcement modes the PCE accepts, loosest to strictest.
ENFORCEMENT_MODES = ('idle', 'visibility_only', 'selective', 'full')

# The PCE accepts at most 1000 workloads per bulk_update call.
BULK_UPDATE_LIMIT = 1000

# Long listings are truncated to this many lines; the tables above them already
# carry the detail.
MAX_LISTED = 20


def fetch_workloads():
    workloads_url = f'{base_url}/orgs/{org}/workloads'
    response = requests.get(
        workloads_url,
        auth=auth,
        verify=cert_path,
        params={'max_results': WORKLOAD_FETCH_LIMIT},
    )

    if response.status_code != 200:
        print(f"Failed to fetch workloads: {response.status_code}")
        print(response.text)
        raise SystemExit(1)

    workloads = response.json()

    # X-Total-Count is what the PCE holds; a smaller list means it truncated.
    total = response.headers.get('X-Total-Count')
    if total and total.isdigit() and int(total) > len(workloads):
        print(f"Warning: the PCE reports {total} workloads but returned "
              f"{len(workloads)}. Raise WORKLOAD_FETCH_LIMIT "
              f"(currently {WORKLOAD_FETCH_LIMIT}).")

    return workloads


_LABELS_CACHE = None


def fetch_labels():
    global _LABELS_CACHE
    if _LABELS_CACHE is not None:
        return _LABELS_CACHE

    labels_url = f'{base_url}/orgs/{org}/labels'
    response = requests.get(labels_url, auth=auth, verify=cert_path)

    if response.status_code != 200:
        print(f"Failed to fetch labels: {response.status_code}")
        print(response.text)
        raise SystemExit(1)

    _LABELS_CACHE = response.json()
    return _LABELS_CACHE


def label_dict(workload):
    return {
        label.get('key'): label.get('value')
        for label in workload.get('labels', [])
    }


def format_labels(labels):
    return ', '.join(f"{k}:{v}" for k, v in labels.items()) or '-'


def print_table(headers, rows):
    widths = [
        max(len(str(row[i])) for row in ([headers] + rows))
        for i in range(len(headers))
    ]

    def print_row(row):
        print('  '.join(str(val).ljust(width) for val, width in zip(row, widths)))

    print_row(headers)
    print_row(['-' * w for w in widths])
    for row in rows:
        print_row(row)


def label_key_columns(workloads):
    keys = set()
    for workload in workloads:
        keys.update(label_dict(workload))

    ordered = [key for key in LABEL_KEY_ORDER if key in keys]
    ordered += sorted(key for key in keys if key not in LABEL_KEY_ORDER)
    return ordered


def backup_filename(prefix='quarantine-backup'):
    # e.g. quarantine-backup.18092026.1430.csv
    # The stamp is minute-granular, so two runs in the same minute would land on
    # the same name; suffix -1, -2, ... rather than overwrite an earlier backup.
    stamp = datetime.now().strftime('%d%m%Y.%H%M')
    candidate = f"{prefix}.{stamp}.csv"
    counter = 1
    while os.path.exists(candidate):
        candidate = f"{prefix}.{stamp}-{counter}.csv"
        counter += 1
    return candidate


def write_labels_backup(workloads, csv_path=None):
    # Writes the targeted workloads' current labels as hostname plus one column
    # per label key, recording the exact state before the change. The hostname
    # column means the file can also be fed back in as --workloads to target the
    # same hosts again.
    csv_path = csv_path or backup_filename()
    columns = label_key_columns(workloads)

    named_workloads = [w for w in workloads if w.get('hostname')]

    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(
            f, fieldnames=['hostname'] + columns + ['enforcement_mode'])
        writer.writeheader()
        for workload in sorted(named_workloads, key=lambda w: w['hostname']):
            labels = label_dict(workload)
            mode, _ = enforcement_info(workload)
            row = {'hostname': workload['hostname'], 'enforcement_mode': mode}
            row.update({key: labels.get(key, '') for key in columns})
            writer.writerow(row)

    print(f"Backed up current labels for {len(named_workloads)} workload(s) "
          f"to {csv_path}")
    print(f"  Records the labels as they were, and can be passed back in "
          f"as --workloads to target the same hosts.")

    return csv_path


# Columns in a workloads file that are not label keys.
HOSTNAME_COLUMN = 'hostname'
ENFORCEMENT_COLUMNS = ('enforcement_mode', 'enforcement')


def read_workload_targets(csv_path):
    """Read the workloads file into one target per host.

    Accepts a plain one-hostname-per-line list (with or without a leading
    "- " bullet), or a CSV with a "hostname" column. In the CSV form every
    other column is taken as a label key and its cell as that label's value,
    so a backup file fed back in carries its labels and enforcement mode with
    it:

        hostname,role,app,env,loc,pt,enforcement_mode
        sarl8wkld1,R-Container,A-Podman,E-Prod,L-NY,P-VEN,visibility_only

    An "enforcement_mode" column sets that host's enforcement mode. An empty
    cell carries no instruction, so that key is left as it stands on the
    workload. Blank lines and "#" comments are ignored, and a repeated
    hostname keeps its first row.

    Returns a list of {'hostname', 'labels', 'enforcement'} dicts, in file
    order.
    """
    targets = []
    seen = set()

    with open(csv_path, newline='') as f:
        rows = list(csv.reader(f))

    if not rows:
        return targets

    header = [cell.strip() for cell in rows[0]]
    lowered = [cell.lower() for cell in header]

    if HOSTNAME_COLUMN in lowered:
        host_index = lowered.index(HOSTNAME_COLUMN)
        data_rows = rows[1:]
        first_line = 2
    else:
        # A plain hostname list: no header, so no labels to read either.
        host_index = 0
        header = []
        lowered = []
        data_rows = rows
        first_line = 1

    label_columns = [
        (index, name)
        for index, name in enumerate(header)
        if index != host_index and name
        and lowered[index] not in ENFORCEMENT_COLUMNS
    ]
    enforcement_index = next(
        (index for index, name in enumerate(lowered)
         if name in ENFORCEMENT_COLUMNS),
        None,
    )

    for number, row in enumerate(data_rows, start=first_line):
        if len(row) <= host_index:
            continue
        hostname = row[host_index].strip()
        if hostname.startswith('- '):
            hostname = hostname[2:].strip()
        if not hostname or hostname.startswith('#'):
            continue
        if hostname in seen:
            continue
        seen.add(hostname)

        labels = {}
        for index, key in label_columns:
            value = row[index].strip() if len(row) > index else ''
            if value:
                labels[key] = value

        enforcement = None
        if enforcement_index is not None and len(row) > enforcement_index:
            enforcement = row[enforcement_index].strip().lower() or None
            if enforcement and enforcement not in ENFORCEMENT_MODES:
                raise ValueError(
                    f"{csv_path} line {number}: unknown enforcement mode "
                    f"'{enforcement}' for {hostname}. Expected one of: "
                    f"{', '.join(ENFORCEMENT_MODES)}"
                )

        targets.append({
            'hostname': hostname,
            'labels': labels,
            'enforcement': enforcement,
        })

    return targets


def validate_target_labels(targets, parser):
    """Check the workloads file's key:value pairs against the PCE's labels.

    The file names both the key (column) and the value (cell), so unlike
    --label there is nothing to resolve; the pair just has to exist, since the
    script never creates labels.
    """
    known = {(label.get('key'), label.get('value')) for label in fetch_labels()}

    unknown = []
    for target in targets:
        for pair in target['labels'].items():
            if pair not in known and pair not in unknown:
                unknown.append(pair)

    if unknown:
        parser.error(
            'the workloads file has labels that do not exist in the PCE: '
            + ', '.join(f'{key}:{value}' for key, value in unknown)
            + '. Every column other than hostname and enforcement_mode is '
              'read as a label key.'
        )


def parse_label_args(values, parser):
    """Split --label arguments into a list of label values.

    Values only, no key: the key is looked up on the PCE by resolve_label_values.
    Repeated and/or comma-separated, e.g.

        --label Q-Level-1 --label E-Dev
        --label Q-Level-1,E-Dev
    """
    parsed = []
    for value in values:
        for item in value.split(','):
            item = item.strip()
            if not item:
                continue
            if item not in parsed:
                parsed.append(item)
    return parsed


def resolve_label_values(values, parser):
    """Map each --label value to its key using the labels defined on the PCE."""
    if not values:
        return {}

    keys_by_value = {}
    for label in fetch_labels():
        keys_by_value.setdefault(label.get('value'), set()).add(label.get('key'))

    labels = {}
    for value in values:
        keys = sorted(keys_by_value.get(value, ()))

        if not keys:
            hint = ''
            # A key:value argument is the old syntax; point at the new one.
            if ':' in value:
                _, _, bare = value.partition(':')
                if bare in keys_by_value:
                    hint = f". Pass the value on its own: --label {bare}"
            parser.error(f"no label with value '{value}' exists in the PCE{hint}")

        if len(keys) > 1:
            parser.error(
                f"label value '{value}' is ambiguous, it exists under keys: "
                f"{', '.join(keys)}"
            )

        key = keys[0]
        if key in labels and labels[key] != value:
            parser.error(
                f"--label given two values for key '{key}': "
                f"{labels[key]} and {value}. A workload can only hold "
                f"one label per key."
            )
        labels[key] = value

    return labels


def enforcement_info(workload):
    # enforcement_mode is the current API field; older PCE versions expose the
    # same idea as "mode", so fall back to that.
    mode = workload.get('enforcement_mode') or workload.get('mode') or '-'
    visibility = workload.get('visibility_level') or '-'
    return mode, visibility


def online_status(workload):
    agent = workload.get('agent') or {}
    status = agent.get('status') or {}
    if workload.get('online') is True:
        return 'online'
    if workload.get('online') is False:
        return 'offline'
    return status.get('status') or '-'


def match_workloads(workloads, hostnames):
    workload_by_host = {
        workload.get('hostname'): workload
        for workload in workloads
        if workload.get('hostname')
    }
    found = [(h, workload_by_host[h]) for h in hostnames if h in workload_by_host]
    missing_hosts = [h for h in hostnames if h not in workload_by_host]
    return found, missing_hosts


def print_missing(missing_hosts):
    if missing_hosts:
        print("\nHostnames in CSV not found in PCE:")
        for hostname in missing_hosts:
            print(f"  {hostname}")


def report(found, missing_hosts, total, targets=None):
    targets = targets or {}
    if found:
        columns = label_key_columns([w for _, w in found])
        headers = (
            ['Hostname']
            + [key.upper() for key in columns]
            + ['Enforcement', 'Visibility', 'Status']
        )

        # The workloads file may name labels and an enforcement mode of its
        # own; show them beside the workload's current ones, without acting
        # on them.
        file_labels = any(
            (targets.get(hostname) or {}).get('labels') for hostname, _ in found)
        file_modes = any(
            (targets.get(hostname) or {}).get('enforcement')
            for hostname, _ in found)
        if file_labels:
            headers.append('From File')
        if file_modes:
            headers.append('File Enforcement')

        rows = []
        for hostname, workload in found:
            labels = label_dict(workload)
            mode, visibility = enforcement_info(workload)
            row = (
                [hostname]
                + [labels.get(key, '-') for key in columns]
                + [mode, visibility, online_status(workload)]
            )
            if file_labels or file_modes:
                target = targets.get(hostname) or {}
                if file_labels:
                    row.append(format_labels(target.get('labels') or {}))
                if file_modes:
                    row.append(target.get('enforcement') or '-')
            rows.append(row)

        heading = f"\nWorkloads ({len(rows)} of {total} found"
        heading += f" on {PCE_NAME}):" if PCE_NAME else "):"
        print(heading)
        print_table(headers, rows)

        if file_labels or file_modes:
            shown_columns = [
                name for name, shown in
                (('From File', file_labels), ('File Enforcement', file_modes))
                if shown
            ]
            named = ' and '.join(shown_columns)
            verb = 'hold' if len(shown_columns) > 1 else 'holds'
            print(f"\n{named} {verb} what the workloads file records for each "
                  f"host, whether or not it differs from the columns to the "
                  f"left; '-' means the file says nothing for that host. "
                  f"--preview shows which of them would change, and nothing "
                  f"is written without --apply.")
    else:
        print("\nNone of the hostnames in the CSV were found in the PCE.")

    print_missing(missing_hosts)


def compute_append(found, new_labels, new_mode=None, targets=None):
    # A workload holds at most one label per key, so appending a key the
    # workload already carries replaces that key's value rather than adding a
    # second one. Those replacements are reported separately.
    #
    # targets maps hostname -> the row read from the workloads file, whose
    # labels and enforcement mode apply to that host alone. --label and
    # --enforcement apply to every host, and win where the two collide.
    targets = targets or {}
    changes = []
    replacements = []
    for hostname, workload in found:
        target = targets.get(hostname) or {}
        add_labels = dict(target.get('labels') or {})
        add_labels.update(new_labels)

        current_labels = label_dict(workload)
        desired_labels = dict(current_labels)
        desired_labels.update(add_labels)

        for key, value in add_labels.items():
            if key in current_labels and current_labels[key] != value:
                replacements.append((hostname, key, current_labels[key], value))

        current_mode, _ = enforcement_info(workload)
        desired_mode = new_mode or target.get('enforcement') or current_mode

        if current_labels != desired_labels or desired_mode != current_mode:
            changes.append({
                'hostname': hostname,
                'workload': workload,
                'current_labels': current_labels,
                'desired_labels': desired_labels,
                'current_mode': current_mode,
                'desired_mode': desired_mode,
            })

    return changes, replacements


def compute_rollback(found, remove_labels, new_mode=None, targets=None):
    """Undo a change: put back what the workloads file records, and remove the
    labels --label names.

    A file carrying label columns describes the state to go back to, so those
    labels are written back onto the workload rather than stripped from it —
    rolling back from a backup restores it. Stripping every label a backup
    lists is almost never what a rollback means, and would leave the workload
    with none at all.

    --label still names labels to remove, matching both key and value, so a
    quarantine label can be dropped in the same run that restores the rest. A
    key that has since been changed to some other value is reported and left
    alone, and where the file and --label name the same key, --label wins.
    """
    targets = targets or {}
    changes = []
    mismatches = []
    for hostname, workload in found:
        target = targets.get(hostname) or {}
        restore_labels = target.get('labels') or {}

        current_labels = label_dict(workload)
        desired_labels = dict(current_labels)
        desired_labels.update(restore_labels)

        # Removals are applied after the restore, so --label has the last word
        # on a key the file also names.
        for key, value in remove_labels.items():
            if key not in desired_labels:
                continue
            if desired_labels[key] == value:
                desired_labels.pop(key)
            else:
                mismatches.append((hostname, key, desired_labels[key], value))

        current_mode, _ = enforcement_info(workload)
        desired_mode = new_mode or target.get('enforcement') or current_mode

        if current_labels != desired_labels or desired_mode != current_mode:
            changes.append({
                'hostname': hostname,
                'workload': workload,
                'current_labels': current_labels,
                'desired_labels': desired_labels,
                'current_mode': current_mode,
                'desired_mode': desired_mode,
            })

    return changes, mismatches


def print_label_diff(changes, action):
    labels_changing = any(
        c['current_labels'] != c['desired_labels'] for c in changes)
    mode_changing = any(c['current_mode'] != c['desired_mode'] for c in changes)

    if changes and mode_changing and not labels_changing:
        heading = 'Enforcement change preview'
    else:
        heading = f'Label {action} preview'
    print(f"\n{heading}:")

    if changes:
        shown = changes[:MAX_LISTED]

        # An enforcement-only change would otherwise print the same label set
        # twice, so drop the label columns.
        if not labels_changing:
            print_table(
                ('Hostname', 'Enforcement', 'New Enforcement'),
                [(c['hostname'], c['current_mode'], c['desired_mode'])
                 for c in shown],
            )
            if len(changes) > len(shown):
                print(f"... and {len(changes) - len(shown)} more workload(s) "
                      f"with the same change")
            return

        if mode_changing:
            headers = ('Hostname', 'Current Labels', 'New Labels',
                       'Enforcement', 'New Enforcement')
            rows = [
                (c['hostname'],
                 format_labels(c['current_labels']),
                 format_labels(c['desired_labels']),
                 c['current_mode'],
                 c['desired_mode'] if c['desired_mode'] != c['current_mode'] else '-')
                for c in shown
            ]
        else:
            headers = ('Hostname', 'Current Labels', 'New Labels')
            rows = [
                (c['hostname'],
                 format_labels(c['current_labels']),
                 format_labels(c['desired_labels']))
                for c in shown
            ]
        print_table(headers, rows)
        if len(changes) > len(shown):
            print(f"... and {len(changes) - len(shown)} more workload(s) "
                  f"with the same change")
    else:
        print("No label changes detected.")


def batched(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def send_updates(resolved):
    """Apply label changes, in batches, via the PCE's bulk_update endpoint.

    resolved is a list of (change, hrefs). The PCE takes at most
    BULK_UPDATE_LIMIT workloads per call, so large runs are split into batches.
    """
    hostname_by_href = {
        change['workload'].get('href'): change['hostname']
        for change, _ in resolved
    }
    payload = []
    for change, hrefs in resolved:
        item = {'href': change['workload'].get('href')}
        if hrefs is not None:
            item['labels'] = [{'href': href} for href in hrefs]
        if change['current_mode'] != change['desired_mode']:
            item['enforcement_mode'] = change['desired_mode']
        payload.append(item)

    bulk_url = f'{base_url}/orgs/{org}/workloads/bulk_update'
    total_batches = (len(payload) + BULK_UPDATE_LIMIT - 1) // BULK_UPDATE_LIMIT
    if total_batches > 1:
        print(f"Updating {len(payload)} workload(s) in {total_batches} batches "
              f"of up to {BULK_UPDATE_LIMIT}...")

    updated = 0
    failures = []  # (hostname, message)

    for number, batch in enumerate(batched(payload, BULK_UPDATE_LIMIT), start=1):
        response = requests.put(bulk_url, auth=auth, verify=cert_path, json=batch)

        # Older PCEs without bulk_update: fall back to one PUT per workload.
        if response.status_code in (404, 405):
            if number == 1:
                print("  bulk_update unavailable on this PCE, "
                      "updating workloads individually...")
            batch_updated, batch_failures = send_updates_individually(
                batch, hostname_by_href)
            updated += batch_updated
            failures.extend(batch_failures)
            continue

        if response.status_code not in (200, 204):
            for item in batch:
                failures.append((
                    hostname_by_href.get(item['href'], item['href']),
                    f"batch failed ({response.status_code}) {response.text}",
                ))
            continue

        # A 200 carries per-workload results; anything without an error counts
        # as applied.
        try:
            results = response.json()
        except ValueError:
            results = []

        if not results:
            updated += len(batch)
        else:
            for result in results:
                href = result.get('href')
                hostname = hostname_by_href.get(href, href)
                errors = result.get('errors') or []
                if errors or result.get('status') not in (None, 'updated'):
                    message = '; '.join(
                        error.get('message', str(error)) for error in errors
                    ) or str(result.get('status'))
                    failures.append((hostname, message))
                else:
                    updated += 1

        if total_batches > 1:
            print(f"  batch {number}/{total_batches}: {len(batch)} sent")

    report_update_results(resolved, hostname_by_href, updated, failures)


def send_updates_individually(batch, hostname_by_href):
    """One PUT per workload, for PCEs that do not offer bulk_update."""
    updated = 0
    failures = []
    for item in batch:
        hostname = hostname_by_href.get(item['href'], item['href'])
        body = {k: v for k, v in item.items() if k != 'href'}
        response = requests.put(
            f"{base_url}{item['href']}",
            auth=auth,
            verify=cert_path,
            json=body,
        )
        if response.status_code in (200, 204):
            updated += 1
        else:
            failures.append((hostname, f"({response.status_code}) {response.text}"))
    return updated, failures


def report_update_results(resolved, hostname_by_href, updated, failures):
    # For a handful of workloads, name each one as before; past that a summary
    # keeps the output readable.
    if len(resolved) <= MAX_LISTED and not failures:
        for change, _ in resolved:
            print(f"  {change['hostname']}: updated")
        return

    print(f"\n{updated} workload(s) updated, {len(failures)} failed.")
    if failures:
        print("\nFailures:")
        for hostname, message in failures[:MAX_LISTED]:
            print(f"  {hostname}: {message}")
        if len(failures) > MAX_LISTED:
            print(f"  ... and {len(failures) - MAX_LISTED} more")


def apply_label_changes(changes, action, skip_confirm=False, backup=True,
                        backup_path=None):
    if not changes:
        print(f"\nNo label changes to {action}.")
        return

    if any(c['current_labels'] != c['desired_labels'] for c in changes):
        label_href_map = {
            (label.get('key'), label.get('value')): label.get('href')
            for label in fetch_labels()
        }
    else:
        label_href_map = {}

    # Resolve desired labels to hrefs up front, so we can report anything
    # unresolvable (e.g. a label that doesn't exist yet) before touching the PCE.
    resolved = []
    unresolved = []
    for change in changes:
        # An enforcement-only change leaves labels alone, so it needs no hrefs
        # and must not send a labels list.
        if change['current_labels'] == change['desired_labels']:
            resolved.append((change, None))
            continue

        hrefs = []
        missing_pairs = []
        for key, value in change['desired_labels'].items():
            href = label_href_map.get((key, value))
            if href is None:
                missing_pairs.append(f"{key}:{value}")
            else:
                hrefs.append(href)
        if missing_pairs:
            unresolved.append((change['hostname'], missing_pairs))
        else:
            resolved.append((change, hrefs))

    if unresolved:
        print("\nSkipping workloads with labels that don't exist in the PCE:")
        for hostname, missing_pairs in unresolved:
            print(f"  {hostname}: {', '.join(missing_pairs)}")

    if not resolved:
        print(f"\nNo applicable label changes to {action}.")
        return

    print(f"\nAbout to update labels on {len(resolved)} workload(s):")
    for change, _ in resolved[:MAX_LISTED]:
        line = (f"  {change['hostname']}: "
                f"{format_labels(change['current_labels'])}  ->  "
                f"{format_labels(change['desired_labels'])}")
        if change['current_mode'] != change['desired_mode']:
            line += (f"  [enforcement {change['current_mode']} -> "
                     f"{change['desired_mode']}]")
        print(line)
    if len(resolved) > MAX_LISTED:
        print(f"  ... and {len(resolved) - MAX_LISTED} more")

    if not skip_confirm:
        try:
            answer = input(
                f"\n{action.capitalize()} these changes? [y/N] ").strip().lower()
        except EOFError:
            # No one on the other end of stdin, e.g. a piped or cron run
            # without -y; treat the missing answer as "no".
            answer = ''
            print()
        if answer != 'y':
            print("Aborted, no changes applied.")
            return

    # Snapshot current labels before touching the PCE, unless --no-backup.
    if backup:
        print()
        write_labels_backup(
            [change['workload'] for change, _ in resolved],
            backup_path,
        )
    else:
        print("\nSkipping backup (--no-backup): the previous labels will not be "
              "saved locally.")

    print()
    send_updates(resolved)


def main():
    parser = argparse.ArgumentParser(
        description='Look up Illumio workloads listed in a CSV file, show their labels '
                    'and enforcement information, and set labels and enforcement '
                    'modes on them - either from --label and --enforcement, or '
                    'per host from the workloads file itself.',
        epilog='Examples:\n'
               '  quarantine_workloads.py --workloads quarantine-workloads.csv\n'
               '  quarantine_workloads.py --workloads quarantine-backup.18092026.1528.csv --apply\n'
               '  quarantine_workloads.py --workloads quarantine-workloads.csv --label Q-Level-1 --preview\n'
               '  quarantine_workloads.py --workloads quarantine-workloads.csv --label Q-Level-1 --apply\n'
               '  quarantine_workloads.py --workloads quarantine-workloads.csv --label Q-Level-1 --rollback\n'
               '  quarantine_workloads.py --workloads quarantine-backup.18092026.1528.csv --label Q-Level-1 --rollback\n'
               '  quarantine_workloads.py --workloads quarantine-workloads.csv --backup\n'
               '  quarantine_workloads.py --pce prod --workloads quarantine-workloads.csv\n'
               '  quarantine_workloads.py --workloads quarantine-workloads.csv --enforcement full --apply',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--workloads',
        metavar='CSV_FILE',
        required=True,
        help='Required. Path to a CSV file listing the hostnames to look up. '
             'Either a CSV with a "hostname" column or one hostname per line. '
             'In the CSV form, every other column is read as a label key and '
             'its cell as that label\'s value, and an "enforcement_mode" '
             'column sets the mode, so a backup file can be applied back as '
             'it was recorded. Empty cells are left alone.',
    )
    parser.add_argument(
        '--pce',
        metavar='NAME',
        help='Which PCE from config.yaml to talk to. Optional when config.yaml '
             'defines a single PCE or sets default_pce.',
    )
    parser.add_argument(
        '--label',
        metavar='VALUE',
        action='append',
        default=[],
        help='Label to append to each workload, given as the label value alone '
             '(e.g. Q-Level-1). The label key is looked up on the PCE. May be '
             'repeated or comma-separated. Applies to every host in the list, '
             'and wins over the same key read from the workloads file. '
             'Required with --preview, --apply and --rollback unless the '
             'workloads file carries the labels itself.',
    )
    parser.add_argument(
        '--enforcement',
        metavar='MODE',
        choices=ENFORCEMENT_MODES,
        help='Enforcement mode to set on each workload: '
             + ', '.join(ENFORCEMENT_MODES) + '. May be used on its own or '
             'together with --label. Applies to every host in the list, and '
             'overrides an "enforcement_mode" column in the workloads file.',
    )
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        '--preview',
        action='store_true',
        help='Show the label changes --label would make, without applying them.',
    )
    action_group.add_argument(
        '--apply',
        action='store_true',
        help='Append the --label labels to the workloads, after showing the '
             'changes and prompting for confirmation.',
    )
    action_group.add_argument(
        '--rollback',
        action='store_true',
        help='Undo a change, after showing it and prompting for confirmation. '
             'Labels carried by the workloads file are written back onto the '
             'workloads, so rolling back from a backup restores the state it '
             'recorded. The --label labels are removed, and only where both '
             'the key and the value match.',
    )
    parser.add_argument(
        '--backup',
        metavar='CSV_FILE',
        nargs='?',
        const=True,
        help='Write the targeted workloads\' current labels to a CSV file, in the '
             'same format the backup files use. This happens automatically '
             'before --apply and --rollback; pass this flag to choose the '
             'filename, or on its own to take a snapshot without changing '
             'anything. Defaults to quarantine-backup.<ddmmyyyy>.<hhmm>.csv.',
    )
    parser.add_argument(
        '-y', '--yes', '--no-prompt',
        dest='yes',
        action='store_true',
        help='Run without asking for confirmation when applying or rolling back. '
             'Intended for scripted runs.',
    )
    parser.add_argument(
        '--no-backup',
        action='store_true',
        help='Skip the backup that is otherwise written before --apply and '
             '--rollback. Nothing is saved locally, so the previous labels '
             'cannot be restored from disk.',
    )

    # With no arguments at all, show the full help instead of argparse's
    # one-line "argument --workloads is required" error.
    if len(sys.argv) == 1:
        parser.print_help()
        return

    args = parser.parse_args()

    label_values = parse_label_args(args.label, parser)
    acting = args.preview or args.apply or args.rollback

    try:
        configure_pce(args.pce)
    except FileNotFoundError:
        parser.error('config.yaml not found')
    except ValueError as exc:
        parser.error(str(exc))

    try:
        targets = read_workload_targets(args.workloads)
    except FileNotFoundError:
        parser.error(f"workloads CSV file not found: {args.workloads}")
    except ValueError as exc:
        parser.error(str(exc))

    if not targets:
        parser.error(f"no hostnames found in {args.workloads}")

    hostnames = [target['hostname'] for target in targets]
    targets_by_host = {target['hostname']: target for target in targets}

    # The workloads file may carry the labels and enforcement mode itself, in
    # which case --label / --enforcement are optional.
    file_labels = any(target['labels'] for target in targets)
    file_enforcement = any(target['enforcement'] for target in targets)

    if acting and not (args.label or args.enforcement
                       or file_labels or file_enforcement):
        parser.error('--preview, --apply and --rollback require --label VALUE '
                     'and/or --enforcement MODE, or a workloads file carrying '
                     'label columns and/or an enforcement_mode column')

    # Resolving a value to its key needs the PCE's label list, so it happens
    # after the connection is configured.
    labels = resolve_label_values(label_values, parser)
    if acting and file_labels:
        validate_target_labels(targets, parser)

    workloads = fetch_workloads()
    found, missing_hosts = match_workloads(workloads, hostnames)

    report(found, missing_hosts, len(hostnames), targets_by_host)

    backup_path = None if args.backup in (None, True) else args.backup

    # With no action flag, --backup writes a standalone snapshot on its own.
    if args.backup and found and not (args.apply or args.rollback):
        print()
        write_labels_backup([workload for _, workload in found], backup_path)

    # No action flag: the report (and any --backup) is all that was asked for.
    if not (args.preview or args.apply or args.rollback):
        return

    if not found:
        return

    if args.rollback:
        if file_labels:
            print("\nThe workloads file carries labels, so they are restored "
                  "onto the workloads rather than removed from them"
                  + (f"; --label {', '.join(sorted(labels.values()))} "
                     f"{'is' if len(labels) == 1 else 'are'} removed."
                     if labels else "."))
        changes, mismatches = compute_rollback(found, labels, args.enforcement,
                                               targets_by_host)
        print_label_diff(changes, 'rollback')
        if mismatches:
            print("\nNot removed, the key now holds a different value:")
            for hostname, key, current, expected in mismatches[:MAX_LISTED]:
                print(f"  {hostname}: {key} is {current}, expected {expected}")
            if len(mismatches) > MAX_LISTED:
                print(f"  ... and {len(mismatches) - MAX_LISTED} more")
        apply_label_changes(changes, 'rollback', skip_confirm=args.yes,
                            backup=not args.no_backup,
                            backup_path=backup_path)
        return

    changes, replacements = compute_append(found, labels, args.enforcement,
                                           targets_by_host)
    print_label_diff(changes, 'append')
    if replacements:
        print("\nNote, a workload holds one label per key, so these are replaced:")
        for hostname, key, current, new in replacements[:MAX_LISTED]:
            print(f"  {hostname}: {key} {current} -> {new}")
        if len(replacements) > MAX_LISTED:
            print(f"  ... and {len(replacements) - MAX_LISTED} more")

    if args.apply:
        apply_label_changes(changes, 'apply', skip_confirm=args.yes,
                            backup=not args.no_backup,
                            backup_path=backup_path)


if __name__ == '__main__':
    main()
