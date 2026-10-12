# Divibe

**A true git history for a project written with an AI agent, rebuilt from the
agent's own records.**

A project built with Claude Code often reaches a public repository as one
commit, or as a handful of snapshots, although every change it went through
is on disk: Claude Code keeps a log of each session and a copy of each file
before it changes it. Divibe reads them, replays the work in order, and
writes a repository whose commits are the requests that were made, dated
when the work was done.

It never invents work. A commit holds what the logs show was changed, and
the last commit is the project as it stands, checked file by file.

```sh
uv tool install git+https://github.com/fgallaire/divibe

divibe survey PROJECT                  # what the logs hold, and the quality to expect
divibe units PROJECT --todo            # the units of work still needing a message
divibe messages PROJECT messages.tsv   # record them: RANK<TAB>MESSAGE per line
divibe build PROJECT --out REPO        # write the history into a new repository
divibe verify PROJECT                  # rebuild a git project's history and compare it with its own
```

Or once, without installing it: `uvx --from git+https://github.com/fgallaire/divibe divibe survey PROJECT`.
From a clone, `uv run divibe` does the same.

Python 3.10 or later, and no dependency. Divibe reads the logs and writes only
the new repository and its own state, under `~/.cache/divibe/`.

## What the survey checks

`divibe survey PROJECT` reads everything before writing anything, and says:

**The sources.**

- The session logs that name the project, in `~/.claude/projects/`: those of
  the project itself and those of sessions run from elsewhere that changed
  its files. Claude Code deletes logs older than `cleanupPeriodDays` (30 days
  unless `~/.claude/settings.json` says otherwise): the survey warns when
  that is short.
- The file backups in `~/.claude/file-history/`: a file's content before a
  turn changed it. Claude Code prunes them; those left are points where the
  replay is checked, and corrected if it went wrong.
- Copies of the project you kept, given as `--copy FOLDER@MOMENT`.
- The project as it stands: in a git repository, what git tracks and does
  not ignore; elsewhere, everything but the usual build folders and what the
  `.gitignore` files leave out.

**How each change was made**, since the history is as good as the changes
the logs describe exactly:

| the change went through | the replay is |
|---|---|
| the agent's `Write` and `Edit` | exact |
| simple shell commands: `sed -i`, `cat > file`, `echo >`, `cp`, `mv`, `rm`, `touch`, `git mv`, a Python script given on standard input | emulated, usually exact |
| anything else run in the project: a build, a package manager, a script on disk, a git command rewriting files | unknown: listed, and taken from the next backup or copy |

**The commits the agent made itself.** They are kept as they were: their cut,
their message, their moment.

**Where the replay ends**, against the project as it stands, and the quality
to expect:

- **exact**: the replay never had to be corrected and ends on the project
  file for file. Every commit holds what that moment held.
- **close**: a few files are not where the replay leaves them. The last commit
  takes them as they are, so the commits before it may miss part of their
  changes.
- **partial**: much of the work is not in the logs, or the logs are gone. The
  commits hold what the logs show and the last one is a large catch-up.
  Copies of the project from the past make it better.

## Messages

A unit is the work of one request. Its commit message comes from, in order:

1. the messages you recorded with `divibe messages`;
2. the agent's own `git commit -m`, for the units that end with one;
3. nothing: `build` stops and names the units, unless `--draft` is given, which
   takes the first line of each request.

When the agent committed, its commits are the history: work cut off before
one of them, by a new request or a session that ended, goes into it.

A message can also be `+`, which folds the unit into the next one that has a
message (a chain of fixes of one defect becomes one commit), or `-`, which
drops a unit that a later one takes back.

A treatment of an audit names its defects as it goes ("F1 is fixed. Now F2."):
`--markers 'pattern'` cuts a unit wherever what the agent says names another
one, the pattern's first group being the defect.

## Dates

A commit is dated when its work ended. A commit the agent made keeps its own
moment. The other commits of one request are spread out after their work,
one and a half to five and a half minutes apart, as a person commits them one
after the other, and all before the first commit of the next request. No
commit is dated before its work, and no two share a second.

## For the best history

- **Let the agent commit as it goes.** Its commits are kept as they are, and
  `divibe verify` proves the logs reproduce them.
- **Let the agent change files with `Write` and `Edit`**, not shell commands:
  those are replayed exactly.
- **Keep the logs**: set `cleanupPeriodDays` high in `~/.claude/settings.json`.
- **Ignore build products** in `.gitignore`, so that no history holds them.
- **Keep copies** of a project you cannot have the agent commit: each one is a
  point where the history is checked.

## Safety

Divibe runs none of the commands it reads, but one kind: a Python script the
agent gave on standard input, run in Divibe's own process so that its
changes land on the in-memory tree. While it runs, an audit hook refuses
anything that would write a file, remove or rename one, start a program or
open a connection, whichever function the script uses. A replay changes
nothing on the machine; the only files Divibe writes are the new repository
and its own state.

## Validation

Divibe was written with Claude Code, committing as it went, and `divibe verify`
rebuilds its history from the logs of that work: every commit identical in
message and tree, including one amended and a rewrite that removed compiled
files committed by mistake, with dates within a second.

## License

Divibe is licensed under the MIT License: see the `LICENSE` file.
