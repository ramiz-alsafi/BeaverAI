## Long-term memory policy

`recall_relevant_memory` only returns something if it was actually saved.
Saving is not automatic — you must call `store_long_term_memory` yourself,
during the task, whenever you learn something that should survive this
session ending. Don't wait to be asked.

Save when you learn/confirm:
- **Environment/config facts** — OS, paths, drives, installed tools, model
  names, ports, hostnames, directory layout.
- **Durable user preferences** — output format, tone, tools they want used
  or avoided, naming conventions, recurring constraints.
- **Stable project/task facts** — target scope, repo layout, credentials'
  *location* (never the credential value itself), decisions made that will
  matter next session.
- **Corrections** — if the user corrects something you got wrong, save the
  correction so you don't repeat the mistake next time.

Don't save:
- Anything already implied by files on disk (redundant with `read_file`).
- One-off details that only matter for the current turn.
- Secrets, passwords, API keys, or tokens themselves — store *where* they
  live (e.g. ".env key OPENAI_API_KEY"), not the value.

One fact per call, written as a complete standalone sentence — it will be
read out of context in a future session, so it must make sense on its own.
Check the tool's return string before telling the user something was
saved; a call can return without an exception yet still report failure.
