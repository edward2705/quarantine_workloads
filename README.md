# Illumio Quarantine

`quarantine_workloads.py` looks up Illumio workloads by hostname, shows their
labels and enforcement state, and sets labels and enforcement modes on them —
either from `--label` / `--enforcement` applied across the list, or per host from
the workloads file itself. The file format it reads is the one its backups are
written in, so a backup can be fed straight back in to restore what it recorded.

## Prerequisites

Set up the following on the PCE before you use the script:

- **Create and provision a quarantine policy for each quarantine level.** A
  workload is only isolated once a provisioned policy acts on its quarantine
  label (for example `Q-Level-1`). If no policy uses that label, the workload
  gets the label but its traffic does not change. The quarantine labels must
  also exist already, because the script never creates labels.
- **The script only assigns quarantine levels to workloads.** It adds or removes
  the quarantine label on the workloads you list, and can also set their
  enforcement mode. It does not create, change or provision labels, rulesets or
  rules. Build those in the PCE first.

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # requests, PyYAML
source .venv/bin/activate

$EDITOR config.yaml                                   # see Configuration

# read-only report on the hosts listed in a file
python quarantine_workloads.py --workloads quarantine-workloads.csv
```

The script has no shebang, so run it as `python quarantine_workloads.py ...`.
Nothing is written to the PCE without `--apply` or `--rollback`, and both show a
diff, save a backup and prompt before sending anything.

## Configuration

`config.yaml` holds one block per PCE:

```yaml
default_pce: sncalpha        # used when --pce is omitted

sncalpha:
  api_key: api_xxxxxxxxxxxx
  api_secret: <secret>
  pce_fqdn: sncalpha.internal.local
  port: 8443
  org: 1                     # optional, defaults to 1
  cert_path: /path/to/server.crt

prod:
  api_key: api_yyyyyyyyyyyy
  api_secret: <secret>
  pce_fqdn: prod-pce.example.com
  port: 8443
  cert_path: /path/to/prod.crt
```

Pick one with `--pce NAME`, which can be omitted when a single PCE is defined or
`default_pce` names one. The URL is built as
`https://<pce_fqdn>:<port>/api/v2/orgs/<org>/...`, so the scheme and API path are
never repeated. `cert_path` is passed to `requests` as `verify` and must point at
the PCE's certificate.

A single PCE may also be written flat, with the connection fields at the top
level and no name, and a legacy `base_url` is still accepted and split into
`pce_fqdn` + `port`.

`config.yaml` is gitignored — it holds the API secret in plaintext.

## Workload list

`--workloads` names the file of hosts to act on, in one of two shapes.

**A plain list** — the target list and nothing more:

```
sarl9wkld1          # one hostname per line
- sarl9wkld1        # leading "- " bullets are stripped
```

**A CSV with a `hostname` column** — every other column is an instruction for
that host. The column name is the label key, the cell is the label value, and an
`enforcement_mode` column sets the mode:

```csv
hostname,role,app,env,loc,pt,Q,enforcement_mode
sarl8wkld1,R-Container,A-Podman,E-Prod,L-NY,P-VEN,Q-Level-2,visibility_only
sarl9wkld2,R-DB,A-App1,E-Prod,L-NY,P-VEN,Q-Level-2,full
```

This is exactly what `--backup` writes, so a backup is also a valid input file.
In both shapes, blank lines and `#` comments are skipped and duplicate hostnames
collapse to their first row.

- **Empty cells carry no instruction** — that key is left as it stands.
- **`--label` and `--enforcement` become optional** when the file carries the
  information. Given anyway, they apply to every host and win where they name the
  same key or the mode.
- **Keys and values from the file must already exist on the PCE**, checked before
  anything is written. Every non-reserved column is read as a label key, so an
  incidental `notes` or `status` column is rejected rather than ignored.
- **An unknown `enforcement_mode` is refused** with the line it is on.

## Usage

```
python quarantine_workloads.py [--pce NAME] --workloads CSV_FILE
                     [--label VALUE] [--enforcement MODE]
                     [--preview | --apply | --rollback]
                     [--backup [CSV_FILE]] [--no-backup] [--no-prompt]
```

| Flag | Purpose |
| --- | --- |
| `--pce NAME` | Which PCE from `config.yaml` to use. Optional with a single PCE or a `default_pce`. |
| `--workloads CSV_FILE` | **Required.** Hosts to act on, and optionally the labels and enforcement mode for each. |
| `--label VALUE` | Label to append or remove, as the value alone (e.g. `Q-Level-1`); the key is looked up on the PCE. Repeatable and comma-separated. Applies to every host. Required by the action flags unless `--enforcement` is given or the file carries the labels. |
| `--enforcement MODE` | `idle`, `visibility_only`, `selective` or `full`. Usable alone, with `--label`, or left to the file's `enforcement_mode` column. |
| `--preview` | Show the changes without making them. |
| `--apply` | Apply the labels and enforcement mode, after the diff and a prompt. |
| `--rollback` | Undo a change: labels carried by the file are restored onto the workloads, and the `--label` labels are removed. |
| `--backup [CSV_FILE]` | Choose the backup filename, or take a standalone snapshot. |
| `--no-backup` | Skip the backup `--apply`/`--rollback` write. |
| `-y`, `--yes`, `--no-prompt` | Run without the confirmation prompt. |

With no action flag the script only reports. Run with no arguments for help.

### Reporting

```console
$ python quarantine_workloads.py --workloads quarantine-workloads.csv

Workloads (2 of 2 found on sncalpha):
Hostname    ROLE         APP       ENV     LOC   Enforcement      Visibility                Status
----------  -----------  --------  ------  ----  ---------------  ------------------------  ------
sarl9wkld2  R-WebApp     A-App1    E-Prod  L-NY  full             enhanced_data_collection  online
sarl8wkld1  R-Container  A-Podman  E-Prod  L-NJ  visibility_only  enhanced_data_collection  online
```

Label columns are discovered from the workloads themselves, ordered
`role, app, env, loc, pt` first and other keys alphabetically — a workload
carrying no labels on the PCE has none to show. Hostnames not found are listed
below the table.

When the file carries labels or an `enforcement_mode` column, the report gains a
`From File` and/or `File Enforcement` column holding what the file records for
that host — the full set, matching or not, so the two sides can be read against
each other. No action flag is needed and nothing is written; `--preview` narrows
it to just the changes.

### Changing labels and enforcement

```bash
# preview, then apply a quarantine label (writes a backup, then prompts)
python quarantine_workloads.py --workloads quarantine-workloads.csv --label Q-Level-1 --preview
python quarantine_workloads.py --workloads quarantine-workloads.csv --label Q-Level-1 --apply

# label and enforcement in one pass; --enforcement also works on its own
python quarantine_workloads.py --workloads quarantine-workloads.csv \
                     --label Q-Level-1 --enforcement full --apply

# several labels, repeated or comma-separated; -y skips the prompt
python quarantine_workloads.py --workloads quarantine-workloads.csv \
                     --label Q-Level-1,E-Dev --apply -y

# release: remove the label again
python quarantine_workloads.py --workloads quarantine-workloads.csv --label Q-Level-1 --rollback
```

Only workloads not already in the target mode are sent, and an enforcement-only
change sends just `enforcement_mode`, leaving the label set untouched.

### Restoring from a backup

A backup records each host's labels and enforcement mode as they were, so
applying one puts them back. `--rollback` does the same restore and additionally
removes whatever `--label` names — the usual "undo the quarantine" in one pass:

```bash
python quarantine_workloads.py --workloads quarantine-backup.18092026.1532.csv --preview
python quarantine_workloads.py --workloads quarantine-backup.18092026.1532.csv \
                     --label Q-Level-2 --rollback
```

```
The workloads file carries labels, so they are restored onto the workloads rather than removed from them; --label Q-Level-2 is removed.

Label rollback preview:
Hostname    Current Labels                                        New Labels
----------  ----------------------------------------------------  --------------------------------------------------------------
sarl8wkld1  role:R-Container, app:A-Podman, env:E-Prod, loc:L-NJ  role:R-Container, app:A-Podman, env:E-Prod, loc:L-NY, pt:P-VEN
```

A file carrying labels describes the state to go back to, so its labels are
**written back onto** the workloads, never stripped from them. Only `--label`
removes. Where both name the same key, `--label` wins and the key is removed.

## Behaviour worth knowing

**One label per key.** A workload holds at most one label per key, so appending a
key it already carries *replaces* that value. Replacements are called out before
anything is written:

```
Note, a workload holds one label per key, so these are replaced:
  sarl9wkld2: env E-Prod -> E-Quarantine
```

**`--rollback --label` removes, it does not restore.** Given only `--label`, it
deletes labels matching both key *and* value, so rolling back `--label
E-Quarantine` leaves the workload with no `env` label at all — not back at
`E-Prod`. A key whose value has since changed is reported and left alone. To get
the old value back, roll back from a file that records it; pointing `--rollback`
at a full backup with no `--label` restores everything and removes nothing.

**Labels must already exist on the PCE.** The script never creates them. A value
defined under more than one key is refused rather than guessed, as are two
`--label` values resolving to the same key.

**Updates send the full label set.** The Illumio API has no append operation, so
each update carries the complete desired label list — which is why workloads are
re-fetched on every run.

**Large estates.** Changes go through `bulk_update` in batches of 1000, so
relabelling 2500 workloads is 3 API calls rather than 2500. Per-workload results
are read back from each batch, so partial failures are reported individually
rather than hidden by an overall success. A PCE without `bulk_update` falls back
to one request per workload. Runs of 20 workloads or fewer name each host; past
that, listings are summarised. The fetch asks for up to 100000 workloads
(`WORKLOAD_FETCH_LIMIT`) and warns if the PCE reports more than it returned.

**Confirmation.** With no one on the other end of stdin — a piped or cron run
without `-y` — the missing answer counts as "no" and the run aborts unchanged.

## Backups

`--apply` and `--rollback` write the targeted workloads' labels, as they were
before the change, to `quarantine-backup.<ddmmyyyy>.<hhmm>.csv`, suffixing `-1`,
`-2` … rather than overwriting an existing file. Take a standalone snapshot with
`--backup` and no action flag, or choose the name with `--backup FILE`.

```csv
hostname,role,app,env,loc,pt,enforcement_mode
sarl8wkld1,R-Container,A-Podman,E-Prod,L-NY,P-VEN,visibility_only
sarl9wkld2,R-DB,A-App1,E-Prod,L-NY,P-VEN,full
```

Backups are gitignored — they are an inventory of your infrastructure.

> `--apply --no-prompt --no-backup` relabels every host in the list with no
> confirmation and no local record of the previous labels. Keep a backup, or a
> restore has nothing to read from.

## Troubleshooting

**A workload shows no label columns, or blank labels.** The report prints what
the PCE returns; empty columns mean the PCE holds no labels for that workload,
not that the script failed to read them. Confirm directly:

```bash
python - <<'EOF'
import quarantine_workloads as q, requests, json
q.configure_pce()                      # or configure_pce('prod')
r = requests.get(f'{q.base_url}/orgs/{q.org}/workloads',
                 auth=q.auth, verify=q.cert_path,
                 params={'max_results': 100000})
for w in r.json():
    print(w.get('hostname'), json.dumps(w.get('labels')))
EOF
```

`labels: []` for a host confirms it. The usual cause is an earlier
`--rollback --label` that named every label the workload had; recover by rolling
back or applying a backup that records them.

**`the workloads file has labels that do not exist in the PCE: notes:internal`.**
Every column other than `hostname` and `enforcement_mode` is read as a label key,
so a spreadsheet's extra column looks like a label. Remove the column, or create
that label on the PCE.

**`--preview, --apply and --rollback require --label VALUE and/or --enforcement
MODE`.** The file is a plain hostname list with nothing to apply. Pass `--label`
/ `--enforcement`, or use a file with label columns.

**`no label with value 'X' exists in the PCE`.** `--label` takes the value alone
and resolves the key from the PCE. Check spelling and case; if the value exists
under two keys the error names them, and the label must be created on the PCE
first.

**`unknown PCE 'x'. Available: ...`** — the name is not a block in `config.yaml`.
With several PCEs and no `default_pce`, `--pce` is required rather than guessed.

**Certificate errors on connect.** `cert_path` must point at the PCE's own
certificate; `requests` uses it as `verify`.

**A run stopped at `Rollback these changes? [y/N]` and reported "Aborted, no
changes applied."** — stdin had no answer to give. Pass `-y` for unattended runs.
