"""Phone calls: Alfred rings its user, reports what it did, and takes the next task by voice.

Two halves, in two processes:

  voice worker   `python -m alfred.voice start` — a LiveKit agent. One job is one
                 outbound call: dial the user over a SIP trunk, speak the report,
                 listen for the next task (Gemini Live does speech in and out),
                 hang up, and write what it heard to a result file.
  caller         call_user() in the main Alfred process: makes sure the worker is
                 up, dispatches a job to it, and waits for the result file.

What the voice model *says* changes nothing. The only way a spoken request
becomes work is the `submit_task` tool, and the task it captures then runs
through the normal worker with the same enforcer and verification as a typed one.

Needs in .env (see .env.example): LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
SIP_OUTBOUND_TRUNK_ID, GOOGLE_API_KEY, and optionally SIP_CALLER_ID.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

AGENT_NAME = os.environ.get("ALFRED_VOICE_AGENT", "alfred-voice")
VOICE_MODEL = os.environ.get("ALFRED_VOICE_MODEL", "gemini-3.8-live")
VOICE_NAME = os.environ.get("ALFRED_VOICE_NAME", "Aoede")
VOICE_LANGUAGE = os.environ.get("ALFRED_VOICE_LANGUAGE", "hi-IN")   # handles English and Hinglish
REQUIRED = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "SIP_OUTBOUND_TRUNK_ID", "GOOGLE_API_KEY")
RINGING_TIMEOUT_S = 30
MAX_CALL_S = 180

PROMPT = """\
You are Alfred, an AI operations worker, on a phone call with the colleague you work for. Speak naturally and \
briefly, like a capable assistant on a quick call. Speak English by default; if they speak Hindi or Hinglish, \
match them.

What you have to report:
{report}

How the call goes
1. Greet them in one short sentence and say you are Alfred.
2. Give the report in two or three spoken sentences: the outcome first, then the key values. Do not read out \
URLs, file paths or long IDs. If they ask for more detail, give it from the report; if it is not in the report, \
say you do not have it.
3. Ask whether there is anything else they would like done.
4. If they give you a task, repeat it back in one sentence to confirm you understood. Once they confirm, call \
`submit_task` with the task written out fully and precisely in English, including every name, number and \
detail they gave. Then say you are on it and will call back when it is done.
5. If they have nothing else, call `no_more_tasks`.
6. Say a short goodbye and call `end_call`.

Rules
- You cannot do any work during the call. You only report and take the next task. Never claim something is done \
that is not in the report.
- Take one task per call. If they list several, capture them together as one task in their words.
- If the line is unclear, ask them to repeat rather than guessing.\
"""

FIRST_CALL_REPORT = "Nothing yet: this is the start of the session. Skip the report and ask what they would like done."


def missing_config() -> list[str]:
    return [k for k in REQUIRED if not os.environ.get(k)]


# --------------------------------------------------------------------------- caller side (main process)
_worker: subprocess.Popen | None = None


def ensure_worker(timeout: float = 60) -> None:
    """Start the voice worker if it is not running and wait until LiveKit has registered it."""
    global _worker
    if _worker and _worker.poll() is None:
        return
    log = Path(tempfile.gettempdir()) / "alfred-voice-worker.log"
    out = log.open("w", encoding="utf-8")
    _worker = subprocess.Popen([sys.executable, "-m", "alfred.voice", "start"], stdout=out, stderr=subprocess.STDOUT)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _worker.poll() is not None:
            raise RuntimeError(f"The voice worker exited on start. See {log}")
        if "registered worker" in log.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(0.5)
    raise RuntimeError(f"The voice worker did not register with LiveKit in {timeout:.0f}s. See {log}")


def stop_worker() -> None:
    global _worker
    if _worker and _worker.poll() is None:
        _worker.terminate()
    _worker = None


async def _dispatch(room: str, metadata: str) -> None:
    from livekit import api
    lk = api.LiveKitAPI(url=os.environ["LIVEKIT_URL"], api_key=os.environ["LIVEKIT_API_KEY"],
                        api_secret=os.environ["LIVEKIT_API_SECRET"])
    try:
        await lk.room.create_room(api.CreateRoomRequest(name=room))
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(room=room, agent_name=AGENT_NAME, metadata=metadata))
    finally:
        await lk.aclose()


def call_user(phone: str, report: str = "", timeout: float = MAX_CALL_S + 90) -> dict:
    """Ring `phone`, speak `report`, and return {"answered", "next_task", "transcript", "error"}."""
    gaps = missing_config()
    if gaps:
        raise RuntimeError(f"Calling is not configured. Missing in .env: {', '.join(gaps)}")
    ensure_worker()
    call_id = uuid.uuid4().hex[:10]
    result_file = Path(tempfile.gettempdir()) / f"alfred-call-{call_id}.json"
    metadata = json.dumps({"phone": phone, "report": report or FIRST_CALL_REPORT, "result_file": str(result_file)})
    # Dispatch on its own thread so this also works when the caller already runs an event loop.
    errors: list[Exception] = []

    def go() -> None:
        try:
            asyncio.run(_dispatch(f"alfred-call-{call_id}", metadata))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=go)
    t.start()
    t.join()
    if errors:
        raise RuntimeError(f"Could not start the call: {errors[0]}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if result_file.exists():
            time.sleep(0.3)   # let the worker finish writing
            data = json.loads(result_file.read_text(encoding="utf-8"))
            result_file.unlink(missing_ok=True)
            return data
        time.sleep(1)
    return {"answered": False, "next_task": None, "transcript": [], "error": "The call did not report back in time."}


def spoken_report(task: str, outcome: dict) -> str:
    """What the voice agent is given to say about a finished run."""
    verified = {True: "It passed independent verification.", False: "It did NOT pass independent verification.",
                None: ""}[outcome.get("verified")]
    details = "\n".join(f"- {d}" for d in outcome.get("details", [])[:6])
    return (f"The task was: {task}\nOutcome status: {outcome['status']}. {verified}\n"
            f"Summary: {outcome.get('summary', '')}\n{details}")


# --------------------------------------------------------------------------- worker side (LiveKit agent process)
def _build_agent(report: str):
    from livekit.agents import Agent, RunContext, function_tool

    class IntakeAgent(Agent):
        def __init__(self) -> None:
            super().__init__(instructions=PROMPT.format(report=report))
            self.next_task: str | None = None
            self.ended = False
            self.transcript: list[dict] = []

        def record(self, role: str, text: str) -> None:
            if text and text.strip():
                self.transcript.append({"role": role, "text": text.strip()})

        @function_tool
        async def submit_task(self, ctx: RunContext, task: str) -> str:
            """Call this once the colleague has stated the next task and confirmed your read-back.
            `task` is the full task in clear written English with every detail they gave."""
            self.next_task = task.strip()
            return "Task recorded. Tell them you are on it and will call back when it is done, then end the call."

        @function_tool
        async def no_more_tasks(self, ctx: RunContext) -> str:
            """Call this when the colleague says there is nothing else to do."""
            self.next_task = None
            return "Understood. Say a short goodbye and end the call."

        @function_tool
        async def end_call(self, ctx: RunContext) -> str:
            """Call this to hang up once the conversation is finished."""
            self.ended = True
            return "__END_CALL__"

    return IntakeAgent()


async def _dial(ctx, phone: str) -> bool:
    """Ring the user into the room. True once answered; False for no answer, busy or declined."""
    from google.protobuf.duration_pb2 import Duration
    from livekit.protocol.sip import CreateSIPParticipantRequest
    req = CreateSIPParticipantRequest(
        room_name=ctx.room.name, sip_trunk_id=os.environ["SIP_OUTBOUND_TRUNK_ID"], sip_call_to=phone,
        participant_identity=phone, wait_until_answered=True, ringing_timeout=Duration(seconds=RINGING_TIMEOUT_S))
    if os.environ.get("SIP_CALLER_ID"):
        req.sip_number = os.environ["SIP_CALLER_ID"]
    try:
        await ctx.api.sip.create_sip_participant(req)
        return True
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__}: {exc}".lower()
        if any(t in text for t in ("no answer", "busy", "declined", "rejected", "not found", "unavailable",
                                   "timeout", "canceled", "cancelled", "486", "480", "603", "404")):
            return False
        raise


async def entrypoint(ctx) -> None:   # ctx: livekit.agents.JobContext
    from google.genai import types as genai_types
    from livekit import api
    from livekit.agents import AgentSession
    from livekit.plugins.google.beta import realtime

    meta = json.loads(ctx.job.metadata or "{}")
    result: dict = {"answered": False, "next_task": None, "transcript": [], "error": None}
    agent = _build_agent(meta.get("report") or FIRST_CALL_REPORT)
    session = None
    try:
        await ctx.connect()
        model = realtime.RealtimeModel(
            model=VOICE_MODEL, api_key=os.environ["GOOGLE_API_KEY"], voice=VOICE_NAME, language=VOICE_LANGUAGE,
            temperature=0.6,
            input_audio_transcription=genai_types.AudioTranscriptionConfig(),
            output_audio_transcription=genai_types.AudioTranscriptionConfig(),
            tool_behavior=genai_types.Behavior.BLOCKING,   # do not keep talking while a tool call is pending
            realtime_input_config=genai_types.RealtimeInputConfig(
                automatic_activity_detection=genai_types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=genai_types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_HIGH,
                    prefix_padding_ms=100, silence_duration_ms=500)))
        session = AgentSession(llm=model)
        speaking = {"now": False}
        closed = asyncio.Event()

        @session.on("conversation_item_added")
        def _item(ev) -> None:
            item = getattr(ev, "item", ev)
            if getattr(item, "role", "") == "user":
                return   # the user's side arrives through user_input_transcribed
            text = getattr(item, "text_content", None) or ""
            agent.record("alfred", str(text))

        @session.on("user_input_transcribed")
        def _user(ev) -> None:
            if getattr(ev, "is_final", True) and getattr(ev, "transcript", ""):
                agent.record("user", ev.transcript)

        @session.on("agent_state_changed")
        def _state(ev) -> None:
            speaking["now"] = getattr(ev, "new_state", None) == "speaking"

        @session.on("close")
        def _close(_ev) -> None:
            closed.set()

        if not await _dial(ctx, meta["phone"]):
            result["error"] = "no answer"
            return
        result["answered"] = True
        await session.start(agent=agent, room=ctx.room)
        await session.generate_reply(user_input="The call just connected. Start: greet them and give the report.")

        async def wait_for_end() -> None:
            while not closed.is_set():
                if agent.ended:
                    for _ in range(40):          # let the goodbye finish playing, up to ~20s
                        if not speaking["now"]:
                            break
                        await asyncio.sleep(0.5)
                    return
                await asyncio.sleep(0.5)

        try:
            await asyncio.wait_for(wait_for_end(), timeout=MAX_CALL_S)
        except asyncio.TimeoutError:
            result["error"] = "call hit the maximum duration"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["next_task"] = agent.next_task
        result["transcript"] = agent.transcript
        if meta.get("result_file"):
            Path(meta["result_file"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        if session is not None:
            try:
                await session.aclose()
            except Exception:  # noqa: BLE001
                pass
        try:   # closing the session leaves the phone leg bridged; deleting the room is what hangs up
            await asyncio.wait_for(ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name)), timeout=10)
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    from livekit.agents import WorkerOptions, cli
    from livekit.plugins.google.beta import realtime  # noqa: F401 - plugins must register on the main thread
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME))


if __name__ == "__main__":
    main()
