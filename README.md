# Divibe

<p align="center"><img src="https://raw.githubusercontent.com/fgallaire/divibe/main/img/divibe.png" alt="Divibe" width="128"></p>

**A true git history for a project written with an AI agent, rebuilt from the
agent's own records.**

A project built with an AI agent often reaches a public repository as one
commit, or as a handful of snapshots, although every change it went through
is on disk: the agent keeps a log of each session. Divibe reads those of
**Claude Code**, **Codex CLI** and **OpenCode**, replays the work in order, and
writes a repository whose commits are the requests that were made, dated
when the work was done.

It never invents work. A commit holds what the logs show was changed, and
the last commit is the project as it stands, checked file by file.

```sh
uv tool install divibe     # or: pip install divibe

divibe PROJECT            # what the logs hold and the quality to expect, then the history in PROJECT-divibe
divibe -n PROJECT         # a dry run: the same, with nothing written
divibe --verify PROJECT   # for a project already in git: rebuild its history and compare it with its own
```

When commits need a message, `divibe PROJECT` writes them in
`PROJECT-divibe-messages.txt` and stops: correct them, run it again.

Or once, without installing it: `uvx divibe -n PROJECT`. From a clone,
`uv run divibe` does the same.

Python 3.10 or later, and no dependency. Divibe reads the logs and writes only
the new repository and the file of messages, next to the project.

## What Divibe checks first

`divibe PROJECT` reads everything before writing anything, and says, as
`divibe -n PROJECT` does without writing:

**The sources.**

- The session logs that name the project: those of the project itself and
  those of sessions run from elsewhere that changed its files, merged in
  time whichever agent wrote them.
  - Claude Code's, in `~/.claude/projects/`. Claude Code deletes logs older
    than `cleanupPeriodDays` (30 days unless `~/.claude/settings.json` says
    otherwise): Divibe warns when that is short.
  - Codex CLI's, in `~/.codex/sessions/` and `~/.codex/archived_sessions/`.
  - OpenCode's, in its database `~/.local/share/opencode/opencode.db`, read
    only. A subagent's session is part of its parent's work.
- Claude Code's file backups in `~/.claude/file-history/`: a file's content
  before a turn changed it. Claude Code prunes them; those left are points
  where the replay is checked, and corrected if it went wrong. Codex and
  OpenCode keep none.
- In a git repository older than the logs, its last commit before them: the
  project as the agent found it.
- Copies of the project you kept, given as `--copy FOLDER@MOMENT`.
- The project as it stands: in a git repository, what git tracks and does
  not ignore; elsewhere, everything but the usual build folders and what the
  `.gitignore` files leave out.

**How each change was made**, since the history is as good as the changes
the logs describe exactly:

| the change went through | the replay is |
|---|---|
| the agent's own tools: Claude Code's and OpenCode's `write` and `edit`, Codex's patches | exact, unless the file was changed outside the logs (by hand, say) |
| simple shell commands: `sed -i`, `cat > file`, `echo >`, `cp`, `mv`, `rm`, `touch`, `git mv`, a Python script given on standard input | emulated, usually exact |
| anything else run in the project: a build, a package manager, a script on disk, a git command rewriting files | unknown: listed, and taken from the next backup or copy |

**The commits the agent made itself.** They are kept as they were: their cut,
their message, their moment. A command that failed after its `git commit`, a
check chained behind it, still made the commit: git's reflog says so. A file
no log describes, written by a tool (`uv.lock`, `package-lock.json`), is
taken from the commit git recorded, and a `--fixup` commit that a rebase with
`--autosquash` squashed folds into the commit it names.

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

1. the file of messages, `PROJECT-divibe-messages.txt`;
2. the agent's own `git commit -m`, for the units that end with one.

When the agent committed, its commits are the history: work cut off before
one of them, by a new request or a session that ended, goes into it.

The other units need a message. `divibe PROJECT` writes them in the file and
stops, each on a line of its own after a short key, filled in with the first
line of its request; below it, what was asked, what the agent said it did,
and the files it changed:

```
3f9a1c  fais le module de rejeu
#  2026-10-12 13:24 UTC, asked: fais le module de rejeu, avec les tests
#  the agent said: The replay module is in place, with its tests.
#  files: replay.py +120/-0, tests/test_replay.py +45/-0
```

Correct the messages, run `divibe PROJECT` again, and it writes the history.
The keys stay the same from one run to the next, so the file keeps its
messages when new sessions add units.

A message can also be `+`, which folds the unit into the next one that has a
message (a chain of fixes of one defect becomes one commit), or `-`, which
drops a unit that a later one takes back.

A treatment of an audit names its defects as it goes ("F1 is fixed. Now F2."):
`--markers 'pattern'` cuts a unit wherever what the agent says names another
one, the pattern's first group being the defect.

## Dates

A commit is dated when its work ended. A commit the agent made keeps its own
moment: the one git's reflog records, while it keeps it (90 days by default),
else the moment the command started. The other commits of one request are
spread out after their work, one and a half to five and a half minutes apart,
as a person commits them one after the other, and all before the first commit
of the next request. No commit is dated before its work, and no two share a
second unless the agent made both in it.

## Tags

A tag the agent made with `git tag` goes on the commit it named, and one it
deleted is gone. A version file at the root (`pyproject.toml`, `package.json`,
`Cargo.toml`) tags `v<version>` the commit where that version first appears,
unless the agent placed that tag itself; a `package.json` marked private is
never released, so it tags nothing.

## For the best history

- **Let the agent commit as it goes.** Its commits are kept as they are, and
  `divibe --verify` proves the logs reproduce them.
- **Let the agent change files with its own tools**, not shell commands: those
  are replayed exactly.
- **Change no file by hand while the agent works**, or tell it to commit
  first: Codex and OpenCode keep no copy of a file, so a change made outside
  the logs makes the agent's next changes to that file fail to replay.
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
and the file of messages, and `divibe -n` writes none.

## Validation

Divibe was written with Claude Code, committing as it went, and
`divibe --verify` rebuilds its history from the logs of that work: every commit identical in
message and tree, including amended ones, one by a command that failed after
it, fixups squashed into older commits, and a rewrite that removed compiled
files committed by mistake, with the same dates to the second; and its tags
on the same commits, those placed afterwards on older commits included.

## License

Divibe is licensed under the MIT License: see the `LICENSE` file.
