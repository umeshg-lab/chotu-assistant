"""
Intent engine — parses voice commands and routes to the correct handler.
Two-tier: fast regex matching → trained command lookup → fallback.

FIX LOG (core/intent.py):
  BUG-A  _handle_play used m.lastindex and m.group(1) but the regex
         r'play(?: music| (.+))?' has an optional non-capturing group
         wrapping a capturing group, meaning m.group(1) is ALWAYS None when
         "play music" is said (no capture).  Added explicit None check.

  BUG-B  _handle_reminder had m.group(2) before m.group(1) — the regex
         r'set( a)? reminder(?: for)? (.+)' puts the task in group(2), but
         r'remind me(?: to)? (.+)' puts it in group(1).  The handler now
         uses m.lastindex to decide which group to read.

  BUG-C  _handle_status imports psutil inline but if psutil is not installed
         the handler crashes and JARVIS speaks "something went wrong".
         Added try/except with a fallback message.

  BUG-D  Intent patterns were ordered so that 'open (.+) in browser' could
         never be reached because 'open (.+)' was listed first and matched
         everything.  Reordered so more-specific patterns come before general
         ones.

  BUG-E  process() called self.memory.log_command(text) but did NOT log the
         response, so the history DB always had an empty response column.
         Fixed to log response after the handler returns.

  NEW    Handlers return the spoken text as the response where useful, so the
         UI can display it in the "last response" label.
"""

import re
import logging
import threading
from typing import Optional

logger = logging.getLogger("JARVIS.Intent")


class IntentEngine:
    def __init__(self, tts, memory, trainer, workflow, modes, reminder, config):
        self.tts      = tts
        self.memory   = memory
        self.trainer  = trainer
        self.workflow = workflow
        self.modes    = modes
        self.reminder = reminder
        self.config   = config

        # Lazy-imported automation modules (avoid circular imports at init)
        from automation import app_control, system_control, media_control, browser_control
        self.apps    = app_control
        self.system  = system_control
        self.media   = media_control
        self.browser = browser_control
        self._assistant_shutdown = None

        self._build_intent_map()

    def set_assistant_shutdown_callback(self, callback):
        self._assistant_shutdown = callback

    def _build_intent_map(self):
        """
        Pattern ordering rules:
          1. More-specific patterns BEFORE general ones ('open X in browser' before 'open X')
          2. Bare-trigger patterns (no capture group) BEFORE patterns that require content,
             so "set reminder" is caught even without a task description.
          3. Time/date patterns listed exhaustively to cover all natural phrasings.

        BUG-5 fix: Added bare "set(?: a)? reminder$" and "remind me$" patterns so saying
                   "set reminder" or "remind me" without additional text is caught and
                   prompts the user for more information rather than hard-failing.

        BUG-6 fix: Added bare "take a note$" and "new note$" patterns so saying
                   "take a note" without content is caught and prompts the user.

        BUG-7 fix: Added multiple time/date phrasings including "what time is it",
                   "time please", "tell me the time", "what's the time".
        """
        self._intents = [
            # ── Modes ─────────────────────────────────────────────────────────
            (r"activate (.+?) mode",                   self._handle_mode),
            (r"switch to (.+?) mode",                  self._handle_mode),
            (r"(.+?) mode",                            self._handle_mode),

            # ── Browser (specific BEFORE 'open (.+)') ─────────────────────────
            (r"open (.+) in browser",                  self._handle_browse),
            (r"open (.+) website",                     self._handle_browse),
            (r"go to (.+)",                            self._handle_browse),
            (r"search(?: for)? (.+)",                  self._handle_search),

            # ── Applications ──────────────────────────────────────────────────
            (r"(?:close|exit|quit|shutdown|shut down)(?: jarvis| assistant)$",
                                                       self._handle_exit_assistant),
            (r"^(?:exit|quit|close)$",                 self._handle_exit_assistant),
            (r"open (.+)",                             self._handle_open),
            (r"launch (.+)",                           self._handle_open),
            (r"start (.+)",                            self._handle_open),
            (r"close (.+)",                            self._handle_close),
            (r"kill (.+)",                             self._handle_close),

            # ── System ────────────────────────────────────────────────────────
            (r"shut down|shutdown",                    self._handle_shutdown),
            (r"restart|reboot",                        self._handle_restart),
            (r"sleep|hibernate",                       self._handle_sleep),
            (r"lock(?: the)? (?:pc|computer|screen)",  self._handle_lock),
            (r"take a screenshot|screenshot",          self._handle_screenshot),
            (r"empty(?: the)? (?:trash|recycle bin)",  self._handle_empty_trash),
            (r"task manager|processes",                self._handle_task_manager),

            # ── Volume ────────────────────────────────────────────────────────
            (r"volume up|increase volume|louder",      self._handle_vol_up),
            (r"volume down|decrease volume|quieter",   self._handle_vol_down),
            (r"set volume to (\d+)",                   self._handle_vol_set),
            (r"\bunmute\b",                            self._handle_unmute),
            (r"\bmute\b",                              self._handle_mute),

            # ── Media ─────────────────────────────────────────────────────────
            (r"play(?: music)?(?: (.+))?",             self._handle_play),
            (r"pause(?: music)?",                      self._handle_pause),
            (r"stop(?: music)?",                       self._handle_stop),
            (r"next(?: song| track)?",                 self._handle_next),
            (r"previous(?: song| track)?",             self._handle_prev),

            # ── Reminders ─────────────────────────────────────────────────────
            # Full form (task + time) must precede bare/partial forms
            (r"(?:move|reschedule|change|push|shift) (.+?)(?: reminder)? (?:to|at|for) (.+)",
                                                       self._handle_reminder_reschedule),
            (r"remind me(?: to)? (.+) at (.+)",        self._handle_reminder_at),
            (r"remind me(?: to)? (.+)",                self._handle_reminder),
            (r"set(?: a)? reminder(?: for)? (.+)",     self._handle_reminder),
            # BUG-5 fix: bare-trigger patterns — catch "set reminder" / "remind me" alone
            (r"^(?:set(?: a)?|add(?: a)?) reminder$",  self._handle_reminder_bare),
            (r"^remind me$",                           self._handle_reminder_bare),
            (r"^reminder$",                            self._handle_reminder_bare),

            # ── Notes ─────────────────────────────────────────────────────────
            (r"take a note(?: about)? (.+)",           self._handle_note),
            (r"note[: ]+(.+)",                         self._handle_note),
            (r"memo[: ]+(.+)",                         self._handle_note),
            # BUG-6 fix: bare-trigger patterns — catch "take a note" alone
            (r"^take a note$",                         self._handle_note_bare),
            (r"^(?:new|add(?: a)?) note$",             self._handle_note_bare),
            (r"^note$",                                self._handle_note_bare),

            # ── Training ──────────────────────────────────────────────────────
            (r"teach(?: jarvis)?[: ]+when i say (.+),? (?:do|open|start) (.+)",
                                                       self._handle_train),
            (r"train new command",                     self._handle_train_interactive),
            (r"forget (.+)",                           self._handle_forget),
            (r"list(?: my)? commands",                 self._handle_list_commands),

            # ── Workflows ─────────────────────────────────────────────────────
            (r"run workflow (.+)",                     self._handle_workflow_run),
            (r"create workflow (.+)",                  self._handle_workflow_create),
            (r"list workflows",                        self._handle_list_workflows),

            # ── Clipboard ─────────────────────────────────────────────────────
            (r"copy (.+) to clipboard",                self._handle_clipboard),
            (r"what'?s? in(?: my)? clipboard",         self._handle_clipboard_read),

            # ── Info — time ───────────────────────────────────────────────────
            # BUG-7 fix: exhaustive time phrasings; listed before date to avoid
            # "what's the time" being caught by a future date pattern.
            (r"what(?:'s| is)(?: the)? time",          self._handle_time),
            (r"what time is it",                       self._handle_time),
            (r"tell me the time",                      self._handle_time),
            (r"current time",                          self._handle_time),
            (r"time please",                           self._handle_time),
            (r"^time$",                                self._handle_time),

            # ── Info — date ───────────────────────────────────────────────────
            (r"what(?:'s| is)(?: the)? date",          self._handle_date),
            (r"what(?:'s| is) today",                  self._handle_date),
            (r"today'?s? date",                        self._handle_date),
            (r"what day is it",                        self._handle_date),

            # ── Info — general ────────────────────────────────────────────────
            (r"(?:how are you|are you there|hello|hi|hey)", self._handle_greeting),
            (r"help|what can you do",                  self._handle_help),
            (r"status|system status",                  self._handle_status),
        ]

    # ── Main entry point ─────────────────────────────────────────────────────

    def process(
        self,
        text: str,
        log_history: bool = True,
        use_fallback: bool = True,
    ) -> Optional[str]:
        """Match text against intents; return response string."""
        text = text.strip().lower()
        if not text:
            return None

        response = None

        # 1. Trained/custom commands (highest priority)
        trained = self.trainer.match(text)
        if trained:
            logger.info(f"Matched trained command: '{trained['trigger']}'")
            response = self.workflow.run_steps(trained["actions"])
        else:
            # 2. Regex intents
            for pattern, handler in self._intents:
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    logger.info(f"Intent matched: {pattern}")
                    try:
                        response = handler(m, text)
                    except Exception as exc:
                        logger.error(f"Handler error ({pattern}): {exc}", exc_info=True)
                        self.tts.speak_async("Sorry, something went wrong.")
                        response = None
                    break

        if response is None and not trained:
            # 3. Fallback
            logger.info(f"Intent needs fallback for: '{text}'")
            if use_fallback:
                response = self._fallback_clarification(text)

        # BUG-E fix: log the response too. DecisionEngine disables this so
        # completed turns are logged only once by the orchestrator.
        if log_history:
            self.memory.log_command(text, response or "")

        return response

    # ── Mode ─────────────────────────────────────────────────────────────────

    def _handle_mode(self, m, text):
        mode_name = m.group(1).strip().lower()
        return self.modes.activate(mode_name)

    # ── App ──────────────────────────────────────────────────────────────────

    def _handle_open(self, m, text):
        target = self._clean_app_target(m.group(1).strip())
        url = self.config.get_url(target)
        if url:
            self.browser.open_url(url)
            self.tts.speak_async(f"Opening {target}.")
            return f"Opened {target}"
        result = self.apps.open_app(target, self.config)
        if result:
            self.tts.speak_async(f"Opening {target}.")
            self.memory.set_context_entity("last_app", target)
            self.memory.record_action(
                "app_open", target, self.config.get("active_mode", "standard")
            )
            return f"Opened {target}"
        else:
            self.tts.speak_async(
                f"I couldn't find {target}. You can add it in settings."
            )
            return f"App not found: {target}"

    def _handle_close(self, m, text):
        target = self._clean_app_target(m.group(1).strip())
        result = self.apps.close_app(target)
        if result:
            self.tts.speak_async(f"Closed {target}.")
            self.memory.record_action(
                "app_close", target, self.config.get("active_mode", "standard")
            )
            return f"Closed {target}"
        else:
            self.tts.speak_async(f"Couldn't find {target} running.")
            return f"App not running: {target}"

    def _handle_exit_assistant(self, m, text):
        if self._assistant_shutdown:
            self.tts.speak_async("Shutting down JARVIS.")
            self._assistant_shutdown()
            return "Assistant shutdown requested"
        self.tts.speak_async("Shutdown is not available from this context.")
        return "Assistant shutdown unavailable"

    # ── System ───────────────────────────────────────────────────────────────

    def _handle_shutdown(self, m, text):
        self.tts.speak("Shutting down in 30 seconds. Say cancel to abort.")
        self.system.shutdown(delay=30)
        return "Shutdown scheduled"

    def _handle_restart(self, m, text):
        self.tts.speak("Restarting in 30 seconds. Say cancel to abort.")
        self.system.restart(delay=30)
        return "Restart scheduled"

    def _handle_sleep(self, m, text):
        self.tts.speak("Going to sleep.")
        self.system.sleep()
        return "Sleep"

    def _handle_lock(self, m, text):
        self.tts.speak_async("Locking the screen.")
        self.system.lock_screen()
        return "Locked"

    def _handle_screenshot(self, m, text):
        path = self.system.take_screenshot()
        self.tts.speak_async("Screenshot saved.")
        return f"Screenshot: {path}"

    def _handle_empty_trash(self, m, text):
        self.system.empty_recycle_bin()
        self.tts.speak_async("Recycle bin emptied.")
        return "Trash emptied"

    def _handle_task_manager(self, m, text):
        if self.apps.open_app("taskmgr", self.config):
            self.tts.speak_async("Opening task manager.")
            return "Task manager opened"
        self.tts.speak_async("I couldn't open Task Manager.")
        return "Task manager failed"

    def _clean_app_target(self, target: str) -> str:
        target = " ".join(str(target or "").strip().split())
        target = re.sub(r"^(?:my|the)\s+", "", target, flags=re.IGNORECASE)
        target = re.sub(
            r"\s+(?:again|please|for me)$", "", target, flags=re.IGNORECASE
        ).strip()
        return target

    # ── Volume ───────────────────────────────────────────────────────────────

    def _handle_vol_up(self, m, text):
        try:
            vol = self.media.volume_up()
        except self.media.AudioControlError as exc:
            return self._audio_failure("change the volume", exc)
        self.tts.speak_async(f"Volume up. Now at {vol} percent.")
        return f"Volume: {vol}%"

    def _handle_vol_down(self, m, text):
        try:
            vol = self.media.volume_down()
        except self.media.AudioControlError as exc:
            return self._audio_failure("change the volume", exc)
        self.tts.speak_async(f"Volume down. Now at {vol} percent.")
        return f"Volume: {vol}%"

    def _handle_mute(self, m, text):
        try:
            self.media.mute()
        except self.media.AudioControlError as exc:
            return self._audio_failure("mute audio", exc)
        self.tts.speak_async("Muted.")
        return "Muted"

    def _handle_unmute(self, m, text):
        try:
            self.media.unmute()
        except self.media.AudioControlError as exc:
            return self._audio_failure("unmute audio", exc)
        self.tts.speak_async("Unmuted.")
        return "Unmuted"

    def _handle_vol_set(self, m, text):
        level = int(m.group(1))
        try:
            verified = self.media.set_volume(level)
        except self.media.AudioControlError as exc:
            return self._audio_failure("set the volume", exc)
        self.tts.speak_async(f"Volume set to {verified} percent.")
        return f"Volume: {verified}%"

    def _audio_failure(self, action: str, exc: Exception):
        logger.error(f"Audio control failed while trying to {action}: {exc}")
        self.tts.speak_async(
            "I couldn't control Windows audio. Please check the default output device."
        )
        return f"Audio control failed: {exc}"

    # ── Media ─────────────────────────────────────────────────────────────────

    def _handle_play(self, m, text):
        # BUG-A fix: group(1) is None when "play" or "play music" is said
        target = (m.group(1) or "").strip() or "music"
        playlist = self.config.get_playlist(target)
        if playlist:
            self.browser.open_url(playlist)
            self.tts.speak_async(f"Playing {target} playlist.")
        else:
            self.media.play()
            self.tts.speak_async("Resuming playback.")
        return "Playing"

    def _handle_pause(self, m, text):
        self.media.pause()
        self.tts.speak_async("Paused.")
        return "Paused"

    def _handle_stop(self, m, text):
        self.media.stop()
        self.tts.speak_async("Stopped.")
        return "Stopped"

    def _handle_next(self, m, text):
        self.media.next_track()
        self.tts.speak_async("Next track.")
        return "Next"

    def _handle_prev(self, m, text):
        self.media.prev_track()
        self.tts.speak_async("Previous track.")
        return "Previous"

    # ── Browser ──────────────────────────────────────────────────────────────

    def _handle_browse(self, m, text):
        target = m.group(1).strip()
        url = self.config.get_url(target)
        if not url:
            url = (
                f"https://{target}"
                if "." in target
                else f"https://www.{target}.com"
            )
        self.browser.open_url(url)
        self.tts.speak_async(f"Opening {target}.")
        return f"Browsed: {url}"

    def _handle_search(self, m, text):
        query = m.group(1).strip()
        url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        self.browser.open_url(url)
        self.tts.speak_async(f"Searching for {query}.")
        return f"Search: {query}"

    # ── Reminders & Notes ─────────────────────────────────────────────────────

    def _handle_reminder_at(self, m, text):
        task     = m.group(1).strip()
        time_str = m.group(2).strip()
        result   = self.reminder.add_from_text(task, time_str)
        if result:
            self.memory.set_context_entity("reminder_text", task)
            rid = getattr(self.reminder, "_last_reminder_id", None)
            if rid:
                self.memory.set_context_entity("reminder_id", rid)
            self.tts.speak_async(f"Reminder set: {task} at {time_str}.")
        else:
            self.tts.speak_async("Sorry, I couldn't parse that time.")
        return result

    def _handle_reminder_reschedule(self, m, text):
        task_hint = m.group(1).strip()
        time_str = m.group(2).strip()
        reminder_id = None
        if task_hint.lower() in {"it", "that", "this", "the reminder"}:
            reminder_id = self.memory.get_context_entity("reminder_id")
        result = self.reminder.reschedule_from_text(task_hint, time_str, reminder_id)
        if result:
            reminder_text = result.split(" moved to ", 1)[0].strip()
            self.memory.set_context_entity("reminder_text", reminder_text or task_hint)
            rid = getattr(self.reminder, "_last_reminder_id", None)
            if rid:
                self.memory.set_context_entity("reminder_id", rid)
            self.tts.speak_async(f"Done. {result}.")
        else:
            self.tts.speak_async("I couldn't find a matching reminder to move.")
        return result

    def _handle_reminder(self, m, text):
        # BUG-B fix: determine which group holds the task text
        task = ""
        if m.lastindex:
            # Last non-None group is the task
            for i in range(m.lastindex, 0, -1):
                try:
                    g = m.group(i)
                    if g:
                        task = g.strip()
                        break
                except IndexError:
                    pass
        self.tts.speak_async(
            f"What time should I remind you about: {task}?"
        )
        if task:
            self.memory.set_context_entity("reminder_text", task)
        return f"Reminder pending: {task}"

    def _handle_reminder_bare(self, m, text):
        """BUG-5 fix: 'set reminder' / 'remind me' with no task — prompt user."""
        self.tts.speak_async(
            "Sure. What would you like me to remind you about, and at what time?"
        )
        return "Reminder prompt"

    def _handle_note(self, m, text):
        note = m.group(1).strip()
        self.memory.add_note(note)
        self.tts.speak_async("Note saved.")
        return f"Note: {note}"

    def _handle_note_bare(self, m, text):
        """BUG-6 fix: 'take a note' / 'new note' with no content — prompt user."""
        self.tts.speak_async("Sure. What would you like to note?")
        return "Note prompt"

    # ── Training ─────────────────────────────────────────────────────────────

    def _handle_train(self, m, text):
        trigger     = m.group(1).strip()
        action_text = m.group(2).strip()
        self.trainer.teach(trigger, action_text)
        self.tts.speak_async(
            f"Got it. I'll remember that when you say '{trigger}'."
        )
        return f"Trained: {trigger}"

    def _handle_train_interactive(self, m, text):
        self.tts.speak_async(
            "Sure. What phrase should trigger the command? "
            "Say the trigger word or phrase."
        )
        return "Training mode started"

    def _handle_forget(self, m, text):
        cmd = m.group(1).strip()
        self.trainer.forget(cmd)
        self.tts.speak_async(f"Forgotten: {cmd}.")
        return f"Deleted: {cmd}"

    def _handle_list_commands(self, m, text):
        cmds = self.trainer.list_commands()
        if cmds:
            names = ", ".join(cmds[:5])
            self.tts.speak_async(
                f"You have {len(cmds)} custom commands. Including: {names}."
            )
        else:
            self.tts.speak_async("No custom commands trained yet.")
        return f"Commands: {len(cmds)}"

    # ── Workflows ────────────────────────────────────────────────────────────

    def _handle_workflow_run(self, m, text):
        name   = m.group(1).strip()
        result = self.workflow.run_by_name(name)
        if result is not None:
            self.tts.speak_async(f"Running workflow: {name}.")
        else:
            self.tts.speak_async(f"Couldn't find workflow '{name}'.")
        return result

    def _handle_workflow_create(self, m, text):
        name = m.group(1).strip()
        self.tts.speak_async(
            f"Creating workflow '{name}'. "
            "Describe the steps, or set them up in the dashboard."
        )
        return f"Workflow creation: {name}"

    def _handle_list_workflows(self, m, text):
        flows = self.workflow.list_names()
        if flows:
            self.tts.speak_async(
                f"You have {len(flows)} workflows: {', '.join(flows[:4])}."
            )
        else:
            self.tts.speak_async("No workflows defined yet.")
        return f"Workflows: {flows}"

    # ── Clipboard ────────────────────────────────────────────────────────────

    def _handle_clipboard(self, m, text):
        content = m.group(1).strip()
        self.system.copy_to_clipboard(content)
        self.tts.speak_async("Copied to clipboard.")
        return f"Clipboard: {content}"

    def _handle_clipboard_read(self, m, text):
        content = self.system.read_clipboard()
        self.tts.speak_async(
            f"Clipboard contains: {content[:100] if content else 'nothing'}"
        )
        return f"Clipboard: {content}"

    # ── Info ─────────────────────────────────────────────────────────────────

    def _handle_reminder_prompt(self, m, text):
        """BUG-5 fix: 'set reminder' with no content — prompt the user."""
        self.tts.speak_async(
            "Sure, what should I remind you about, and when?"
        )
        return "Reminder: awaiting details"

    def _handle_note_prompt(self, m, text):
        """BUG-6 fix: 'take a note' with no content — prompt the user."""
        self.tts.speak_async(
            "What would you like me to note?"
        )
        return "Note: awaiting content"

    def _handle_time(self, m, text):
        from datetime import datetime
        now = datetime.now().strftime("%I:%M %p")
        self.tts.speak_async(f"It's {now}.")
        return now

    def _handle_date(self, m, text):
        from datetime import datetime
        now = datetime.now().strftime("%A, %B %d, %Y")
        self.tts.speak_async(f"Today is {now}.")
        return now

    def _handle_greeting(self, m, text):
        import random
        responses = [
            "At your service.",
            "Online and ready.",
            "How can I help?",
            "Standing by.",
        ]
        self.tts.speak_async(random.choice(responses))
        return "Greeting"

    def _handle_help(self, m, text):
        self.tts.speak_async(
            "I can open apps, control volume, activate modes, set reminders, "
            "run workflows, browse the web, and learn custom commands. "
            "Check the dashboard for the full command list."
        )
        return "Help"

    def _handle_status(self, m, text):
        # BUG-C fix: guard psutil import
        try:
            import psutil
            cpu  = psutil.cpu_percent(interval=1)
            ram  = psutil.virtual_memory().percent
            mode = self.config.get("active_mode", "standard")
            msg  = (
                f"System status: CPU at {cpu:.0f} percent, "
                f"RAM at {ram:.0f} percent. Current mode: {mode}."
            )
            self.tts.speak_async(msg)
            return f"CPU:{cpu}% RAM:{ram}%"
        except Exception as exc:
            logger.error(f"Status handler error: {exc}")
            self.tts.speak_async("Could not retrieve system status.")
            return "Status unavailable"

    def _fallback_clarification(self, text: str) -> str:
        msg = (
            "I didn't quite catch that. Did you want to open something, "
            "set a reminder, or update something you told me?"
        )
        self.tts.speak_async(msg)
        return f"Clarifying: {text}"
