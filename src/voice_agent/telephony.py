"""ConversationRelay transport: TwiML generation and the /conversation WebSocket."""

import asyncio
import json
import re
from contextlib import aclosing, suppress
from time import monotonic
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import structlog
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from starlette.datastructures import FormData
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from voice_agent.prompts import GREETING, SYSTEM_UNAVAILABLE
from voice_agent.prompts.phrases import message as phrase
from voice_agent.session import Session, Turn
from voice_agent.settings import Settings

router = APIRouter()

KNOWN_EVENTS = {"setup", "prompt", "interrupt", "error", "disconnect"}


"""Useful validation functions for Twilio call validation."""


def call_id(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"CA[0-9a-fA-F]{32}", value):
        return value
    return None


def valid_event_type(message: object, active_call_id: str | None) -> str | None:
    """Return the event's type if the message is well-formed, else None."""
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        return None
    event_type = message["type"]
    if event_type == "setup":
        setup_already_done = active_call_id is not None
        bad_call_sid = call_id(message.get("callSid")) is None
        bad_caller_phone = not isinstance(message.get("from", ""), str)
        if setup_already_done or bad_call_sid or bad_caller_phone:
            return None
    if event_type == "prompt":
        no_active_call = active_call_id is None
        bad_prompt_text = not isinstance(message.get("voicePrompt"), str)
        bad_last_flag = not isinstance(message.get("last"), bool)
        bad_language = not isinstance(message.get("lang", ""), str)
        if no_active_call or bad_prompt_text or bad_last_flag or bad_language:
            return None
    if event_type == "interrupt" and not isinstance(message.get("utteranceUntilInterrupt", ""), str):
        return None
    return event_type

async def authenticate_websocket(websocket: WebSocket) -> bool:
    provider = websocket.app.state.telephony
    url = provider.wss_url("/conversation", websocket.url.query)
    signature = websocket.headers.get("x-twilio-signature", "")
    if not provider.validate(url, FormData(), signature):
        structlog.get_logger().warning("twilio_signature_rejected", event_type="handshake")
        await websocket.close(code=1008)
        return False
    await websocket.accept()
    return True

async def validated_form(request: Request) -> FormData:
    provider = request.app.state.telephony
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type != "application/x-www-form-urlencoded" and await request.body():
        raise HTTPException(415, "Expected form-encoded webhook")
    try:
        params = FormData(parse_qsl((await request.body()).decode("utf-8"), keep_blank_values=True))
    except UnicodeDecodeError:
        raise HTTPException(400, "Invalid webhook encoding") from None

    url = provider.https_url(request.scope["route"].path, request.url.query)
    signature = request.headers.get("x-twilio-signature", "")
    if not provider.validate(url, params, signature):
        structlog.get_logger().warning("twilio_signature_rejected", event_type="voice")
        raise HTTPException(403, "Invalid Twilio signature")

    return params


"""TwilioProvider: handles Twilio request validation and TwiML generation."""
class TwilioProvider:
    def __init__(self, settings: Settings):
        base = settings.public_base_url.rstrip("/")
        self._netloc = urlsplit(base).netloc
        self._path = urlsplit(base).path
        self._voice = settings.relay_voice
        self._validator = RequestValidator(settings.twilio_auth_token.get_secret_value())

    def https_url(self, path: str, query: str = "") -> str:
        return urlunsplit(("https", self._netloc, self._path + path, query, ""))

    def wss_url(self, path: str, query: str = "") -> str:
        return urlunsplit(("wss", self._netloc, self._path + path, query, ""))

    def validate(self, url: str, params: FormData, signature: str) -> bool:
        if not signature:
            return False
        return self._validator.validate(url, params, signature)

    def voice_twiml(self) -> str:
        response = VoiceResponse()
        response.connect(action=self.https_url("/call-ended"), method="POST").conversation_relay(
            url=self.wss_url("/conversation"),
            welcome_greeting=GREETING,
            interruptible="speech",
            report_input_during_agent_speech="speech",
            preemptible=True,
            language="multi",
            transcription_provider="Deepgram",
            speech_model="nova-3-general",
            tts_provider="ElevenLabs",
            voice=self._voice,
            events="speaker-events tokens-played",
        )
        return str(response)


"""Voice endpoint for handling the first incoming call. --> greetings & sends to /conversation"""
@router.post("/voice")
async def voice(request: Request) -> Response:
    params = await validated_form(request)
    structlog.get_logger().info("twilio_event", event_type="voice", call_id=call_id(params.get("CallSid")))
    return Response(request.app.state.telephony.voice_twiml(), media_type="application/xml")


"""Endpoint for handling the end of a call. --> plays farewell message and hangs up."""
@router.post("/call-ended")
async def call_ended(request: Request) -> Response:
    params = await validated_form(request)
    response = VoiceResponse()

    # A farewell is played by TwiML before Hangup, so ending the relay cannot
    # cut off buffered farewell speech. Handoff data contains no caller details.
    reason, _, hint = params.get("HandoffData", "").partition(":")
    language = "es" if hint == "es" or reason == "es" else "en"
    text = ""
    if reason == "completed":
        text = phrase("goodbye", language)
    elif reason == "silence":
        text = phrase("silence_goodbye", language)
    elif reason in {"failed", "en", "es"}:
        text = SYSTEM_UNAVAILABLE[language]
    if text:
        response.say(text, language="es-MX" if language == "es" else "en-US")
    response.hangup()
    structlog.get_logger().info("call_ending", call_id=call_id(params.get("CallSid")), text=text)
    return Response(str(response), media_type="application/xml")

"""WebSocket endpoint for handling the conversation."""
@router.websocket("/conversation")
async def conversation(websocket: WebSocket) -> None:
    if not await authenticate_websocket(websocket):
        return
    call = RelayCall(websocket)
    await call.run()


class RelayCall:
    """Own one WebSocket and its single cancellable reply task.

    Session and the agent own business decisions; this class only handles transport.
    """

    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.logger = structlog.get_logger()
        self.call_id: str | None = None
        self.session: Session | None = None
        self.reply_task: asyncio.Task | None = None
        self.close_code = 1000
        self.ending = False
        self.silence_task: asyncio.Task | None = None
        self.activity = asyncio.Event()
        self.silence_deadline = 0.0
        self.silence_count = 0

    async def run(self) -> None:
        try:
            await self.receive_events()
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self.close_socket(1008)
        except WebSocketDisconnect as error:
            self.close_code = error.code
        finally:
            await self.cleanup()

    async def receive_events(self) -> None:
        while True:
            message = await self.websocket.receive_json()
            event_type = valid_event_type(message, self.call_id)
            if event_type is None:
                self.logger.warning("twilio_invalid_message", call_id=self.call_id)
                await self.close_socket(1008)
                return
            if not await self.handle_event(event_type, message):
                return

    async def handle_event(self, event_type: str, message: dict) -> bool:
        """Dispatch an event; return False when the receive loop should stop."""
        if event_type == "setup":
            self.start_session(message["callSid"], message.get("from", ""))
        self.note_activity(caller=event_type in {"prompt", "interrupt"})
        if event_type == "disconnect":
            await self.close_socket(1000)
            return False
        if self.ending or (self.session is not None and self.session.failed):
            return True

        if event_type == "error":
            await self.close_socket(1011)
            return False
        if event_type == "interrupt" and self.session is not None:
            await self.handle_interrupt(message)
        elif event_type == "prompt":
            await self.handle_prompt(message)
        return True

    def start_session(self, identifier: str, caller_phone: str) -> None:
        state = self.websocket.app.state
        self.call_id = identifier
        self.session = Session(identifier, state.graph, state.context, caller_phone=caller_phone)
        self.note_activity(playback=GREETING)
        self.silence_task = asyncio.create_task(self.watch_silence())

    async def end_session(self, reason: str) -> None:
        if self.ending:
            return
        self.ending = True
        await self.websocket.send_json({"type": "end", "handoffData": f"{reason}:{self.session.language}"})
        self.logger.info("session_ended", call_id=self.call_id, reason=reason)

    async def handle_prompt(self, message: dict) -> None:
        if not message["voicePrompt"].strip():
            return
        await self.cancel_reply()
        # Interim transcripts cancel stale speech but never start a new turn.
        if message["last"]:
            turn = self.session.start_turn(message["voicePrompt"], message.get("lang", ""))
            self.reply_task = asyncio.create_task(self.send_reply(turn))

    async def handle_interrupt(self, message: dict) -> None:
        await self.cancel_reply()
        self.session.interrupt(message.get("utteranceUntilInterrupt"))

    async def cancel_reply(self) -> None:
        if self.reply_task is not None and not self.reply_task.done():
            self.session.interrupt()
            self.reply_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.reply_task
        self.reply_task = None

    # Send the agent's reply to the client, handling interruptions and session state.
    async def send_reply(self, turn: Turn) -> None:
        delivered_text = ""
        try:
            async with aclosing(self.session.reply(turn)) as tokens:
                async for token in tokens:
                    if not self.session.is_current(turn):
                        return
                    await self.websocket.send_json({"type": "text", "token": token, "last": False, "interruptible": True, "preemptible": True})
                    delivered_text += token
        except (WebSocketDisconnect, RuntimeError, OSError):
            if self.session.is_current(turn):
                self.session.interrupt()
            self.logger.warning("reply_delivery_failed", call_id=self.call_id, turn_id=turn.id)
        finally:
            # Close even an interrupted talk cycle before starting the next one.
            self.logger.info("agent_text_sent", call_id=self.call_id, turn_id=turn.id, text=delivered_text, interrupted=turn.interrupted)
            if delivered_text:
                with suppress(WebSocketDisconnect, RuntimeError, OSError):
                    await self.websocket.send_json({"type": "text", "token": "", "last": True})
            if self.session.failed:
                with suppress(WebSocketDisconnect, RuntimeError, OSError):
                    await self.end_session("failed")
            elif self.session.finished:
                with suppress(WebSocketDisconnect, RuntimeError, OSError):
                    await self.end_session("completed")
            self.note_activity(playback=delivered_text if not turn.interrupted else "")

    async def close_socket(self, code: int) -> None:
        self.close_code = code
        await self.websocket.close(code=code)

    async def cleanup(self) -> None:
        self.ending = True
        if self.silence_task:
            self.silence_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.silence_task
        await self.cancel_reply()
        if self.session is not None:
            await self.session.close()
        self.logger.info("twilio_disconnected", event_type="disconnect", call_id=self.call_id, close_code=self.close_code)

    # Record activity to prevent premature silence detection.
    def note_activity(self, playback: str = "", caller: bool = False) -> None:
        timeout = self.websocket.app.state.settings.silence_timeout_seconds
        deadline = monotonic() + timeout + len(playback.split()) / 2.0
        self.silence_deadline = deadline if caller else max(self.silence_deadline, deadline)
        if caller:
            self.silence_count = 0
        self.activity.set()

    async def watch_silence(self) -> None:
        try:
            while not self.ending:
                self.activity.clear()
                delay = max(0, self.silence_deadline - monotonic())
                try:
                    await asyncio.wait_for(self.activity.wait(), timeout=delay)
                    continue
                except TimeoutError:
                    pass
                if self.reply_task and not self.reply_task.done():
                    # Provider work has its own deadline; don't talk over it.
                    await self.activity.wait()
                    continue
                if self.silence_count:
                    await self.end_session("silence")
                    return
                self.silence_count += 1
                text = phrase("silence", self.session.language)
                self.session.note_silence_prompt(text)
                await self.websocket.send_json({"type": "text", "token": text, "last": True})
                self.note_activity(playback=text)
                self.logger.info("silence_prompt", call_id=self.call_id)
        except (WebSocketDisconnect, RuntimeError, OSError):
            self.logger.warning("silence_delivery_failed", call_id=self.call_id)
