# Contributing

Bug reports and focused pull requests are welcome. Please keep the project true to its purpose: one fast, glanceable context bar with no account, quota, notification, or dashboard features.

Before opening a pull request, run:

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/ctx_bar.py
```

When adding or changing an agent collector, include an anonymized fixture-style unit test that covers the relevant session format and compaction behavior.
