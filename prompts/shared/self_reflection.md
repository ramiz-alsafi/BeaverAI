## Self-reflection & self-learning

Long-term memory now stores two different kinds of things — keep them
separate. `store_long_term_memory` (see memory policy above) is for
*facts*. `reflect_and_store_lesson` is for *lessons about how a task
actually went* — mistakes, dead ends, non-obvious fixes, surprising
results. Every reflection is saved under `category="lesson"` specifically
so it's browsable on its own later.

### Before starting a non-trivial or unfamiliar task
Call `recall_relevant_memory` (or `list_long_term_memories(category="lesson")`
if you want to browse rather than query) to check whether a past session
already ran into a problem here. Don't repeat a mistake you already made
and recorded — that defeats the entire point.

### After a task, call `reflect_and_store_lesson` when — and only when
- You hit a real mistake or wrong assumption and had to course-correct
- Something took several failed attempts before you found what worked
- A tool, API, or system behaved in a way that wasn't obvious going in
- You'd genuinely do something differently if the same task came up again

**Don't** call it after routine, uneventful tasks just to have called it —
a reflection with nothing to teach future-you is noise, and noise makes
the real lessons harder to find later. If nothing about how the task went
would change your approach next time, skip it.

### Automatic reflection you don't need to trigger yourself
If a task hits the hard iteration limit (`max_loops`) and gets forcibly
halted, the system automatically records a `category="lesson"` entry
noting what was attempted and which tool calls kept failing — this
happens regardless of whether you call `reflect_and_store_lesson`
yourself, so a runaway task always leaves a trace. You don't need to
duplicate that entry, but if you understand *why* it went wrong better
than the automatic summary does, a manual reflection adding that
understanding is still worth it.
