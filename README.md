# Divibe

**A true git history for a project written with an AI agent, rebuilt from the
agent's own records.**

A project built with Claude Code often reaches a public repository as one
commit, or as a handful of snapshots, although every change it went through
is on disk: Claude Code keeps a log of each session and a copy of each file
before it changes it. Divibe reads them, replays the work in order, and
writes a repository whose commits are the requests that were made, dated
when the work was done, with their messages.

It never invents work. A commit holds what the logs show was changed, and
the last commit is the project as it stands, checked file by file.

## What it reads

- **The session logs**, `~/.claude/projects/*/*.jsonl`: every request, every
  `Edit` and `Write` with its exact text, every shell command with its working
  directory, every `git commit` the agent made, and the time of each.
- **The file backups**, `~/.claude/file-history/<session>/`: a file's content
  before a turn changed it. Claude Code prunes them over time; those left are
  points where the replay is checked and corrected.
- **The project as it stands**, which the last commit must equal.
- **Copies you kept**, if any: a folder and the moment it shows.

## How good the history will be

`divibe survey` answers before anything is written. A history is as good as
the changes the logs describe exactly:

| the changes went through | the replay is |
|---|---|
| `Edit` and `Write` | exact |
| simple shell commands (`sed -i`, `cat >`, `cp`, `mv`, `rm`, a Python heredoc) | emulated, usually exact |
| scripts, builds, package managers, editors outside the agent | unknown: taken from the next backup or copy |

The survey says how many changes fall in each row, how many backups are left,
whether the agent committed itself, and how far the replay ends from the
project as it stands.

## Usage

```sh
python3 -m divibe survey PROJECT                # what the logs hold, and the expected quality
python3 -m divibe units PROJECT                 # cut the work into units, one per request
python3 -m divibe build PROJECT --out REPO      # write the history
```
