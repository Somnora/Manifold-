# Data pipelines: scrape → synthesize → download

The pattern: your script pulls raw data, the model served on the same
instance turns every record into structured points, and you download the
result as one file. Everything runs on the GPU box; nothing leaves it until
you pull the finished output down.

Worked example throughout: researching political candidates up for
reelection — scraping public filings, then synthesizing each record into
usable points.

## 0. One-time setup

Launch an instance and start the model you want to synthesize with:

- Jobs → `vllm-serve` with e.g. `Qwen/Qwen2.5-7B-Instruct`
- Wait until the chat panel shows it ready (first serve downloads weights)

## 1. Upload your scraper

Write the scraper on your machine (or have Claude write it). Inside the
container it sees the whole persistent filesystem at `/data`:

```python
# scrape_candidates.py — runs with the filesystem mounted at /data
import json, shlex, sys

args = shlex.split(sys.argv[1]) if len(sys.argv) > 1 else []
# ... fetch public filings, one dict per candidate ...
with open("/data/research/scrapes/candidates-raw.jsonl", "w") as out:
    for record in fetch_all(args):
        out.write(json.dumps(record) + "\n")
```

Upload it: instance card → **Browse** → navigate to `scripts/` (create by
uploading into it) → **Upload here**. If it needs packages, upload a
`requirements.txt` next to it — `script-run` installs it automatically.

## 2. Run the scrape

Jobs → **script-run**:

- `script`: `scrape_candidates.py`
- `args`: `--state TX --cycle 2026` (arrives as one string in `argv[1]`;
  `shlex.split` it yourself — the quoting is Manifold's injection guard)

Watch the logs stream on the Jobs page; outputs land on the persistent
filesystem, visible live in the Files panel.

## 3. Synthesize with the served model

Jobs → **llm-synthesize**:

- `input_path`: `research/scrapes/candidates-raw.jsonl` (.jsonl or .csv)
- `instruction`: `Extract the candidate's name, district, incumbency,
  top three funding sources, and stated positions as compact JSON.`
- `limit`: `5` first — check quality on a handful before burning GPU time
  on the whole dataset, then rerun with `0` (= all)

The job first **waits** for the served model to actually answer (so it is
safe to queue right after `vllm-serve`, before weights finish loading),
then maps your instruction over every record at temperature 0.1 and writes
one line per record to `synthesized/<output_name>.jsonl`:

```json
{"record": {...raw scrape...},
 "synthesis": "{\"name\": \"Jane Doe\", ...}",
 "synthesis_json": {"name": "Jane Doe", ...}}
```

When the model returns JSON (including ```json fenced blocks), it is parsed
into `synthesis_json` — your ready-to-use points. A non-JSON reply is kept
verbatim in `synthesis` and flagged with `"parse_error": true` instead.

Robustness built in: a transient model error is retried before a record is
counted as failed; a malformed input line is skipped and logged, never
fatal; a missing input path or an unreachable model fails immediately with
an actionable message (not a Python traceback). Progress prints every 25
records; failures are counted and logged, never silently dropped.

## 4. Pull the results down

Browse → `synthesized/` → **Download** the .jsonl (or **Download folder
(.tar.gz)** to grab everything). Done: raw scrapes stay on the filesystem
for re-synthesis with a better instruction; the box can be terminated —
everything on `/data` survives.

## Why this design

- **The model is on the same box** — llm-synthesize calls vLLM over the
  instance's own loopback (host networking, no tunnels, no per-token cost;
  you pay the GPU hour you were already paying).
- **Scripts are data** — `script-run` is one generic template; your
  pipeline stages are files on the filesystem, versioned however you like.
- **Everything is observable** — logs stream to the Jobs page, outputs
  appear in Files as they are written, and every job is audited.

## Doing it all from an agent

Every step above is also an MCP tool call (`upload_file`, `run_job`,
`get_job_status`, `download_file`) — so "scrape Texas candidates and give
me synthesized points" is a workflow Claude can drive end to end through
the same guarded backend.
