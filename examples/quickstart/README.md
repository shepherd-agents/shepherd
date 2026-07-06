# Shepherd Quickstart Examples

These scripts are the checked-in form of the demos emitted by `sp demo write`.

```bash
python examples/quickstart/offline_task.py

mkdir /tmp/shepherd-quickstart
cd /tmp/shepherd-quickstart
sp init
sp demo write quickstart > quickstart_demo.py
python quickstart_demo.py
sp run show --latest
sp run trace --latest --events
```

`claude_readme.py` is optional: it needs a live `claude` CLI, so it skips (with
the reason) when auth isn't ready. `sp doctor claude` should pass first; add
`--probe` when debugging auth for a real round-trip. On a subscription, the most
reliable path is a long-lived token: `export CLAUDE_CODE_OAUTH_TOKEN=$(claude setup-token)`.
