# Contributing

Use [GitHub Issues](https://github.com/alphaq0099/media-tool/issues) for feature
requests and reproducible non-sensitive bugs. Read [SUPPORT.md](SUPPORT.md) before
sharing diagnostics and [SECURITY.md](SECURITY.md) for security problems.

Development uses Python 3.13 and `requirements-dev.txt`. Run tests with
`python -m pytest -q`. Headless Linux tests use `QT_QPA_PLATFORM=offscreen`;
packaged native GUI checks run under a display or Xvfb.

Use synthetic or disposable test inputs. Preserve originals, validate outputs,
keep optional tool work supervised/cancellable, and keep expensive imports off
the startup path. Public documentation should describe current behavior.

Changes are contributed under this project's GPL-3.0-or-later license.
